"""Lifecycle manager for a long-lived ``opencode serve`` process.

The bot owns the server in server mode: it probes an existing healthy instance
(and adopts it), or spawns one and waits until ``/global/health`` answers.
Ownership matters: ``stop()`` only kills a process this manager spawned, so a
bot and the Flet GUI can coexist against the same server.

Windows specifics: npm installs ``opencode`` as a ``.cmd`` shim, which
``CreateProcess`` cannot execute directly — the command is wrapped in
``cmd /c``. Shutdown uses ``terminate()`` first and escalates to
``taskkill /F /T`` (the whole tree) when the process ignores it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from services.opencode_server.errors import (
    OpenCodeServerConfigError,
    OpenCodeServerError,
    OpenCodeUnavailableError,
)
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

IS_WINDOWS = os.name == "nt"

AUTH_USERNAME = "opencode"
HEALTH_TIMEOUT = 2.0
START_POLL_INTERVAL = 0.2
STOP_TIMEOUT = 5.0


@dataclass(frozen=True)
class ServerConfig:
    """Everything the manager needs to spawn and probe the server.

    ``password`` has no default on purpose: an empty value is rejected by
    ``ensure_running()`` / ``start()`` with ``OpenCodeServerConfigError``.
    """

    host: str = "127.0.0.1"
    port: int = 4096
    password: str = ""
    binary: str = "opencode"
    directory: str | Path = Path(__file__).resolve().parents[2]
    timeout: float = 20.0


class OpenCodeServerManager:
    """Probe, spawn, supervise, and shut down ``opencode serve``."""

    #: Seconds between supervisor health probes.
    health_interval: float = 30.0
    #: Consecutive failures before the supervisor restarts the server.
    max_failures: int = 3

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._owned = False
        self._drain_tasks: list[asyncio.Task[None]] = []

    @property
    def base_url(self) -> str:
        return f"http://{self._config.host}:{self._config.port}"

    @property
    def is_owned(self) -> bool:
        """True only when this manager spawned the running process."""
        return self._owned

    async def health(self) -> bool:
        """Return True when ``GET /global/health`` answers 200 (2s budget)."""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(HEALTH_TIMEOUT),
                auth=httpx.BasicAuth(AUTH_USERNAME, self._config.password),
            ) as client:
                response = await client.get(f"{self.base_url}/global/health")
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure_running(self) -> None:
        """Adopt a healthy server, or spawn one and wait until it is healthy.

        Raises ``OpenCodeServerConfigError`` immediately when the password is
        empty (the server would reject every authenticated route) and
        ``OpenCodeUnavailableError`` when the spawned server never becomes
        healthy within ``config.timeout``.
        """
        self._require_password()

        if await self.health():
            logger.info(
                "OpenCode server already healthy; adopting it",
                extra={"event": "opencode_server_adopted", "base_url": self.base_url},
            )
            return

        await self.start()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout
        while loop.time() < deadline:
            if await self.health():
                logger.info(
                    "OpenCode server is ready",
                    extra={
                        "event": "opencode_server_ready",
                        "base_url": self.base_url,
                        "pid": self._process.pid if self._process else None,
                    },
                )
                return
            await asyncio.sleep(START_POLL_INTERVAL)

        await self.stop()
        raise OpenCodeUnavailableError(
            f"El servidor OpenCode no respondió en {self._config.timeout:g}s ({self.base_url})."
        )

    async def start(self) -> None:
        """Spawn ``opencode serve`` and start draining its output.

        Idempotent while the tracked process is alive: a second call is a
        no-op, so the supervisor cannot spawn duplicates.
        """
        self._require_password()

        if self._process is not None and self._process.returncode is None:
            return

        cmd = self._build_command()
        env = os.environ.copy()
        env["OPENCODE_SERVER_PASSWORD"] = self._config.password

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._config.directory),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise OpenCodeUnavailableError(
                f"No se pudo iniciar el servidor OpenCode ({self._config.binary!r}): {exc}"
            ) from exc

        self._process = process
        self._owned = True
        self._start_drainers(process)

        logger.info(
            "OpenCode server started",
            extra={
                "event": "opencode_server_start",
                "base_url": self.base_url,
                "pid": process.pid,
                "binary": self._config.binary,
                "workdir": str(self._config.directory),
            },
        )

    async def stop(self) -> None:
        """Stop the server only if this manager owns it.

        ``terminate()`` -> wait up to 5s -> ``taskkill /F /T`` (Windows) or
        ``kill()`` elsewhere. An adopted server (``is_owned is False``) is
        never touched.
        """
        process = self._process
        if not self._owned or process is None:
            return

        pid = process.pid
        forced = False

        if process.returncode is None:
            if IS_WINDOWS:
                # npm installs ``opencode`` as a .cmd shim, so the real server
                # runs as a CHILD of cmd.exe. ``terminate()`` kills only the
                # wrapper and orphans the server (verified live: the child
                # kept the port bound). Kill the whole tree up front instead.
                forced = True
                await self._taskkill(pid)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT)
            else:
                try:
                    process.terminate()
                except ProcessLookupError:
                    log_exception(
                        "opencode_server_terminate_missing",
                        module=__name__,
                        pid=pid,
                    )

                try:
                    await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT)
                except TimeoutError:
                    forced = True
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

        await self._cancel_drainers()
        self._process = None
        self._owned = False

        logger.info(
            "OpenCode server stopped",
            extra={"event": "opencode_server_stop", "pid": pid, "forced": forced},
        )

    async def restart(self) -> None:
        """Stop an owned server and bring a healthy one back (owned)."""
        await self.stop()
        await self.ensure_running()

    async def run_forever(self) -> None:
        """Supervisor: restart after ``max_failures`` consecutive misses.

        A single transient probe failure never restarts the server (a restart
        costs ~1 GB RSS + model warm-up). Successive successes reset the
        counter.
        """
        failures = 0
        while True:
            await asyncio.sleep(self.health_interval)

            if await self.health():
                if failures:
                    logger.info(
                        "OpenCode server recovered",
                        extra={"event": "opencode_server_recovered", "failures": failures},
                    )
                failures = 0
                continue

            failures += 1
            logger.warning(
                "OpenCode server health check failed",
                extra={
                    "event": "opencode_server_health_miss",
                    "failures": failures,
                    "max_failures": self.max_failures,
                },
            )

            if failures < self.max_failures:
                continue

            logger.error(
                "Restarting unhealthy OpenCode server",
                extra={"event": "opencode_server_restart", "failures": failures},
            )
            try:
                await self.restart()
            except OpenCodeServerError:
                log_exception(
                    "opencode_server_restart_failed",
                    module=__name__,
                    level=logging.ERROR,
                )
            failures = 0

    # ── Internals ──────────────────────────────────────────────────────────

    def _require_password(self) -> None:
        if not self._config.password:
            raise OpenCodeServerConfigError(
                "OPENCODE_SERVER_PASSWORD no está configurado. El servidor "
                "OpenCode exige Basic auth; define la variable antes de iniciar."
            )

    def _build_command(self) -> list[str]:
        cmd = [
            self._config.binary,
            "serve",
            "--port",
            str(self._config.port),
            "--hostname",
            self._config.host,
        ]
        if IS_WINDOWS and self._config.binary.lower().endswith((".cmd", ".bat")):
            # npm shims are batch files; CreateProcess needs cmd.exe to run them.
            cmd = ["cmd", "/c", *cmd]
        return cmd

    def _start_drainers(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is not None:
            self._drain_tasks.append(
                asyncio.create_task(self._drain_stream(process.stdout, "stdout"))
            )
        if process.stderr is not None:
            self._drain_tasks.append(
                asyncio.create_task(self._drain_stream(process.stderr, "stderr"))
            )

    async def _drain_stream(self, stream: asyncio.StreamReader, name: str) -> None:
        """Consume the child's pipe so it can never block on a full buffer."""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug(
                    "OpenCode server output",
                    extra={"event": "opencode_server_output", "stream": name, "line": text[:500]},
                )

    async def _cancel_drainers(self) -> None:
        for task in self._drain_tasks:
            task.cancel()
        for task in self._drain_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drain_tasks.clear()

    async def _taskkill(self, pid: int) -> None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            log_exception(
                "opencode_server_taskkill_failed",
                module=__name__,
                level=logging.WARNING,
                pid=pid,
            )
