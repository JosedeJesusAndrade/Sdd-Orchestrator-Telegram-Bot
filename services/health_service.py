"""HealthService: raw /health data, frontend-agnostic (Fase B).

Moves the ``/health`` computation out of ``handlers/commands.py`` (B3-B7). This
module is PTB-free: the Telegram ``get_me`` connectivity check is INJECTED at
call time through the :class:`ConnectivityProbe` Protocol, which removes the old
layering smell where a service would depend on ``BotPort``. Flet passes no
probe and gets ``connected is None`` (rendered as "n/a").
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from config import BASE_DIR
from persistence.sessions import fetch_opencode_sessions
from services.session_store import SessionStore
from utils.logging import get_module_logger, log_exception

if TYPE_CHECKING:
    from services.chat_view import ConversationId

logger = get_module_logger(__name__)


@runtime_checkable
class ConnectivityProbe(Protocol):
    """Minimal transport probe the health check needs (Telegram ``get_me``)."""

    async def get_me(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HealthSnapshot:
    """Raw health data. ``connected is None`` means "no probe available"."""

    version: str
    uptime_seconds: int
    python: str
    connected: bool | None
    bot_user: str
    oc_session_count: int | None
    workdir: str
    model: str


class HealthService:
    """Compute the data behind /health without any frontend dependency."""

    def __init__(self, session_store: SessionStore, start_time: float) -> None:
        self._store = session_store
        self._start_time = start_time

    async def snapshot(
        self,
        conversation_id: ConversationId,
        *,
        connectivity_probe: ConnectivityProbe | None = None,
    ) -> HealthSnapshot:
        """Return a raw health snapshot for a conversation."""
        version = self._read_version()
        uptime_seconds = int(time.time() - self._start_time)
        python = f"{sys.version_info.major}.{sys.version_info.minor}"

        connected: bool | None
        bot_user = "?"
        if connectivity_probe is None:
            connected = None
        else:
            try:
                me = await connectivity_probe.get_me()
                bot_user = me.get("username", "?")
                connected = True
            except Exception as e:
                log_exception(
                    "health_get_me_failed",
                    module=__name__,
                    level=logging.WARNING,
                    error_type=type(e).__name__,
                )
                bot_user = "?"
                connected = False

        try:
            oc_sessions = await fetch_opencode_sessions()
            oc_count: int | None = len(oc_sessions)
        except Exception:
            log_exception(
                "health_oc_sessions_failed",
                module=__name__,
                level=logging.DEBUG,
            )
            oc_count = None

        settings = await self._store.get_all_chat_settings(conversation_id)
        workdir = settings.get("workdir", "default")
        model = await self._store.get_model(conversation_id)

        return HealthSnapshot(
            version=version,
            uptime_seconds=uptime_seconds,
            python=python,
            connected=connected,
            bot_user=bot_user,
            oc_session_count=oc_count,
            workdir=workdir,
            model=model,
        )

    @staticmethod
    def _read_version() -> str:
        """Read the project version from ``pyproject.toml`` via ``config.BASE_DIR``."""
        try:
            content = (BASE_DIR / "pyproject.toml").read_text(encoding="utf-8")
            match = re.search(r'version\s*=\s*"([^"]+)"', content)
            return match.group(1) if match else "?"
        except Exception:
            log_exception(
                "health_version_read_failed",
                module=__name__,
                level=logging.DEBUG,
            )
            return "?"
