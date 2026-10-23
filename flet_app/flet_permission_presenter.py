"""Flet frontend for the permission rendezvous.

The router receives ``permission.asked`` from the OpenCode server and waits
for a human. This adapter renders the request as a modal ``ft.AlertDialog``
with the three possible decisions and, when the router settles the request,
pops the dialog and appends a small muted notice to the conversation.

Design notes:

  - **One modal at a time.** Flet's dialog stack is global to the page; two
    simultaneous ``permission.asked`` events would otherwise fight for the
    same overlay. ``show`` queues every request behind the visible one.
  - **The decision callback is injected.** Buttons call the router's
    ``resolve_from_ui`` (passed as ``resolve``) instead of importing the
    router, which keeps this module Flet-only and free of a circular import.
  - **The router notifies presenters after every decision** (Phase 4), so
    ``resolve`` is where the dialog is popped and the outcome is rendered.
    Calling the callback does NOT pop anything directly.
  - **Everything is async** on Flet's single event loop. Event handlers are
    coroutines (Flet awaits them); ``page.run_thread`` is never used because
    the core is loop-bound.
  - **Failures degrade gracefully.** A missing/closed page, a dialog already
    popped by the client, or a late resolution for a queued request must
    never crash the router or the backend.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import flet as ft

from flet_app.components.message_bubble import assistant_message
from flet_app.core.design_system import DS
from services.permissions import (
    PermissionDecision,
    PermissionRequest,
    Reply,
)
from utils.logging import get_module_logger

logger = get_module_logger(__name__)

TITLE = "🔐 Permiso requerido"

PERMIT_LABEL = "✅ Permitir"
ALWAYS_LABEL = "🔁 Siempre"
REJECT_LABEL = "❌ Rechazar"

RESULT_PERMITTED = "✅ Permiso permitido"
RESULT_PERMITTED_ALWAYS = "🔁 Permiso permitido siempre"
RESULT_REJECTED = "❌ Permiso rechazado"
RESULT_TIMED_OUT = "⏱️ El permiso expiró y se rechazó automáticamente"
RESULT_CANCELLED = "⏹️ Permiso cancelado (turno interrumpido)"

#: ``(request_id, decision) -> accepted`` — the router's ``resolve_from_ui``.
ResolveCallback = Callable[[str, PermissionDecision], Awaitable[bool]]
DecisionHandler = Callable[[str, PermissionDecision], Awaitable[None]]


def result_text(decision: PermissionDecision) -> str:
    """Return the one-line outcome rendered into the conversation.

    ``timed_out`` wins over ``cancelled``: the backend's own budget expiring
    cancels the task but the human timed out, so "expired" is the honest
    message. A real interruption (turn cancelled) carries only ``cancelled``.
    """
    if decision.timed_out:
        return RESULT_TIMED_OUT
    if decision.cancelled:
        return RESULT_CANCELLED
    if decision.reply == "reject":
        return RESULT_REJECTED
    if decision.reply == "always":
        return RESULT_PERMITTED_ALWAYS
    return RESULT_PERMITTED


def _detail(label: str, value: str) -> ft.Control:
    """A muted label above a selectable value (DS tokens only)."""
    return ft.Column(
        controls=[
            ft.Text(
                value=label,
                size=DS.typography.muted_size,
                color=DS.colors.text_secondary,
            ),
            ft.Text(
                value=value,
                size=DS.typography.body_size,
                color=DS.colors.text_primary,
                selectable=True,
            ),
        ],
        spacing=DS.spacing.xs,
        tight=True,
    )


def build_details(request: PermissionRequest) -> ft.Column:
    """Render action, pattern(s), command and ``always`` patterns."""
    rows: list[ft.Control] = [_detail("Acción", request.permission)]

    if len(request.patterns) == 1:
        rows.append(_detail("Patrón", request.patterns[0]))
    elif request.patterns:
        rows.append(_detail("Patrones", "\n".join(request.patterns)))

    command = request.metadata.get("command")
    if isinstance(command, str) and command:
        rows.append(_detail("Comando", command))

    if request.always:
        rows.append(_detail("Siempre", ", ".join(request.always)))

    return ft.Column(
        controls=rows,
        spacing=DS.spacing.md,
        tight=True,
    )


def _action(
    label: str,
    reply: Reply,
    color: str,
    request_id: str,
    decide: DecisionHandler,
) -> ft.TextButton:
    """One action button whose click delivers ``reply`` to the router."""

    async def handle_click(_: ft.Event[ft.TextButton]) -> None:
        await decide(request_id, PermissionDecision(reply=reply))

    return ft.TextButton(
        content=ft.Text(value=label),
        style=ft.ButtonStyle(color=color),
        on_click=handle_click,
    )


def build_permission_dialog(
    request: PermissionRequest,
    decide: DecisionHandler,
) -> ft.AlertDialog:
    """Build the modal dialog with the three decisions (pure, testable)."""
    return ft.AlertDialog(
        modal=True,
        title=ft.Text(
            value=TITLE,
            size=DS.typography.title_size,
            weight=DS.typography.title_weight,
            color=DS.colors.text_primary,
        ),
        content=build_details(request),
        bgcolor=DS.colors.surface,
        actions=[
            _action(
                PERMIT_LABEL,
                "once",
                DS.colors.accent,
                request.id,
                decide,
            ),
            _action(
                ALWAYS_LABEL,
                "always",
                DS.colors.accent_soft,
                request.id,
                decide,
            ),
            _action(
                REJECT_LABEL,
                "reject",
                DS.colors.text_secondary,
                request.id,
                decide,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )


class FletPermissionPresenter:
    """Renders permission requests as queued modal dialogs."""

    def __init__(
        self,
        page: ft.Page,
        conversation_column: ft.Column,
        resolve: ResolveCallback,
    ) -> None:
        self._page = page
        self._column = conversation_column
        self._resolve = resolve
        self._queue: list[PermissionRequest] = []
        self._current: PermissionRequest | None = None
        self._dialog: ft.AlertDialog | None = None

    @property
    def pending_count(self) -> int:
        """Requests visible or waiting for their turn."""
        return len(self._queue) + (1 if self._current is not None else 0)

    async def show(self, request: PermissionRequest) -> None:
        """Show the request, or queue it behind the visible dialog."""
        queued = self._current is not None
        self._queue.append(request)
        logger.info(
            "Permission presenter show",
            extra={
                "event": "permission_presenter_show",
                "presenter": "flet",
                "request_id": request.id,
                "queued": queued,
                "pending": self.pending_count,
            },
        )
        if queued:
            logger.debug(
                "Permission dialog queued",
                extra={
                    "event": "permission_dialog_queued",
                    "request_id": request.id,
                    "pending": self.pending_count,
                },
            )
            return
        self._show_next()

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
        """Pop this request's dialog and append the outcome notice."""
        current = self._current
        if current is not None and current.id == request_id:
            self._current = None
            dialog = self._dialog
            self._dialog = None
            if dialog is not None:
                self._pop_dialog()
            self._append_notice(decision)
            logger.info(
                "Permission presenter resolve",
                extra={
                    "event": "permission_presenter_resolve",
                    "presenter": "flet",
                    "request_id": request_id,
                    "reply": decision.reply,
                    "was_queued": False,
                },
            )
            self._show_next()
            return

        # The request may have timed out while it was still queued: it must
        # never be shown afterwards, but its outcome still deserves a notice.
        if self._remove_queued(request_id):
            logger.debug(
                "Queued permission resolved before being shown",
                extra={
                    "event": "permission_dialog_resolved_while_queued",
                    "request_id": request_id,
                },
            )
            self._append_notice(decision)
            logger.info(
                "Permission presenter resolve",
                extra={
                    "event": "permission_presenter_resolve",
                    "presenter": "flet",
                    "request_id": request_id,
                    "reply": decision.reply,
                    "was_queued": True,
                },
            )
            return

        logger.debug(
            "Permission resolution ignored (unknown or already resolved)",
            extra={
                "event": "permission_dialog_resolve_ignored",
                "request_id": request_id,
            },
        )

    async def _decide(self, request_id: str, decision: PermissionDecision) -> None:
        """Deliver a button click to the router's ``resolve_from_ui``."""
        try:
            await self._resolve(request_id, decision)
        except Exception:
            logger.debug(
                "Permission decision could not be delivered",
                extra={
                    "event": "permission_dialog_decision_failed",
                    "request_id": request_id,
                },
                exc_info=True,
            )

    def _show_next(self) -> None:
        """Present queued requests until one dialog is actually shown."""
        while self._current is None and self._queue:
            request = self._queue.pop(0)
            if not self._present(request):
                logger.debug(
                    "Permission dialog could not be shown",
                    extra={
                        "event": "permission_dialog_show_failed",
                        "request_id": request.id,
                    },
                )

    def _present(self, request: PermissionRequest) -> bool:
        page = self._page
        if page is None:
            return False

        self._current = request
        self._dialog = build_permission_dialog(request, self._decide)
        try:
            page.show_dialog(self._dialog)
        except Exception:
            self._current = None
            self._dialog = None
            logger.debug(
                "page.show_dialog failed",
                extra={
                    "event": "permission_dialog_show_error",
                    "request_id": request.id,
                },
                exc_info=True,
            )
            return False
        return True

    def _pop_dialog(self) -> None:
        page = self._page
        pop = getattr(page, "pop_dialog", None) if page is not None else None
        if pop is None:
            return
        try:
            pop()
        except Exception:
            logger.debug(
                "page.pop_dialog failed",
                extra={"event": "permission_dialog_pop_error"},
                exc_info=True,
            )

    def _append_notice(self, decision: PermissionDecision) -> None:
        try:
            self._column.controls.append(
                assistant_message(
                    ft.Text(
                        value=result_text(decision),
                        size=DS.typography.small_size,
                        color=DS.colors.text_muted,
                        italic=True,
                    )
                )
            )
        except Exception:
            logger.debug(
                "Permission notice could not be appended",
                extra={"event": "permission_dialog_notice_error"},
                exc_info=True,
            )
            return
        self._update()

    def _update(self) -> None:
        update = getattr(self._page, "update", None) if self._page is not None else None
        if update is None:
            return
        try:
            update()
        except Exception:
            logger.debug(
                "page.update failed after permission notice",
                extra={"event": "permission_dialog_update_error"},
                exc_info=True,
            )

    def _remove_queued(self, request_id: str) -> bool:
        for index, request in enumerate(self._queue):
            if request.id == request_id:
                del self._queue[index]
                return True
        return False
