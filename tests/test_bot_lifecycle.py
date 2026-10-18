"""Phase 6: entry-point lifecycle tests (no PTB, no polling, no live server).

``services.server_lifecycle`` owns the ordering invariant the bot relies on:

  1. ``ensure_running()`` (fail fast on a missing password),
  2. the ``run_forever()`` supervisor task,
  3. polling,
  4. on shutdown: cancel the supervisor, then ``stop()`` (owned only).

These tests drive the real orchestration functions with a fake manager, so a
regression in the order is caught without importing ``bot`` or starting PTB.
"""

from __future__ import annotations

import asyncio

import pytest

from services.opencode_server.errors import OpenCodeServerConfigError
from services.server_lifecycle import (
    SUPERVISOR_TASK_NAME,
    start_server_lifecycle,
    stop_server_lifecycle,
)


class _FakeManager:
    def __init__(self, order: list[str], *, config_error: bool = False) -> None:
        self.order = order
        self.config_error = config_error
        self.supervising = asyncio.Event()
        self.stop_calls = 0

    async def ensure_running(self) -> None:
        self.order.append("ensure_running")
        if self.config_error:
            raise OpenCodeServerConfigError("OPENCODE_SERVER_PASSWORD is missing")

    async def run_forever(self) -> None:
        self.order.append("run_forever")
        self.supervising.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.order.append("run_forever_cancelled")

    async def stop(self) -> None:
        self.stop_calls += 1
        self.order.append("stop")


async def _noop() -> None:
    return None


async def test_server_is_ready_before_polling_starts() -> None:
    order: list[str] = []
    manager = _FakeManager(order)

    async def start_polling() -> None:
        order.append("start_polling")

    supervisor = await start_server_lifecycle(manager, start_polling=start_polling)

    assert order.index("ensure_running") < order.index("start_polling")
    assert supervisor is not None
    assert supervisor.get_name() == SUPERVISOR_TASK_NAME

    await asyncio.wait_for(manager.supervising.wait(), timeout=1)
    assert "run_forever" in order

    await stop_server_lifecycle(manager, supervisor)


async def test_shutdown_cancels_the_supervisor_then_stops_the_server() -> None:
    order: list[str] = []
    manager = _FakeManager(order)
    supervisor = await start_server_lifecycle(manager, start_polling=_noop)
    assert supervisor is not None
    await asyncio.wait_for(manager.supervising.wait(), timeout=1)

    await stop_server_lifecycle(manager, supervisor)

    assert supervisor.cancelled() is True
    assert manager.stop_calls == 1
    assert order.index("run_forever_cancelled") < order.index("stop")


async def test_missing_password_fails_fast_before_polling() -> None:
    order: list[str] = []
    manager = _FakeManager(order, config_error=True)
    polled: list[bool] = []

    async def start_polling() -> None:
        polled.append(True)

    with pytest.raises(OpenCodeServerConfigError):
        await start_server_lifecycle(manager, start_polling=start_polling)

    assert polled == []
    assert "run_forever" not in order


async def test_cli_mode_polls_without_a_server() -> None:
    polled: list[bool] = []

    async def start_polling() -> None:
        polled.append(True)

    supervisor = await start_server_lifecycle(None, start_polling=start_polling)

    assert supervisor is None
    assert polled == [True]


async def test_shutdown_without_a_server_is_a_noop() -> None:
    await stop_server_lifecycle(None, None)


async def test_shutdown_stops_a_manager_even_without_a_supervisor() -> None:
    order: list[str] = []
    manager = _FakeManager(order)

    await stop_server_lifecycle(manager, None)

    assert manager.stop_calls == 1
