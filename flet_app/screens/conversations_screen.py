"""Conversations screen: read-only history list with loading/empty/error states.

The screen is frontend-only: it owns presentation and delegates every data
operation to ``ConversationsService`` (PTB-free). Tapping a tile opens the
conversation (adopting or switching the active bot session) and loads its
recent messages; the loaded batch is handed to ``on_open`` so the entry point
can render it into the existing chat screen and swap the host back to it.

All I/O is async on Flet's event loop: event handlers only schedule coroutines
with ``page.run_task`` — never ``run_thread``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import flet as ft

from flet_app.core.design_system import DS
from services.agent_sessions import ConversationSummary, Message
from services.conversations_service import ConversationsService

_HISTORY_LIMIT = 50


def relative_time(timestamp_ms: int, *, now_ms: int | None = None) -> str:
    """Format an epoch-ms timestamp as a short Spanish relative label."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    delta = now - timestamp_ms
    if delta < 60_000:
        return "ahora"

    minutes = delta // 60_000
    if minutes < 60:
        return f"hace {minutes} min"

    hours = minutes // 60
    if hours < 24:
        return f"hace {hours} h"

    days = hours // 24
    if days < 7:
        return f"hace {days} d"

    return time.strftime("%d/%m/%Y", time.localtime(timestamp_ms / 1000))


def message_count_label(count: int) -> str:
    """Human label for the stored message count (includes non-text turns)."""
    return "1 mensaje" if count == 1 else f"{count} mensajes"


