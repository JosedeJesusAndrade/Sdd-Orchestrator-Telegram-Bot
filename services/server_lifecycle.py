"""Entry-point lifecycle for the long-lived OpenCode server.

The bot must not start answering Telegram updates before its transport is
usable. In ``server`` mode that means:

  1. ``ensure_running()`` — adopt a healthy server or spawn one and wait for
     ``/global/health``. A missing ``OPENCODE_SERVER_PASSWORD`` raises
     ``OpenCodeServerConfigError`` here, BEFORE polling starts (fail fast).
  2. Start ``run_forever()`` as a supervisor task.
  3. Only then start polling.

Shutdown mirrors the order: cancel the supervisor, then ``stop()`` the server
(the manager is ownership-aware and never kills an adopted instance).

The module is PTB-free so the lifecycle is unit-testable without importing
``telegram`` or running the updater.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from utils.logging import get_module_logger

logger = get_module_logger(__name__)

SUPERVISOR_TASK_NAME = "opencode_server_supervisor"


class ServerLifecycle(Protocol):
    """Structural type of the manager pieces this module drives."""

    async def ensure_running(self) -> None: ...

    async def run_forever(self) -> None: ...

    async def stop(self) -> None: ...


async def start_server_lifecycle(
    manager: ServerLifecycle | None,
    *,
    start_polling: Callable[[], Awaitable[None]],
) -> asyncio.Task[None] | None:
    """Bring the server up (if any) and only then start polling.

    ``manager is None`` is the CLI rollback path: polling starts immediately.
    ``OpenCodeServerConfigError`` from ``ensure_running()`` propagates and
    ``start_polling`` is never called, so a misconfigured server cannot leave
    the bot half-started.
    """
    supervisor: asyncio.Task[None] | None = None

    if manager is not None:
        await manager.ensure_running()
        supervisor = asyncio.create_task(
            manager.run_forever(),
            name=SUPERVISOR_TASK_NAME,
        )
        logger.info(
            "OpenCode server supervisor started",
            extra={"event": "opencode_server_supervisor_started"},
        )

    await start_polling()
    return supervisor


async def stop_server_lifecycle(
    manager: ServerLifecycle | None,
    supervisor: asyncio.Task[None] | None,
) -> None:
    """Cancel the supervisor and stop the server (owned instances only)."""
    if supervisor is not None:
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await supervisor
        logger.info(
            "OpenCode server supervisor stopped",
            extra={"event": "opencode_server_supervisor_stopped"},
        )

    if manager is not None:
        await manager.stop()
