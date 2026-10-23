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
    BASE_DIR,
    OPENCODE_BACKEND,
    OPENCODE_CMD,
    OPENCODE_PERMISSION_TIMEOUT,
    OPENCODE_SERVER_ALLOW_REMOTE,
    OPENCODE_SERVER_BINARY,
    OPENCODE_SERVER_HOST,
    OPENCODE_SERVER_PASSWORD,
    OPENCODE_SERVER_PORT,
    OPENCODE_TIMEOUT,
    OPENCODE_WORKDIR,
    SESSIONS_PATH,
)
from opencode.sessions import OpenCodeSessions
from services.agent_sessions import AgentSessions
from services.ai_provider_factory import AIProviderFactory
from services.conversations_service import ConversationsService
from services.health_service import HealthService
from services.opencode_cli_backend import OpenCodeCLIBackend
from services.opencode_server import (
    OpenCodeServerClient,
    OpenCodeServerManager,
    ServerConfig,
)
from services.opencode_server_backend import OpenCodeServerBackend
from services.permission_router import PermissionRouter
from services.permissions import PermissionPort
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
    agent_sessions: AgentSessions
    conversations_service: ConversationsService
    start_time: float
    backend: str = OPENCODE_BACKEND
    permission_timeout: int = OPENCODE_PERMISSION_TIMEOUT
    # Server-mode only; all stay ``None`` with ``OPENCODE_BACKEND=cli``.
    server_manager: OpenCodeServerManager | None = None
    server_client: OpenCodeServerClient | None = None
    permission_port: PermissionPort | None = None
    #: The concrete router when the active port is a ``PermissionRouter``
    #: (Phase 4/5 register their presenters on it).
    permission_router: PermissionRouter | None = None


def build_prompt_stack(
    *,
    sessions_path: str | Path = SESSIONS_PATH,
    default_provider: str = "opencode",
    opencode_cmd: str = OPENCODE_CMD,
    workdir: str = OPENCODE_WORKDIR,
    timeout: int = OPENCODE_TIMEOUT,
    sessions_directory: str | Path = BASE_DIR,
    start_time: float | None = None,
    backend: str = OPENCODE_BACKEND,
    server_host: str = OPENCODE_SERVER_HOST,
    server_port: int = OPENCODE_SERVER_PORT,
    server_password: str = OPENCODE_SERVER_PASSWORD,
    server_binary: str = OPENCODE_SERVER_BINARY,
    server_allow_remote: bool = OPENCODE_SERVER_ALLOW_REMOTE,
    permission_timeout: int = OPENCODE_PERMISSION_TIMEOUT,
    permission_port: PermissionPort | None = None,
    server_client: OpenCodeServerClient | None = None,
    server_manager: OpenCodeServerManager | None = None,
) -> CoreStack:
    """Build the frontend-agnostic core graph.

    Keyword-only, with defaults taken from ``config`` so it is callable as a
    bare ``build_prompt_stack()`` yet unit-testable by injecting fakes.

    Registers the ``"opencode"`` provider (moved out of ``bot.py``) and owns
    ``start_time`` — both frontends therefore share uptime semantics.

    ``backend`` selects the transport:
      - ``"server"`` (default): ``opencode serve`` over HTTP with SSE.
      - ``"cli"``: the one-shot ``opencode run`` subprocess (rollback path).

    Server construction is LAZY: creating the manager/client opens no socket
    and spawns no process. ``OpenCodeServerBackend`` calls
    ``manager.ensure_running()`` on its first ``execute``.

    ``permission_port`` defaults to a ``PermissionRouter`` with no presenters
    (Phase 3: fail closed). Phase 4/5 register their UI adapters on
    ``CoreStack.permission_router``.
    """
    selected_backend = backend.strip().lower()
    if selected_backend not in {"server", "cli"}:
        raise ValueError(
            f"OPENCODE_BACKEND inválido: {backend!r}. Valores permitidos: 'server', 'cli'."
        )

    session_store = SessionStore(Path(sessions_path))
    resolved_start_time = time.time() if start_time is None else start_time

    provider_factory = AIProviderFactory(default_provider=default_provider)
    manager: OpenCodeServerManager | None = None
    client: OpenCodeServerClient | None = None
    resolved_port: PermissionPort | None = None
    router: PermissionRouter | None = None

    if selected_backend == "server":
        manager = server_manager or OpenCodeServerManager(
            ServerConfig(
                host=server_host,
                port=server_port,
                password=server_password,
                binary=server_binary,
                directory=workdir,
                allow_remote=server_allow_remote,
            )
        )
        client = server_client or OpenCodeServerClient(
            manager.base_url,
            server_password,
            workdir,
        )
        resolved_port = (
            permission_port
            if permission_port is not None
            else PermissionRouter(presenters=[], timeout=permission_timeout)
        )
        if isinstance(resolved_port, PermissionRouter):
            router = resolved_port
        provider_factory.register(
            "opencode",
            OpenCodeServerBackend,
            client=client,
            # The backend only needs the callable; pass the port's bound
            # method so any PermissionPort implementation can be injected.
            permission_handler=resolved_port.request_permission,
            timeout=timeout,
            permission_timeout=permission_timeout,
            manager=manager,
        )
    else:
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

    agent_sessions = OpenCodeSessions(sessions_directory)
    session_service = SessionService(session_store, catalog=agent_sessions)
    conversations_service = ConversationsService(
        agent_sessions,
        session_service,
        session_store,
    )

    return CoreStack(
        session_store=session_store,
        provider_factory=provider_factory,
        prompt_service=prompt_service,
        status_service=StatusService(session_store, resolved_start_time),
        health_service=HealthService(session_store, resolved_start_time),
        session_service=session_service,
        settings_service=SettingsService(session_store),
        agent_sessions=agent_sessions,
        conversations_service=conversations_service,
        start_time=resolved_start_time,
        backend=selected_backend,
        permission_timeout=permission_timeout,
        server_manager=manager,
        server_client=client,
        permission_port=resolved_port,
        permission_router=router,
    )
