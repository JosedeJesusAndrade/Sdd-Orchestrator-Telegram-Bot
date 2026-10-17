"""ConversationsService: compose agent history with bot session state.

The agent seam supplies the read-only history (``AgentSessions``); adoption and
activation remain bot state, so they stay in ``SessionService``/``SessionStore``.
This service only merges and delegates — it never talks to OpenCode directly.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent_sessions import (
    AgentSessions,
    ConversationSummary,
    InvalidSessionIdError,
    Message,
    is_valid_real_id,
)
from services.session_service import SessionService
from services.session_store import SessionStore

if TYPE_CHECKING:
    from services.chat_view import ConversationId

_SLUG_MAX_LENGTH = 30


def _slugify(title: str, real_id: str) -> str:
    """Turn a conversation title into a valid bot session name."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = slug[:_SLUG_MAX_LENGTH].rstrip("-")
    return slug or f"ses-{real_id[-6:].lower()}"


def _unique_name(base: str, taken: set[str]) -> str:
    """Return ``base``, or the first free ``base-2``, ``base-3``, ... suffix."""
    if base not in taken:
        return base

    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = f"{base[: _SLUG_MAX_LENGTH - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1


class ConversationsService:
    """Frontend-agnostic operations for the conversations screen."""

    def __init__(
        self,
        agent_sessions: AgentSessions,
        session_service: SessionService,
        session_store: SessionStore,
    ) -> None:
        self._agent_sessions = agent_sessions
        self._session_service = session_service
        self._session_store = session_store

    async def list_conversations(
        self, conversation_id: ConversationId
    ) -> list[ConversationSummary]:
        """List root conversations, annotated with adoption/active state."""
        summaries = await self._agent_sessions.list_sessions()
        stored = await self._session_store.list_sessions(conversation_id)

        active_name = next((s.name for s in stored if s.is_active), None)
        by_real_id = {s.real_id: s for s in stored if s.real_id}

        result: list[ConversationSummary] = []
        for summary in summaries:
            info = by_real_id.get(summary.real_id)
            result.append(
                replace(
                    summary,
                    adopted_name=info.name if info is not None else None,
                    is_active=info is not None and info.name == active_name,
                )
            )
        return result

    async def open(self, conversation_id: ConversationId, real_id: str, title: str) -> str:
        """Activate a conversation, adopting it first when it is new.

        Returns the bot session name that is now active.
        """
        if not is_valid_real_id(real_id):
            raise InvalidSessionIdError(real_id)

        stored = await self._session_store.list_sessions(conversation_id)
        existing = next((s for s in stored if s.real_id == real_id), None)
        if existing is not None:
            await self._session_service.switch(conversation_id, existing.name)
            return existing.name

        name = _unique_name(_slugify(title, real_id), {s.name for s in stored})
        await self._session_service.adopt(conversation_id, real_id, name)
        return name

    async def messages(
        self, real_id: str, limit: int = 50, before: int | None = None
    ) -> list[Message]:
        """Load a page of messages; pure delegation to the agent seam."""
        return await self._agent_sessions.get_messages(real_id, limit=limit, before=before)
