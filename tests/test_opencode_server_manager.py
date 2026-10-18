"""Tests for the OpenCode server lifecycle manager (no live server)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import services.opencode_server.manager as manager_module
from services.opencode_server.errors import (
    OpenCodeServerConfigError,
    OpenCodeUnavailableError,
)
from services.opencode_server.manager import OpenCodeServerManager, ServerConfig


class _FakeStream:
    async def read(self, _size: int = -1) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self, *, pid: int = 4242, wait_timeout: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self._wait_timeout = wait_timeout
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        self.returncode = 0
        if self._wait_timeout:
            raise TimeoutError
        return 0


class _KillProcess:
    returncode = 0

    async def wait(self) -> int:
        return 0


def _manager(**overrides: Any) -> OpenCodeServerManager:
    return OpenCodeServerManager(ServerConfig(**overrides))


def _install_spawn(monkeypatch: pytest.MonkeyPatch, process: object) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_ensure_running_adopts_healthy_server(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret")

    async def healthy() -> bool:
        return True

    monkeypatch.setattr(manager, "health", healthy)
    calls = _install_spawn(monkeypatch, _FakeProcess())

    await manager.ensure_running()

    assert calls == []
    assert manager.is_owned is False


async def test_ensure_running_starts_when_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret")
    probes = [False, True]

    async def health() -> bool:
        return probes.pop(0)

    started: list[bool] = []

    async def start(*, directory: object = None) -> None:
        started.append(True)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(manager, "health", health)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await manager.ensure_running()

    assert started == [True]


async def test_ensure_running_rejects_empty_password(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="")

    async def explode() -> bool:
        raise AssertionError("health must not be probed without a password")

    monkeypatch.setattr(manager, "health", explode)
    calls = _install_spawn(monkeypatch, _FakeProcess())

    with pytest.raises(OpenCodeServerConfigError):
        await manager.ensure_running()

    assert calls == []


async def test_ensure_running_times_out_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret", timeout=0)

    async def unhealthy() -> bool:
        return False

    stopped: list[bool] = []

    async def start(*, directory: object = None) -> None:
        return None

    async def stop() -> None:
        stopped.append(True)

    monkeypatch.setattr(manager, "health", unhealthy)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "stop", stop)

    with pytest.raises(OpenCodeUnavailableError):
        await manager.ensure_running()

    assert stopped == [True]


async def test_start_wraps_cmd_shim_with_cmd_c(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "IS_WINDOWS", True)
    manager = _manager(
        password="secret",
        binary=r"C:\npm\opencode.cmd",
        host="127.0.0.1",
        port=4096,
        directory=Path("C:/project"),
    )
    calls = _install_spawn(monkeypatch, _FakeProcess())

    await manager.start()
    await asyncio.sleep(0)

    assert calls, "expected a subprocess spawn"
    assert calls[0][:3] == ("cmd", "/c", r"C:\npm\opencode.cmd")
    assert "--port" in calls[0]
    assert "4096" in calls[0]


async def test_start_passes_password_env_and_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "IS_WINDOWS", False)
    manager = _manager(
        password="super-secret",
        binary="opencode",
        directory=Path("C:/project"),
    )
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await manager.start()
    await asyncio.sleep(0)

    assert captured["args"] == (
        "opencode",
        "serve",
        "--port",
        "4096",
        "--hostname",
        "127.0.0.1",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["OPENCODE_SERVER_PASSWORD"] == "super-secret"
    assert kwargs["cwd"] == str(Path("C:/project"))
    assert manager.is_owned is True


async def test_start_is_idempotent_while_process_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret")
    manager._process = _FakeProcess()  # type: ignore[assignment]
    calls = _install_spawn(monkeypatch, _FakeProcess())

    await manager.start()

    assert calls == []


async def test_start_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="")
    calls = _install_spawn(monkeypatch, _FakeProcess())

    with pytest.raises(OpenCodeServerConfigError):
        await manager.start()

    assert calls == []


async def test_stop_is_noop_when_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret")
    process = _FakeProcess()
    manager._process = process  # type: ignore[assignment]
    manager._owned = False
    calls = _install_spawn(monkeypatch, _KillProcess())

    await manager.stop()

    assert process.terminated is False
    assert calls == []


async def test_stop_terminates_owned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "IS_WINDOWS", False)
    manager = _manager(password="secret")
    process = _FakeProcess()
    manager._process = process  # type: ignore[assignment]
    manager._owned = True

    async def fake_exec(*args: object, **kwargs: object) -> _KillProcess:
        return _KillProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await manager.stop()

    assert process.terminated is True
    assert manager.is_owned is False


async def test_stop_kills_the_tree_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: terminate() on the .cmd shim orphans the real server.

    The live probe showed the child keeping the port bound after the wrapper
    was terminated, so shutdown must taskkill the tree up front.
    """
    monkeypatch.setattr(manager_module, "IS_WINDOWS", True)
    manager = _manager(password="secret")
    process = _FakeProcess(pid=777)
    manager._process = process  # type: ignore[assignment]
    manager._owned = True
    calls = _install_spawn(monkeypatch, _KillProcess())

    await manager.stop()

    assert process.terminated is False
    assert calls == [("taskkill", "/F", "/T", "/PID", "777")]
    assert manager.is_owned is False


