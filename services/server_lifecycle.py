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
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from services.opencode_server.errors import (
    OpenCodeServerConfigError,
    OpenCodeServerError,
)
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

SUPERVISOR_TASK_NAME = "opencode_server_supervisor"

#: Fail-fast message for a misconfigured server backend. Logged with
#: ``logger.critical()`` (always reaches the console handler) before exit.
SERVER_CONFIG_ERROR_MESSAGE = (
    "No se puede iniciar el bot en modo servidor: falta OPENCODE_SERVER_PASSWORD. "
    "El servidor OpenCode exige Basic auth; define la variable en .env "
    "(recomendado: openssl rand -hex 16) o usa OPENCODE_BACKEND=cli."
)
SERVER_STARTUP_ERROR_MESSAGE = (
    "No se pudo iniciar el servidor OpenCode. Revisa el puerto "
    "(OPENCODE_SERVER_PORT), que no lo ocupe otro proceso con otras "
    "credenciales, y OPENCODE_SERVER_BINARY."
)


def log_startup_failure(error: OpenCodeServerError) -> None:
    """Log the structured startup-failure event before the process exits 1.

    Lives here (not inside ``run_bot``) so the event mapping is unit-testable
    without importing the PTB entry point. A config error keeps its own
    diagnostic and message; every other server error (spawn/port) is reported
    as ``opencode_server_startup_failed`` with ``error_type``.
    """
    if isinstance(error, OpenCodeServerConfigError):
        logger.critical(
            SERVER_CONFIG_ERROR_MESSAGE,
            extra={
                "event": "opencode_server_config_error",
                "error_type": type(error).__name__,
            },
            exc_info=True,
        )
        return
    logger.critical(
        SERVER_STARTUP_ERROR_MESSAGE,
        extra={
            "event": "opencode_server_startup_failed",
            "error_type": type(error).__name__,
        },
        exc_info=True,
    )


class ServerLifecycle(Protocol):
    """Structural type of the manager pieces this module drives."""

    async def ensure_running(self, *, directory: str | Path | None = None) -> None: ...

    async def run_forever(self) -> None: ...

    async def stop(self) -> None: ...


class WarmUpLifecycle(Protocol):
    """Minimal contract for an eager GUI warm-up (no supervisor)."""

    async def ensure_running(self, *, directory: str | Path | None = None) -> None: ...


async def warm_up_server(
    manager: WarmUpLifecycle | None,
    *,
    directory: str | Path | None = None,
) -> bool:
    """Bring the shared server up for the GUI; NEVER raise on a server error.

    ``manager is None`` is the CLI rollback path, which needs no server. The
    GUI must not crash when a spawn fails: it reports ``False`` so the caller
    can show a notice and let the next prompt retry lazily.
    """
    if manager is None:
        return True
    try:
        await manager.ensure_running(directory=directory)
    except OpenCodeServerError as exc:
        logger.warning(
            "No se pudo preparar el servidor OpenCode para la GUI",
            extra={
                "event": "opencode_server_warm_up_failed",
                "error_type": type(exc).__name__,
                "directory": str(directory) if directory is not None else None,
            },
        )
        return False
    logger.info(
        "OpenCode server ready for the GUI",
        extra={
            "event": "opencode_server_warm_up",
            "directory": str(directory) if directory is not None else None,
        },
    )
    return True


def log_server_left_running(manager: object | None) -> None:
    """Log that the GUI closed but the shared server was left running.

    D5: the server lives always. Closing Flet must NEVER stop it — the bot may
    be using the same process, and a stop would cause a supervisor outage.
    """
    logger.info(
        "Flet closed; leaving the OpenCode server running for the bot",
        extra={
            "event": "opencode_server_left_running",
            "owned": bool(getattr(manager, "is_owned", False)),
        },
    )


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


async def start_server_or_exit(
    manager: ServerLifecycle | None,
    *,
    start_polling: Callable[[], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]],
    on_error: Callable[[OpenCodeServerError], None],
) -> asyncio.Task[None] | None:
    """Start the server, and on ANY startup error tear down and exit 1.

    Catches the BASE ``OpenCodeServerError``: a missing password
    (``OpenCodeServerConfigError``) and a spawn/port failure
    (``OpenCodeUnavailableError``) both used to reach ``bot.py``, but only the
    config error was handled — the unavailable one escaped with a raw
    traceback and skipped ``app.stop()/shutdown()``.

    ``on_error`` owns the logging policy (the entry point knows the message).
    ``shutdown`` mirrors the app teardown (``app.stop()`` + ``app.shutdown()``).
    """
    try:
        return await start_server_lifecycle(manager, start_polling=start_polling)
    except OpenCodeServerError as exc:
        on_error(exc)
        await stop_after_startup_failure(manager, shutdown=shutdown)
        # Non-zero, clean exit: ``SystemExit`` never prints a traceback.
        raise SystemExit(1) from None


async def stop_after_startup_failure(
    manager: ServerLifecycle | None,
    *,
    shutdown: Callable[[], Awaitable[None]],
) -> None:
    """Best-effort teardown after a failed startup; NEVER raises.

    Order matters: stop the server this bot owns first (a half-spawned
    ``opencode serve`` must not survive), then release the Bot API resources.
    A broken ``stop()`` must not prevent ``shutdown()``.
    """
    if manager is not None:
        try:
            await manager.stop()
        except Exception:
            log_exception(
                "opencode_server_startup_stop_failed",
                module=__name__,
                level=logging.ERROR,
            )
    try:
        await shutdown()
    except Exception:
        log_exception(
            "opencode_server_startup_shutdown_failed",
            module=__name__,
            level=logging.ERROR,
        )


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
