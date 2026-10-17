"""Composition root — the frontend-agnostic core object graph.

Both entry points build the SAME core by calling :func:`build_prompt_stack`:
  - the Telegram entry point (``bot.py::run_bot``), and
  - the future Flet entry point.

This module is PTB-free on purpose: it must never import ``telegram`` (nor
``flet``). The Telegram adapters (``TelegramAdapter``, ``MessageSender``,
``AppContainer``) stay in ``bot.py``; the Flet adapters will live in its own
package. See design `sdd/flet-preparation/design` (#426) for the boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from config import (
    OPENCODE_CMD,
    OPENCODE_TIMEOUT,
    OPENCODE_WORKDIR,
    SESSIONS_PATH,
)
from services.ai_provider_factory import AIProviderFactory
from services.health_service import HealthService
from services.opencode_cli_backend import OpenCodeCLIBackend
from services.prompt_service import PromptService
from services.session_service import SessionService
from services.session_store import SessionStore
from services.settings_service import SettingsService
from services.status_service import StatusService


@dataclass(frozen=True)
class CoreStack:
    """The shared core graph both frontends depend on.

    The Telegram-only payload (``MessageSender``/``AppContainer``/``BotPort``)
    never enters here; the four ``*_service`` fields are PTB-free.
    """

    session_store: SessionStore
    provider_factory: AIProviderFactory
    prompt_service: PromptService
    status_service: StatusService
    health_service: HealthService
    session_service: SessionService
    settings_service: SettingsService
    start_time: float


def build_prompt_stack(
    *,
    sessions_path: str | Path = SESSIONS_PATH,
    default_provider: str = "opencode",
    opencode_cmd: str = OPENCODE_CMD,
    workdir: str = OPENCODE_WORKDIR,
    timeout: int = OPENCODE_TIMEOUT,
    start_time: float | None = None,
) -> CoreStack:
    """Build the frontend-agnostic core graph.

    Keyword-only, with defaults taken from ``config`` so it is callable as a
    bare ``build_prompt_stack()`` yet unit-testable by injecting fakes.

    Registers the ``"opencode"`` provider (moved out of ``bot.py``) and owns
    ``start_time`` — both frontends therefore share uptime semantics.
    """
    session_store = SessionStore(Path(sessions_path))
    resolved_start_time = time.time() if start_time is None else start_time

    provider_factory = AIProviderFactory(default_provider=default_provider)
    provider_factory.register(
        "opencode",
        OpenCodeCLIBackend,
        opencode_cmd=opencode_cmd,
        workdir=workdir,
        timeout=timeout,
    )

    prompt_service = PromptService(
        session_store=session_store,
        provider_factory=provider_factory,
    )

    return CoreStack(
        session_store=session_store,
        provider_factory=provider_factory,
        prompt_service=prompt_service,
        status_service=StatusService(session_store, resolved_start_time),
        health_service=HealthService(session_store, resolved_start_time),
        session_service=SessionService(session_store),
        settings_service=SettingsService(session_store),
        start_time=resolved_start_time,
    )
