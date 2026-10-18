"""Shared permission data model (transport-agnostic).

Phase 1 only needs the request shape returned by ``GET /permission``; the
PermissionPort / PermissionRouter contract lands in Phase 3. Keeping the
dataclass here (instead of in the HTTP client) guarantees the Telegram and
Flet presenters will share the exact same type the client produces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

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
