"""Fase A: tests for the shared composition root (``services/composition.py``).

Covers:
  - ``build_prompt_stack()`` returns a ``CoreStack`` with the expected fields
    and types, wired to a ``SessionStore``.
  - The ``"opencode"`` provider registration actually happens — this closes a
    real coverage gap: the registration used to be untested (see
    ``foundation/testing`` #425).
  - ``start_time`` is honored / defaults to ~now.
  - ``services.composition`` is PTB-free (never imports ``telegram``).
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opencode.sessions import OpenCodeSessions
from services.ai_provider_factory import AIProviderFactory
from services.composition import CoreStack, build_prompt_stack
from services.conversations_service import ConversationsService
from services.opencode_cli_backend import OpenCodeCLIBackend
from services.opencode_server import OpenCodeServerClient, OpenCodeServerManager
from services.opencode_server_backend import OpenCodeServerBackend
from services.permission_router import PermissionRouter
from services.permissions import NoOpPermissionPort
from services.prompt_service import PromptService
from services.session_service import SessionService
from services.session_store import SessionStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Shape / typing ────────────────────────────────────────────────────


def test_build_prompt_stack_returns_core_stack(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert isinstance(core, CoreStack)
    assert isinstance(core.session_store, SessionStore)
    assert isinstance(core.provider_factory, AIProviderFactory)
    assert isinstance(core.prompt_service, PromptService)
    assert isinstance(core.session_service, SessionService)
    assert isinstance(core.agent_sessions, OpenCodeSessions)
    assert isinstance(core.conversations_service, ConversationsService)
    assert isinstance(core.start_time, float)


def test_prompt_service_is_wired_to_the_core_pieces(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    # The PromptService must share the SAME store/factory held by CoreStack,
    # so both frontends drive one graph.
    assert core.prompt_service._store is core.session_store
    assert core.prompt_service._factory is core.provider_factory


def test_core_stack_is_frozen(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    with pytest.raises(dataclasses.FrozenInstanceError):
        core.__setattr__("start_time", 0.0)


def test_session_service_gets_the_history_catalog(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    catalog = core.session_service._catalog
    assert isinstance(catalog, OpenCodeSessions)
    assert isinstance(core.agent_sessions, OpenCodeSessions)
    assert catalog is core.agent_sessions


def test_conversations_service_shares_the_core_pieces(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert core.conversations_service._agent_sessions is core.agent_sessions
    assert core.conversations_service._session_service is core.session_service
    assert core.conversations_service._session_store is core.session_store


def test_sessions_directory_is_injected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        sessions_directory=project,
    )

    agent = core.agent_sessions
    assert isinstance(agent, OpenCodeSessions)
    assert agent._directory(None) == project.as_posix()


# ── Provider registration (previously-untested coverage gap) ───────────


def test_opencode_provider_is_registered(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        backend="cli",
    )

    assert core.provider_factory.list_providers() == ["opencode"]
    backend = core.provider_factory.create("opencode")
    assert isinstance(backend, OpenCodeCLIBackend)


def test_default_provider_resolves_to_opencode_backend(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        backend="cli",
    )

    # create() with no provider name must fall back to the default provider,
    # which was registered as OpenCodeCLIBackend.
    assert isinstance(core.provider_factory.create(), OpenCodeCLIBackend)


# ── Transport selection (server vs cli) ───────────────────────────────


def test_server_backend_is_the_default(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert core.backend == "server"
    assert isinstance(core.server_manager, OpenCodeServerManager)
    assert isinstance(core.server_client, OpenCodeServerClient)
    assert isinstance(core.provider_factory.create("opencode"), OpenCodeServerBackend)


def test_server_selection_is_lazy_and_does_not_spawn(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert core.server_manager is not None
    assert core.server_manager.is_owned is False


def test_cli_selection_has_no_server_pieces(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        backend="cli",
    )

    assert core.backend == "cli"
    assert core.server_manager is None
    assert core.server_client is None


def test_injected_server_client_is_wired_into_the_backend(tmp_path: Path) -> None:
    sentinel = object()
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        server_client=sentinel,  # type: ignore[arg-type]
    )

    assert core.server_client is sentinel
    backend = core.provider_factory.create("opencode")
    assert isinstance(backend, OpenCodeServerBackend)
    assert backend._client is sentinel


def test_invalid_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="OPENCODE_BACKEND"):
        build_prompt_stack(
            sessions_path=tmp_path / "sessions.json",
            backend="nope",
        )


# ── Permission router wiring ──────────────────────────────────────────


def test_server_mode_builds_a_permission_router(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        permission_timeout=42,
    )

    router = core.permission_router
    assert isinstance(router, PermissionRouter)
    assert core.permission_port is router
    assert router.pending_count == 0
    assert router._timeout == 42

    backend = core.provider_factory.create("opencode")
    assert isinstance(backend, OpenCodeServerBackend)
    handler = backend._permission_handler
    assert handler is not None
    assert getattr(handler, "__self__", None) is router


def test_injected_permission_port_is_wired_without_a_router(tmp_path: Path) -> None:
    port = NoOpPermissionPort()
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        permission_port=port,
    )

    assert core.permission_port is port
    assert core.permission_router is None

    backend = core.provider_factory.create("opencode")
    assert isinstance(backend, OpenCodeServerBackend)
    handler = backend._permission_handler
    assert handler is not None
    assert getattr(handler, "__self__", None) is port


def test_cli_selection_has_no_permission_router(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        backend="cli",
    )

    assert core.permission_port is None
    assert core.permission_router is None


# ── start_time ownership ──────────────────────────────────────────────


def test_explicit_start_time_is_honored(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json", start_time=123.0)

    assert core.start_time == 123.0


def test_start_time_defaults_to_now(tmp_path: Path) -> None:
    before = time.time()
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")
    after = time.time()

    assert before <= core.start_time <= after


# ── PTB-free guarantee ────────────────────────────────────────────────


def test_composition_module_is_ptb_free() -> None:
    """Importing the composition root in a clean interpreter must not pull in
    ``telegram``. Uses a subprocess because this test session already imports
    PTB (other test modules), which would otherwise pollute ``sys.modules``.
    """
    script = (
        "import services.composition, sys; print('PTB_FREE=' + str('telegram' not in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "PTB_FREE=True" in result.stdout
