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

from services.ai_provider_factory import AIProviderFactory
from services.composition import CoreStack, build_prompt_stack
from services.opencode_cli_backend import OpenCodeCLIBackend
from services.prompt_service import PromptService
from services.session_store import SessionStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Shape / typing ────────────────────────────────────────────────────


def test_build_prompt_stack_returns_core_stack(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert isinstance(core, CoreStack)
    assert isinstance(core.session_store, SessionStore)
    assert isinstance(core.provider_factory, AIProviderFactory)
    assert isinstance(core.prompt_service, PromptService)
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


# ── Provider registration (previously-untested coverage gap) ───────────


def test_opencode_provider_is_registered(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    assert core.provider_factory.list_providers() == ["opencode"]
    backend = core.provider_factory.create("opencode")
    assert isinstance(backend, OpenCodeCLIBackend)


def test_default_provider_resolves_to_opencode_backend(tmp_path: Path) -> None:
    core = build_prompt_stack(sessions_path=tmp_path / "sessions.json")

    # create() with no provider name must fall back to the default provider,
    # which was registered as OpenCodeCLIBackend.
    assert isinstance(core.provider_factory.create(), OpenCodeCLIBackend)


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
