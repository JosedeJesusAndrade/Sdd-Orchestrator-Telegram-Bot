"""Agent-agnostic, read-only session history seam.

This module is deliberately PTB-free and stdlib-only so any frontend (Telegram,
Flet, or a future one) can consume session history without importing a UI
framework. Implementations (e.g. ``opencode.sessions.OpenCodeSessions``) provide
the concrete data source; ``ConversationsService`` composes them with bot state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

REAL_ID_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")


def is_valid_real_id(value: str) -> bool:
    """Return whether ``value`` is a syntactically valid OpenCode session id."""
    return REAL_ID_PATTERN.fullmatch(value) is not None


class InvalidSessionIdError(ValueError):
    """Raised when a session id fails the ``ses_...`` validation rule."""

    def __init__(self, real_id: str) -> None:
        self.real_id = real_id
        super().__init__(f"Invalid OpenCode session id: {real_id!r}")


@dataclass(frozen=True)
class MessagePart:
    """A single message part as stored by the agent."""

    type: str
    text: str


@dataclass(frozen=True)
class Message:
    """A rendered message: its text parts joined for display."""

    id: str
    role: str
    text: str
    timestamp: int
    parts: tuple[MessagePart, ...] = ()


@dataclass(frozen=True)
class ConversationSummary:
    """A root conversation, optionally annotated with bot adoption state.

    The agent implementation fills the first seven fields. ``ConversationsService``
    annotates ``is_active`` / ``adopted_name`` with ``dataclasses.replace``.
    """

    real_id: str
    title: str
    created_at: int
    updated_at: int
    message_count: int
    directory: str
    agent: str
    is_active: bool = False
    adopted_name: str | None = None


@runtime_checkable
class AgentSessions(Protocol):
    """Read-only history source for a CLI agent."""

    async def list_sessions(
        self, directory: str | None = None, limit: int = 50
    ) -> list[ConversationSummary]: ...

    async def get_messages(
        self, real_id: str, limit: int = 50, before: int | None = None
    ) -> list[Message]: ...


@runtime_checkable
class SessionCatalog(Protocol):
    """Exact-by-id catalog lookup; validates adoption beyond the CLI's 15."""

    async def get_session(self, real_id: str) -> ConversationSummary | None: ...
