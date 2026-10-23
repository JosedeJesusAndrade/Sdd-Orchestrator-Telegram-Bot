"""PermissionRouter — one decision point for every permission request.

Why this exists: the backend receives ``permission.asked`` from the SSE stream
and must block until a human answers — but the answer can come from Telegram,
Flet, or both. The router owns that rendezvous:

  1. Each request gets its own ``Future`` keyed by ``request.id``, so concurrent
     requests never interfere with each other.
  2. The request is broadcast to every presenter with ``gather``; a presenter
     that raises (or a broken frontend) is logged and never prevents the others
     from showing the request.
  3. The first ``resolve_from_ui`` wins. Duplicate button clicks are ignored
     instead of raising ``InvalidStateError``.
  4. On timeout the router fails closed: it returns ``reject`` with
     ``timed_out=True`` and notifies the presenters so the UIs show the notice.
     A timeout of ``0`` (or negative) disables the safety net and waits for the
     human indefinitely. Without any presenter it rejects immediately — the
     agent must never hang.
  5. If the turn is interrupted the backend cancels this task; the router
     notifies the presenters with ``cancelled=True`` and re-raises so every UI
     closes its dialog. When the cancellation carries the backend's budget
     expiry reason, the notice is ``timed_out=True`` instead ("expired").
     When it carries the turn-ended reason (a terminal event arrived while the
     dialog was open), the notice is ``turn_ended=True``: the dialog closes,
     but it is neither an expiry nor an interruption.
  6. The pending entry is always removed in ``finally`` (no leaked futures).

The router only *decides*. The backend sends the decision to the server via
``POST /permission/{id}/reply``; UI adapters stay presentation-only.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any

from services.permissions import (
    PERMISSION_BUDGET_EXPIRED_REASON,
    PERMISSION_TURN_ENDED_REASON,
    PermissionDecision,
    PermissionPresenter,
    PermissionRequest,
)
from utils.logging import get_module_logger

logger = get_module_logger(__name__)

DEFAULT_TIMEOUT = 300.0
TIMEOUT_MESSAGE = "El permiso expiró y fue rechazado automáticamente."
NO_PRESENTER_MESSAGE = "No hay una interfaz disponible para aprobar el permiso."
DUPLICATE_MESSAGE = "Solicitud de permiso duplicada."
CANCELLED_MESSAGE = "El turno se interrumpió antes de resolver el permiso."
TURN_ENDED_MESSAGE = "El turno terminó antes de que respondieras el permiso."

#: Keep a single log line readable: commands/patterns can be arbitrarily long.
_FIELD_LIMIT = 300

#: Never write credentials to the log even when they appear in a tool command.
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b"
    r"(\s*[=:]\s*)((?:bearer|basic|token)\s+)?(\S+)"
)


def _one_line(value: str) -> str:
    """Collapse newlines so a single event stays a single physical log line."""
    return " ".join(value.splitlines())[:_FIELD_LIMIT]


def _compact_patterns(patterns: Sequence[str]) -> str | None:
    if not patterns:
        return None
    return _one_line(", ".join(patterns))


def _safe_command(metadata: Mapping[str, Any]) -> str | None:
    command = metadata.get("command")
    if not isinstance(command, str) or not command:
        return None
    return _one_line(_SECRET_RE.sub(r"\1\2\3***", command))


class PermissionRouter:
    """Implements :class:`~services.permissions.PermissionPort` structurally."""

    def __init__(
        self,
        presenters: Sequence[PermissionPresenter] = (),
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._presenters = list(presenters)
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[PermissionDecision]] = {}

    @property
    def pending_count(self) -> int:
        """Number of requests still waiting for a decision."""
        return len(self._pending)

    def add_presenter(self, presenter: PermissionPresenter) -> None:
        """Register a frontend adapter (Phase 4/5 wiring)."""
        self._presenters.append(presenter)

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        """Ask every presenter and wait for the first answer or the timeout."""
        asked: dict[str, object] = {
            "event": "permission_asked",
            "request_id": request.id,
            "session_id": request.session_id,
            "action": request.permission,
            "presenters": len(self._presenters),
        }
        patterns = _compact_patterns(request.patterns)
        if patterns is not None:
            asked["patterns"] = patterns
        command = _safe_command(request.metadata)
        if command is not None:
            asked["command"] = command
        agent = request.metadata.get("agent")
        if isinstance(agent, str) and agent:
            asked["agent"] = agent
        logger.info("Permission asked", extra=asked)

        if not self._presenters:
            logger.warning(
                "Permission request rejected: no presenter registered",
                extra={
                    "event": "permission_no_presenter",
                    "request_id": request.id,
                    "session_id": request.session_id,
                },
            )
            logger.info(
                "Permission resolved",
                extra={
                    "event": "permission_resolved",
                    "request_id": request.id,
                    "session_id": request.session_id,
                    "reply": "reject",
                    "timed_out": False,
                    "via": "no_presenter",
                },
            )
            return PermissionDecision(reply="reject", message=NO_PRESENTER_MESSAGE)

        if request.id in self._pending:
            # The backend dedups by request id, so this is defensive: never
            # overwrite a live future (that waiter would hang forever).
            logger.warning(
                "Permission request ignored: id already pending",
                extra={
                    "event": "permission_duplicate_request",
                    "request_id": request.id,
                    "session_id": request.session_id,
                },
            )
            return PermissionDecision(reply="reject", message=DUPLICATE_MESSAGE)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionDecision] = loop.create_future()
        self._pending[request.id] = future
        show_task = asyncio.create_task(self._show_all(request))
        try:
            if self._timeout <= 0:
                # 0 = wait for the human with no deadline. The turn is then
                # bounded only by an explicit cancellation.
                return await future
            return await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.CancelledError as exc:
            # The backend's own budget expiring is still a cancellation, but
            # the human actually timed out: carry both flags so the UIs show
            # "expired" instead of "turn interrupted". A terminal event that
            # arrives while the dialog is open is a third case: the turn ended
            # before the human answered, so the notice is its own.
            reasons = exc.args or ()
            expired = PERMISSION_BUDGET_EXPIRED_REASON in reasons
            turn_ended = PERMISSION_TURN_ENDED_REASON in reasons
            if expired:
                message = TIMEOUT_MESSAGE
                event = "permission_budget_expired"
                via = "budget_expired"
                warning = "Permission budget expired; rejecting"
            elif turn_ended:
                message = TURN_ENDED_MESSAGE
                event = "permission_turn_ended"
                via = "turn_ended"
                warning = "Permission request cancelled: the turn already ended"
            else:
                message = CANCELLED_MESSAGE
                event = "permission_cancelled"
                via = "cancel"
                warning = "Permission request cancelled; rejecting"
            decision = PermissionDecision(
                reply="reject",
                message=message,
                timed_out=expired,
                cancelled=True,
                turn_ended=turn_ended,
            )
            logger.warning(
                warning,
                extra={
                    "event": event,
                    "request_id": request.id,
                    "session_id": request.session_id,
                },
            )
            logger.info(
                "Permission resolved",
                extra={
                    "event": "permission_resolved",
                    "request_id": request.id,
                    "session_id": request.session_id,
                    "reply": "reject",
                    "timed_out": expired,
                    "cancelled": True,
                    "turn_ended": turn_ended,
                    "via": via,
                },
            )
            # Tell every UI to close the dialog before propagating the
            # cancellation: otherwise the buttons/dialog outlive the turn.
            await self._resolve_all(request.id, decision)
            raise
        except TimeoutError:
            decision = PermissionDecision(
                reply="reject",
                message=TIMEOUT_MESSAGE,
                timed_out=True,
            )
            logger.warning(
                "Permission request timed out; rejecting",
                extra={
                    "event": "permission_timeout",
                    "request_id": request.id,
                    "session_id": request.session_id,
                    "timeout": self._timeout,
                },
            )
            logger.info(
                "Permission resolved",
                extra={
                    "event": "permission_resolved",
                    "request_id": request.id,
                    "session_id": request.session_id,
                    "reply": "reject",
                    "timed_out": True,
                    "via": "timeout",
                },
            )
            await self._resolve_all(request.id, decision)
            return decision
        finally:
            self._pending.pop(request.id, None)
            if not show_task.done():
                # Do not cancel a half-sent UI message; let the broadcast end
                # on its own (presenters are expected to be quick).
                show_task.add_done_callback(_consume_task_result)

    async def resolve_from_ui(self, request_id: str, decision: PermissionDecision) -> bool:
        """Resolve a pending request. Returns ``False`` if unknown/already resolved.

        After the future is settled, every presenter is notified so each UI can
        render the outcome (edit the Telegram message, pop the Flet dialog)
        without the callback handler knowing which frontend owns the request.
        This is the normal-resolution counterpart of the timeout notice below.
        """
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        self._pending.pop(request_id, None)
        logger.info(
            "Permission resolved from UI",
            extra={
                "event": "permission_resolved",
                "request_id": request_id,
                "reply": decision.reply,
                "timed_out": decision.timed_out,
                "via": "ui",
            },
        )
        await self._resolve_all(request_id, decision)
        return True

    async def _show_all(self, request: PermissionRequest) -> None:
        results = await asyncio.gather(
            *(presenter.show(request) for presenter in self._presenters),
            return_exceptions=True,
        )
        for presenter, result in zip(self._presenters, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Permission presenter failed while showing request",
                    extra={
                        "event": "permission_presenter_show_failed",
                        "request_id": request.id,
                        "presenter": type(presenter).__name__,
                        "error_type": type(result).__name__,
                        "error": str(result)[:200],
                    },
                )

    async def _resolve_all(self, request_id: str, decision: PermissionDecision) -> None:
        results = await asyncio.gather(
            *(presenter.resolve(request_id, decision) for presenter in self._presenters),
            return_exceptions=True,
        )
        for presenter, result in zip(self._presenters, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Permission presenter failed while resolving request",
                    extra={
                        "event": "permission_presenter_resolve_failed",
                        "request_id": request_id,
                        "presenter": type(presenter).__name__,
                        "error_type": type(result).__name__,
                        "error": str(result)[:200],
                    },
                )


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """Retrieve the task result so asyncio never warns about an un awaited task."""
    if task.cancelled():
        return
    task.exception()
