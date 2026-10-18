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
     Without any presenter it rejects immediately — the agent must never hang.
  5. The pending entry is always removed in ``finally`` (no leaked futures).

The router only *decides*. The backend sends the decision to the server via
``POST /permission/{id}/reply``; UI adapters stay presentation-only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from services.permissions import (
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
        if not self._presenters:
            logger.warning(
                "Permission request rejected: no presenter registered",
                extra={
                    "event": "permission_no_presenter",
                    "request_id": request.id,
                    "session_id": request.session_id,
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
            return await asyncio.wait_for(future, timeout=self._timeout)
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
            await self._resolve_all(request.id, decision)
            return decision
        finally:
            self._pending.pop(request.id, None)
            if not show_task.done():
                # Do not cancel a half-sent UI message; let the broadcast end
                # on its own (presenters are expected to be quick).
                show_task.add_done_callback(_consume_task_result)

    async def resolve_from_ui(self, request_id: str, decision: PermissionDecision) -> bool:
        """Resolve a pending request. Returns ``False`` if unknown/already resolved."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        self._pending.pop(request_id, None)
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