async def test_stop_survives_wait_timeout_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "IS_WINDOWS", True)
    manager = _manager(password="secret")
    process = _FakeProcess(pid=778, wait_timeout=True)
    manager._process = process  # type: ignore[assignment]
    manager._owned = True
    calls = _install_spawn(monkeypatch, _KillProcess())

    await manager.stop()

    assert calls == [("taskkill", "/F", "/T", "/PID", "778")]
    assert manager._process is None
    assert manager.is_owned is False


async def test_restart_stops_then_ensures_running(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(password="secret")
    order: list[str] = []

    async def stop() -> None:
        order.append("stop")

    async def ensure_running() -> None:
        order.append("ensure_running")

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "ensure_running", ensure_running)

    await manager.restart()

    assert order == ["stop", "ensure_running"]


async def test_supervisor_restarts_after_three_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(password="secret")
    manager.health_interval = 0
    health_calls: list[bool] = []

    async def unhealthy() -> bool:
        health_calls.append(False)
        return False

    async def no_sleep(_delay: float) -> None:
        return None

    restarts: list[int] = []

    async def restart() -> None:
        restarts.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "health", unhealthy)
    monkeypatch.setattr(manager, "restart", restart)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await manager.run_forever()

    assert len(health_calls) == 3
    assert restarts == [1]


async def test_supervisor_resets_counter_after_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(password="secret")
    manager.health_interval = 0
    probes = [False, False, True, False, False]

    async def health() -> bool:
        if not probes:
            raise asyncio.CancelledError
        return probes.pop(0)

    async def no_sleep(_delay: float) -> None:
        return None

    restarts: list[int] = []

    async def restart() -> None:
        restarts.append(1)

    monkeypatch.setattr(manager, "health", health)
    monkeypatch.setattr(manager, "restart", restart)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await manager.run_forever()

    assert restarts == []


# -- Conversation directory ------------------------------------------------


async def test_start_uses_the_given_directory_as_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "IS_WINDOWS", False)
    manager = _manager(password="secret", binary="opencode", directory=Path("C:/default"))
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await manager.start(directory=Path("C:/conv/project"))
    await asyncio.sleep(0)

    assert captured["cwd"] == str(Path("C:/conv/project"))


async def test_ensure_running_forwards_directory_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(password="secret")
    probes = iter([False, True])
    started: list[object] = []

    async def health() -> bool:
        return next(probes)

    async def start(*, directory: object = None) -> None:
        started.append(directory)

    monkeypatch.setattr(manager, "health", health)
    monkeypatch.setattr(manager, "start", start)

    await manager.ensure_running(directory=Path("C:/conv/project"))

    assert started == [Path("C:/conv/project")]
