"""Shared permission data model and ports (transport-agnostic).

The HTTP client, the backend, and every UI adapter share these types. Keeping
them here (instead of in the HTTP client) guarantees the Telegram and Flet
presenters answer the exact request shape the client produces, and lets the
router stay free of PTB/Flet imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Reply = Literal["once", "always", "reject"]

#: Cancellation message the backend attaches to ``Task.cancel()`` when its own
#: permission budget expires. The router maps it to ``timed_out=True`` so UIs
#: say "expired" instead of "turn interrupted" (defense in depth only: the
#: router's shorter timeout normally settles the request first).
PERMISSION_BUDGET_EXPIRED_REASON = "permission_budget_expired"

#: Cancellation message the backend attaches to ``Task.cancel()`` when the
#: assistant turn reaches a terminal event while the request is still pending.
#: The router maps it to ``turn_ended=True`` so UIs close the dialog with
#: "the turn ended before you answered", distinct from an expired budget or an
#: explicit interruption.
PERMISSION_TURN_ENDED_REASON = "permission_turn_ended"

#: Visible notice for a permission reply the server NEVER accepted. The agent
#: may keep waiting until its own timeout, so a silent "✅" would be a lie.
PERMISSION_REPLY_NOT_ACCEPTED_NOTICE = (
    "⚠️ Tu decisión de permiso no se pudo aplicar en el servidor. "
    "El agente pudo haber seguido esperando."
)


@dataclass(frozen=True)
class PermissionRequest:
    """A pending permission request emitted by the OpenCode server."""

    id: str
    session_id: str
    permission: str
    patterns: tuple[str, ...]
    metadata: Mapping[str, Any]
    always: tuple[str, ...]
    message_id: str | None
    call_id: str | None


@dataclass(frozen=True)
class PermissionDecision:
    """The answer to a :class:`PermissionRequest`.

    ``timed_out`` lets presenters show "rechazado automáticamente" without
    inspecting the message text. ``cancelled`` means the whole turn was
    interrupted while the dialog was still open, so presenters can close it
    instead of leaving the buttons/dialog dangling. Both flags are set when the
    backend's own budget expires (cancelled by us, but the human timed out):
    presenters must render that as "expired", not as an interrupted turn.

    ``turn_ended`` is the third case: the assistant turn reached a terminal
    event before the human answered. It is also a cancellation (the dialog must
    close), but the notice is its own: not "expired" and not "interrupted".
    """

    reply: Reply
    message: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    turn_ended: bool = False


class PermissionPort(Protocol):
    """Decision seam between the backend and whatever UI answers the request."""

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision: ...


class PermissionPresenter(Protocol):
    """A frontend adapter (Telegram, Flet) that renders and resolves requests."""

    async def show(self, request: PermissionRequest) -> None: ...

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None: ...


class NoOpPermissionPort:
    """Test-only port that approves immediately without any UI."""

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(reply="once")
