"""Lifecycle manager for a long-lived ``opencode serve`` process.

The bot owns the server in server mode: it probes an existing healthy instance
(and adopts it), or spawns one and waits until ``/global/health`` answers.
Ownership matters: ``stop()`` only kills a process this manager spawned, so a
bot and the Flet GUI can coexist against the same server.

Windows specifics: npm installs ``opencode`` as a ``.cmd`` shim, which
``CreateProcess`` cannot execute directly — the command is wrapped in
``cmd /c``. Because the real server is then a child of ``cmd.exe``, shutdown
uses ``taskkill /F /T`` on the whole tree up front (``terminate()`` would
orphan the child). On POSIX it terminates first and escalates to ``kill()``
after 5s.
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

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

NON_LOOPBACK_HOST_MESSAGE = (
    "OPENCODE_SERVER_HOST={host!r} no es una dirección loopback y el servidor "
    "OpenCode queda expuesto en la red. El bot es su ÚNICO cliente (los "
    "clientes Telegram/APK nunca hablan con el agente): mantén el servidor en "
    "127.0.0.1, ::1 o localhost. Solo si el bot y el agente viven en máquinas "
    "distintas, define OPENCODE_SERVER_ALLOW_REMOTE=1 y protege el puerto con "
    "TLS, firewall y una contraseña fuerte."
)


@dataclass(frozen=True)
class ServerConfig:
    """Everything the manager needs to spawn and probe the server.

    ``password`` has no default on purpose: an empty value is rejected by
    ``ensure_running()`` / ``start()`` with ``OpenCodeServerConfigError``.

    ``allow_remote`` opts out of the loopback guard. The guard is enforced
    here (at construction, on the startup path) rather than in ``config.py``
    so importing ``config`` stays side-effect free.
    """

    host: str = "127.0.0.1"
    port: int = 4096
    password: str = ""
    binary: str = "opencode"
    directory: str | Path = Path(__file__).resolve().parents[2]
    timeout: float = 20.0
    allow_remote: bool = False

    def __post_init__(self) -> None:
        if self.allow_remote:
            return
        if self.host.strip().lower() in LOOPBACK_HOSTS:
            return
        raise OpenCodeServerConfigError(NON_LOOPBACK_HOST_MESSAGE.format(host=self.host))


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
        #: Directory used by the process this manager spawned. Restarts reuse
        #: it so the process cwd stays consistent with the first spawn
        #: (per-call routing still uses ``?directory=``).
        self._spawn_directory: Path | None = None
        #: Adoption is logged at INFO only once per manager; probing a shared
        #: server must not spam the log on every prompt.
        self._adoption_logged = False

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

    async def ensure_running(self, *, directory: str | Path | None = None) -> None:
        """Adopt a healthy server, or spawn one and wait until it is healthy.

        ``directory`` is the CONVERSATION's workdir when known: it becomes the
        spawn cwd for a server this call starts. An already-healthy server is
        adopted untouched — routing is per request (``?directory=``), so a
        shared process can serve several project folders.

        Raises ``OpenCodeServerConfigError`` immediately when the password is
        empty (the server would reject every authenticated route) and
        ``OpenCodeUnavailableError`` when the spawned server never becomes
        healthy within ``config.timeout``.
        """
        self._require_password()

        if await self.health():
            if self._adoption_logged:
                logger.debug(
                    "OpenCode server already healthy; reusing the adopted instance",
                    extra={"event": "opencode_server_adopted", "base_url": self.base_url},
                )
            else:
                self._adoption_logged = True
                logger.info(
                    "OpenCode server already healthy; adopting it",
                    extra={"event": "opencode_server_adopted", "base_url": self.base_url},
                )
            return

        await self.start(directory=directory)

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

    async def start(self, *, directory: str | Path | None = None) -> None:
        """Spawn ``opencode serve`` and start draining its output.

        ``directory`` overrides the configured cwd for this spawn (used for
        the conversation's project). Idempotent while the tracked process is
        alive: a second call is a no-op, so the supervisor cannot spawn
        duplicates.
        """
        self._require_password()

        if self._process is not None and self._process.returncode is None:
            return

        workdir = Path(directory) if directory is not None else Path(self._config.directory)
        cmd = self._build_command()
        env = os.environ.copy()
        env["OPENCODE_SERVER_PASSWORD"] = self._config.password

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workdir),
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
        self._spawn_directory = workdir
        self._start_drainers(process)

        logger.info(
            "OpenCode server started",
            extra={
                "event": "opencode_server_start",
                "base_url": self.base_url,
                "pid": process.pid,
                "binary": self._config.binary,
                "workdir": str(workdir),
            },
        )

    async def stop(self) -> None:
        """Stop the server only if this manager owns it.

        Windows: ``taskkill /F /T`` kills the ``cmd.exe`` tree so the real
        server cannot be orphaned. POSIX: ``terminate()``, wait up to 5s,
        then ``kill()``. An adopted server (``is_owned is False``) is never
        touched.

        Ownership is cleared ONLY after the child is confirmed dead. If the
        kill fails (``taskkill`` could not launch / returned non-zero) or the
        wait times out, the process may still hold the port: keep it tracked
        so a later ``stop()`` can retry, and log ERROR instead of pretending
        the shutdown succeeded.
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
                killed = await self._taskkill(pid)
                if not killed:
                    logger.warning(
                        "taskkill did not confirm the OpenCode server tree was killed",
                        extra={
                            "event": "opencode_server_taskkill_unconfirmed",
                            "pid": pid,
                        },
                    )
                # Even when taskkill reports failure the child may have exited
                # on its own; a bounded wait reaps it when that is the case.
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
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT)

        if process.returncode is None:
            # The kill could not be confirmed: the child may still be alive and
            # hold the port. Keep ``_process``/``_owned``/drainers so a later
            # ``stop()`` retries instead of forgetting a live orphan.
            logger.error(
                "OpenCode server stop could not be confirmed; keeping ownership",
                extra={
                    "event": "opencode_server_stop_unconfirmed",
                    "pid": pid,
                    "forced": forced,
                },
            )
            return

        await self._cancel_drainers()
        self._process = None
        self._owned = False

        logger.info(
            "OpenCode server stopped",
            extra={"event": "opencode_server_stop", "pid": pid, "forced": forced},
        )

    async def restart(self) -> None:
        """Stop an owned server and bring a healthy one back (owned).

        The replacement is spawned in the SAME directory as the original
        spawn, so the process cwd and the conversation routing agree. A
        never-started manager falls back to ``ServerConfig.directory``.
        """
        await self.stop()
        await self.ensure_running(directory=self._spawn_directory)

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

    async def _taskkill(self, pid: int) -> bool:
        """Kill a process tree; return True only when taskkill confirms it.

        A launch ``OSError`` or a non-zero exit code means the tree may still
        be alive, so the caller must keep ownership instead of clearing it.
        """
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
            return False
        return killer.returncode == 0
