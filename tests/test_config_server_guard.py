"""Loopback guard for the OpenCode server (finding #5).

The bot is the ONLY client of the agent, so binding ``opencode serve`` to a
non-loopback host must fail fast unless ``OPENCODE_SERVER_ALLOW_REMOTE`` is
explicitly enabled. The validation lives on the startup path
(``ServerConfig.__post_init__``), never in ``config.py`` at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.composition import build_prompt_stack
from services.opencode_server.errors import OpenCodeServerConfigError
from services.opencode_server.manager import ServerConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Startup guard ─────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST", " LocalHost "])
def test_loopback_hosts_are_accepted(host: str) -> None:
    config = ServerConfig(host=host)

    assert config.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(OpenCodeServerConfigError) as exc_info:
        ServerConfig(host=host)

    message = str(exc_info.value)
    assert "loopback" in message
    assert "OPENCODE_SERVER_ALLOW_REMOTE=1" in message
    # WHY: the bot is the only client — the message must say so.
    assert "ÚNICO cliente" in message


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_allow_remote_opt_in_accepts_non_loopback(host: str) -> None:
    config = ServerConfig(host=host, allow_remote=True)

    assert config.host == host


def test_allow_remote_defaults_to_false() -> None:
    assert ServerConfig(host="127.0.0.1").allow_remote is False


# ── Composition wiring ────────────────────────────────────────────────


def test_build_prompt_stack_rejects_non_loopback(tmp_path: Path) -> None:
    with pytest.raises(OpenCodeServerConfigError):
        build_prompt_stack(
            sessions_path=tmp_path / "sessions.json",
            server_host="0.0.0.0",
        )


def test_build_prompt_stack_passes_the_allow_remote_flag(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        server_host="0.0.0.0",
        server_allow_remote=True,
    )

    assert core.server_manager is not None
    assert core.server_manager.base_url == "http://0.0.0.0:4096"


def test_cli_mode_ignores_the_host_guard(tmp_path: Path) -> None:
    core = build_prompt_stack(
        sessions_path=tmp_path / "sessions.json",
        backend="cli",
        server_host="0.0.0.0",
    )

    assert core.server_manager is None


# ── No import-time side effects ───────────────────────────────────────


def _import_config(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ALLOWED_CHAT_IDS": "111",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.OPENCODE_SERVER_ALLOW_REMOTE)"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("raw", ["1", "true", "yes", "TRUE", " Yes "])
def test_allow_remote_env_values_enable_the_opt_in(raw: str) -> None:
    result = _import_config({"OPENCODE_SERVER_ALLOW_REMOTE": raw})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize("raw", ["", "0", "false", "no"])
def test_other_allow_remote_env_values_disable_the_opt_in(raw: str) -> None:
    result = _import_config({"OPENCODE_SERVER_ALLOW_REMOTE": raw})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_importing_config_with_a_non_loopback_host_does_not_fail() -> None:
    """The guard must run at startup, not at import (no config side effects)."""
    result = _import_config({"OPENCODE_SERVER_HOST": "0.0.0.0", "OPENCODE_SERVER_ALLOW_REMOTE": ""})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
