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
    inspecting the message text.
    """

    reply: Reply
    message: str | None = None
    timed_out: bool = False


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
