"""StatusService: raw /status data, frontend-agnostic (Fase B).

Moves the ``/status`` computation out of ``handlers/commands.py`` so a second
frontend (Flet) can reuse the SAME data without duplicating logic. This module
is PTB-free and returns a :class:`StatusSnapshot` with raw values (strings,
ints, datetimes); formatting stays in the handler.

Inventory (design `sdd/flet-preparation/design` #426):
  - B1: git-branch lookup (workdir -> ``git rev-parse``).
  - B2: active session / model / timestamps / prompt count / uptime assembly.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from services.session_store import SessionStore
from utils.logging import get_module_logger, log_exception

if TYPE_CHECKING:
    from services.chat_view import ConversationId

logger = get_module_logger(__name__)


@dataclass(frozen=True)
class StatusSnapshot:
    """Raw status data. ``session_name is None`` means "no active session".

    ``created``/``last_used`` are only set when the stored ISO string parses to
    a timezone-aware datetime. ``*_fallback`` carries the raw string when it
    does not, so the Telegram view can keep its historical fallback rendering.
    """

    session_name: str | None
    session_real_id: str | None
    model: str
    branch: str
    created: datetime | None
    last_used: datetime | None
    prompt_count: int
    uptime_seconds: int | None
    created_fallback: str | None = None
    last_used_fallback: str | None = None


class StatusService:
    """Compute the data behind /status without any frontend dependency."""

    def __init__(self, session_store: SessionStore, start_time: float | None = None) -> None:
        self._store = session_store
        self._start_time = start_time

    async def snapshot(self, conversation_id: ConversationId) -> StatusSnapshot:
        """Return a raw status snapshot for a conversation."""
        session = await self._store.get_active_session(conversation_id)
        model = await self._store.get_model(conversation_id)
        settings = await self._store.get_all_chat_settings(conversation_id)
        branch = self._git_branch(settings.get("workdir", ""))
        uptime_seconds = self._uptime_seconds()

        if session is None:
            return StatusSnapshot(
                session_name=None,
                session_real_id=None,
                model=model,
                branch=branch,
                created=None,
                last_used=None,
                prompt_count=0,
                uptime_seconds=uptime_seconds,
            )

        created = self._parse_timestamp(session.created)
        last_used = self._parse_timestamp(session.last_used)

        return StatusSnapshot(
            session_name=session.name,
            session_real_id=session.real_id,
            model=model,
            branch=branch,
            created=created,
            last_used=last_used,
            prompt_count=session.prompt_count,
            uptime_seconds=uptime_seconds,
            created_fallback=(session.created if session.created and created is None else None),
            last_used_fallback=(
                session.last_used if session.last_used and last_used is None else None
            ),
        )

    def _uptime_seconds(self) -> int | None:
        if self._start_time is None:
            return None
        return int(time.time() - self._start_time)

    def _git_branch(self, workdir: str) -> str:
        """Return the current git branch of ``workdir``, or ``"?"``."""
        if not workdir:
            return "?"
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=5,
            )
        except Exception:
            log_exception(
                "git_branch_lookup_failed",
                module=__name__,
                level=logging.DEBUG,
            )
            return "?"
        if branch.returncode == 0:
            return branch.stdout.strip()
        return "?"

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        """Parse an ISO timestamp, returning None when absent or unusable.

        Naive datetimes are deliberately rejected: ``relative_time`` requires a
        timezone-aware value, which is exactly why the old handler rendered the
        raw string instead.
        """
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else None