class ConversationsScreen:
    """The conversations list plus the handles the entry point drives."""

    def __init__(
        self,
        page: ft.Page,
        service: ConversationsService,
        conversation_id: str,
        *,
        on_back: Callable[[], None] | None = None,
        on_open: Callable[[ConversationSummary, list[Message], str], None] | None = None,
    ) -> None:
        self._page = page
        self._service = service
        self._conversation_id = conversation_id
        self.on_back = on_back
        self.on_open = on_open

        self.state = "idle"
        self.items: list[ConversationSummary] = []
        self.tiles: list[ft.Control] = []
        self.list_view: ft.ListView | None = None

        self._loading = self._build_loading()
        self._empty = self._build_empty()
        self.state_body = ft.Container(
            content=self._loading,
            expand=True,
            alignment=ft.Alignment.CENTER,
        )
        self.control = self._build_control()

    # ------------------------------------------------------------------ build
    def _build_control(self) -> ft.Container:
        header = ft.Row(
            controls=[
                ft.IconButton(
                    icon=ft.Icons.ARROW_BACK,
                    icon_color=DS.colors.text_primary,
                    icon_size=22,
                    tooltip="Volver al chat",
                    disabled=self.on_back is None,
                    on_click=self._on_back_click,
                ),
                ft.Text(
                    value="Conversaciones",
                    size=DS.typography.title_size,
                    weight=DS.typography.title_weight,
                    color=DS.colors.text_primary,
                ),
                ft.Container(expand=True),
                ft.IconButton(
                    icon=ft.Icons.REFRESH,
                    icon_color=DS.colors.text_primary,
                    icon_size=20,
                    tooltip="Actualizar",
                    on_click=self._on_refresh_click,
                ),
            ],
            spacing=DS.spacing.sm,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        header_holder = ft.Container(
            content=header,
            padding=ft.Padding.only(
                left=DS.spacing.md,
                right=DS.spacing.xl,
                top=DS.spacing.md + DS.spacing.xs // 4,
                bottom=DS.spacing.md,
            ),
        )

        body = ft.Column(
            controls=[header_holder, self.state_body],
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        return ft.Container(
            expand=True,
            gradient=DS.gradients.background(),
            content=ft.Stack(
                expand=True,
                fit=ft.StackFit.EXPAND,
                controls=[
                    ft.Container(
                        width=520,
                        height=520,
                        top=-200,
                        right=-180,
                        border_radius=260,
                        gradient=DS.gradients.lavender_glow(),
                        ignore_interactions=True,
                    ),
                    ft.Container(
                        width=420,
                        height=420,
                        bottom=-220,
                        left=-140,
                        border_radius=210,
                        gradient=DS.gradients.blue_glow(),
                        ignore_interactions=True,
                    ),
                    ft.SafeArea(
                        content=body,
                        expand=True,
                        avoid_intrusions_top=True,
                        avoid_intrusions_bottom=True,
                        avoid_intrusions_left=False,
                        avoid_intrusions_right=False,
                    ),
                ],
            ),
        )

    def _build_loading(self) -> ft.Container:
        return ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                controls=[
                    ft.ProgressRing(width=28, height=28, color=DS.colors.accent),
                    ft.Text(
                        value="Cargando conversaciones…",
                        size=DS.typography.body_size,
                        color=DS.colors.text_secondary,
                    ),
                ],
                spacing=DS.spacing.md,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

    def _build_empty(self) -> ft.Container:
        return ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                value="No hay conversaciones todavía.",
                size=DS.typography.body_size,
                color=DS.colors.text_secondary,
            ),
        )

    def _build_tile(self, summary: ConversationSummary) -> ft.Container:
        title = summary.title.strip() or "(sin título)"
        title_text = ft.Text(
            value=title,
            size=DS.typography.title_size,
            weight=DS.typography.title_weight,
            color=DS.colors.text_primary,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        meta_text = ft.Text(
            value=(
                f"{relative_time(summary.updated_at)} · "
                f"{message_count_label(summary.message_count)}"
            ),
            size=DS.typography.small_size,
            color=DS.colors.text_secondary,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

        trailing: list[ft.Control] = []
        if summary.is_active:
            trailing.append(
                ft.Container(
                    width=8,
                    height=8,
                    border_radius=4,
                    bgcolor=DS.colors.accent,
                    tooltip="Conversación activa",
                )
            )
        if summary.adopted_name:
            trailing.append(
                ft.Container(
                    content=ft.Text(
                        value=summary.adopted_name,
                        size=DS.typography.muted_size,
                        color=DS.colors.accent_soft,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    padding=ft.Padding.symmetric(
                        horizontal=DS.spacing.sm,
                        vertical=DS.spacing.xs,
                    ),
                    border_radius=DS.radii.pill,
                    bgcolor=DS.colors.alpha(DS.colors.accent_soft, 0.14),
                )
            )

        def handle_tap(
            _: ft.Event[ft.Container],
            item: ConversationSummary = summary,
        ) -> None:
            self._page.run_task(self.open, item)

        tile = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[title_text, meta_text],
                        spacing=DS.spacing.xs,
                        expand=True,
                    ),
                    ft.Row(
                        controls=trailing,
                        spacing=DS.spacing.sm,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=DS.spacing.md,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(
                horizontal=DS.spacing.lg,
                vertical=DS.spacing.md,
            ),
            bgcolor=DS.colors.surface,
            border=ft.Border.all(1, DS.colors.outline),
            border_radius=DS.radii.card,
            ink=True,
            on_click=handle_tap,
            data=summary,
        )
        return tile

    # ------------------------------------------------------------------ state
    def set_loading(self) -> None:
        self.state = "loading"
        self.state_body.content = self._loading
        self.state_body.alignment = ft.Alignment.CENTER

    def set_items(self, items: list[ConversationSummary]) -> None:
        if not items:
            self.set_empty()
            return

        self.state = "list"
        self.items = list(items)
        self.tiles = [self._build_tile(item) for item in self.items]
        self.list_view = ft.ListView(
            controls=self.tiles,
            spacing=DS.spacing.sm,
            padding=ft.Padding.only(
                left=DS.spacing.xl,
                right=DS.spacing.xl,
                bottom=DS.spacing.xl,
            ),
            expand=True,
        )
        self.state_body.alignment = ft.Alignment.TOP_LEFT
        self.state_body.content = self.list_view

    def set_empty(self) -> None:
        self.state = "empty"
        self.items = []
        self.tiles = []
        self.list_view = None
        self.state_body.alignment = ft.Alignment.CENTER
        self.state_body.content = self._empty

    def set_error(self, message: str) -> None:
        self.state = "error"
        self.items = []
        self.tiles = []
        self.list_view = None

        details = message.strip() or "Error desconocido."
        self.state_body.alignment = ft.Alignment.CENTER
        self.state_body.content = ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                controls=[
                    ft.Text(
                        value="No se pudieron cargar las conversaciones.",
                        size=DS.typography.body_size,
                        weight=DS.typography.title_weight,
                        color=DS.colors.text_primary,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        value=details,
                        size=DS.typography.small_size,
                        color=DS.colors.text_secondary,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.FilledButton(
                        content="Reintentar",
                        icon=ft.Icons.REFRESH,
                        on_click=self._on_retry_click,
                    ),
                ],
                spacing=DS.spacing.md,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

    # ------------------------------------------------------------------- I/O
    async def load(self) -> None:
        """Show loading, then render the list, the empty state or the error."""
        self.set_loading()
        self._page.update()
        try:
            items = await self._service.list_conversations(self._conversation_id)
        except Exception as exc:
            self.set_error(str(exc))
        else:
            self.set_items(items)
        self._page.update()

    async def open(self, summary: ConversationSummary) -> tuple[list[Message], str] | None:
        """Adopt/switch the conversation and load its recent messages.

        Returns ``None`` when the operation failed; the error state is already
        rendered so the user can retry from the screen.
        """
        try:
            active_name = await self._service.open(
                self._conversation_id,
                summary.real_id,
                summary.title,
            )
            messages = await self._service.messages(
                summary.real_id,
                limit=_HISTORY_LIMIT,
            )
        except Exception as exc:
            self.set_error(str(exc))
            self._page.update()
            return None

        if self.on_open is not None:
            self.on_open(summary, messages, active_name)
        return messages, active_name

    # -------------------------------------------------------------- handlers
    def _on_back_click(self, _: ft.Event[ft.IconButton]) -> None:
        if self.on_back is not None:
            self.on_back()

    def _on_refresh_click(self, _: ft.Event[ft.IconButton]) -> None:
        self._page.run_task(self.load)

    def _on_retry_click(self, _: ft.Event[ft.Button]) -> None:
        self._page.run_task(self.load)
