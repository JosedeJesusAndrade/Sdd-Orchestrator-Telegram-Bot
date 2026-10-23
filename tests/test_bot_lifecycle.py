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
import logging

import pytest

from services.opencode_server.errors import (
    OpenCodeServerConfigError,
    OpenCodeServerError,
    OpenCodeUnavailableError,
)
from services.server_lifecycle import (
    SUPERVISOR_TASK_NAME,
    log_startup_failure,
    start_server_lifecycle,
    start_server_or_exit,
    stop_after_startup_failure,
    stop_server_lifecycle,
)


class _FakeManager:
    def __init__(
        self,
        order: list[str],
        *,
        config_error: bool = False,
        unavailable_error: bool = False,
    ) -> None:
        self.order = order
        self.config_error = config_error
        self.unavailable_error = unavailable_error
        self.supervising = asyncio.Event()
        self.stop_calls = 0

    async def ensure_running(self) -> None:
        self.order.append("ensure_running")
        if self.config_error:
            raise OpenCodeServerConfigError("OPENCODE_SERVER_PASSWORD is missing")
        if self.unavailable_error:
            raise OpenCodeUnavailableError("no se pudo iniciar opencode serve")

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


# ── Graceful startup failure (#1) ────────────────────────────────────────


class _FakeApp:
    """Mirrors the ``app.stop()`` / ``app.shutdown()`` pair ``bot.py`` runs."""

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def stop(self) -> None:
        self.order.append("app.stop")

    async def shutdown(self) -> None:
        self.order.append("app.shutdown")


async def test_unavailable_server_startup_exits_non_zero_after_cleanup() -> None:
    """The old code let ``OpenCodeUnavailableError`` escape with a traceback."""
    order: list[str] = []
    manager = _FakeManager(order, unavailable_error=True)
    app = _FakeApp(order)
    polled: list[bool] = []
    errors: list[OpenCodeServerError] = []

    async def start_polling() -> None:
        polled.append(True)

    async def shutdown() -> None:
        await app.stop()
        await app.shutdown()

    with pytest.raises(SystemExit) as exc_info:
        await start_server_or_exit(
            manager,
            start_polling=start_polling,
            shutdown=shutdown,
            on_error=errors.append,
        )

    assert exc_info.value.code == 1
    assert polled == []
    assert [type(error) for error in errors] == [OpenCodeUnavailableError]
    assert order.index("stop") < order.index("app.stop") < order.index("app.shutdown")


async def test_config_error_keeps_the_fail_fast_and_also_exits_cleanly() -> None:
    order: list[str] = []
    manager = _FakeManager(order, config_error=True)
    app = _FakeApp(order)
    errors: list[OpenCodeServerError] = []

    async def shutdown() -> None:
        await app.stop()
        await app.shutdown()

    with pytest.raises(SystemExit) as exc_info:
        await start_server_or_exit(
            manager,
            start_polling=_noop,
            shutdown=shutdown,
            on_error=errors.append,
        )

    assert exc_info.value.code == 1
    assert len(errors) == 1
    assert isinstance(errors[0], OpenCodeServerConfigError)
    assert order.index("stop") < order.index("app.stop")


async def test_stop_after_startup_failure_isolates_cleanup_errors() -> None:
    """A failed stop or shutdown must never mask the original startup error."""

    class _BrokenManager(_FakeManager):
        async def stop(self) -> None:
            self.order.append("stop")
            raise RuntimeError("stop boom")

    manager = _BrokenManager([])

    async def shutdown() -> None:
        raise RuntimeError("shutdown boom")

    # Must not raise: best-effort teardown.
    await stop_after_startup_failure(manager, shutdown=shutdown)


async def test_startup_cleanup_without_a_server_still_shuts_the_app_down() -> None:
    order: list[str] = []

    async def shutdown() -> None:
        order.append("app.shutdown")

    await stop_after_startup_failure(None, shutdown=shutdown)

    assert order == ["app.shutdown"]


# ── Startup failure log events (#1, finding 3) ───────────────────────────


def test_config_error_logs_the_config_event(caplog: pytest.LogCaptureFixture) -> None:
    """The missing-password path keeps its own diagnostic event."""
    with caplog.at_level(logging.CRITICAL):
        log_startup_failure(OpenCodeServerConfigError("OPENCODE_SERVER_PASSWORD is missing"))

    events = [getattr(record, "event", "") for record in caplog.records]
    assert "opencode_server_config_error" in events
    assert "opencode_server_startup_failed" not in events


def test_unavailable_error_logs_the_startup_failed_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn/port failure must be distinguishable by event + error_type."""
    with caplog.at_level(logging.CRITICAL):
        log_startup_failure(OpenCodeUnavailableError("no se pudo iniciar opencode serve"))

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "opencode_server_startup_failed"
    ]
    assert len(records) == 1
    assert getattr(records[0], "error_type", None) == "OpenCodeUnavailableError"
