"""OpenCode CLI backend — executes prompts via subprocess.

Phase 2 logging: every subprocess execution emits structured events:
  - INFO    event=subprocess_start     (cmd, model)
  - INFO    event=subprocess_complete  (cmd, model, returncode, duration_s)
  - WARNING event=subprocess_timeout   (cmd, timeout_s)
  - ERROR   event=subprocess_error     (cmd, model, returncode, stderr)
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING

from services.ai_backend import AIBackendResult
from utils.logging import get_module_logger, log_exception

if TYPE_CHECKING:
    pass


logger = get_module_logger(__name__)


class OpenCodeCLIBackend:
    """Executes OpenCode prompts via CLI subprocess."""

    def __init__(self, opencode_cmd: str, workdir: str, timeout: int = 300):
        self._cmd = opencode_cmd
        self._workdir = workdir
        self._timeout = timeout
        self._current_process: subprocess.Popen | None = None

    async def execute(
        self, prompt: str, model: str, session_id: str | None,
        agent: str | None = None, workdir: str | None = None,
        source_label: str = "",
        timeout: int | None = None,
    ) -> AIBackendResult:
        """Execute a prompt via the OpenCode CLI subprocess.

        Phase 2 logging: emits subprocess_start/complete/timeout/error events
        with structured fields. The `cmd` field is the OpenCode CLI name
        (not the full prompt) — prompts may contain user data we don't want
        in logs.

        Args:
            timeout: Per-call timeout in seconds (F10). Overrides the
                constructor's ``self._timeout``. ``None`` means "use the
                backend default". The effective value is the one used by
                ``asyncio.wait_for`` AND reported in the timeout event/error
                so the log is truthful about what actually happened.
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        cmd_parts = [self._cmd, "run"]
        if model:
            cmd_parts.extend(["--model", model])
        if agent:
            cmd_parts.extend(["--agent", agent])
        if session_id:
            cmd_parts.extend(["--continue", "--session", session_id])
        # Prepend frontend marker + fix Windows newline-as-separator bug.
        # The label is supplied by the frontend (ChatView.source_label) so the
        # execution engine carries no hardcoded frontend branding.
        effective_workdir = workdir or self._workdir
        labeled = f"[{source_label}] {prompt}" if source_label else prompt
        cmd_parts.append(labeled.replace("\n", " "))

        env = os.environ.copy()
        env["NO_COLOR"] = "1"

        logger.info(
            "Subprocess start",
            extra={
                "event": "subprocess_start",
                "cmd": self._cmd,
                "model": model,
                "workdir": effective_workdir,
            },
        )
        start_time = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=effective_workdir,
            env=env,
        )
        self._current_process = proc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout,
            )
            elapsed = time.monotonic() - start_time
            returncode = proc.returncode or 0

            if returncode != 0:
                logger.error(
                    "Subprocess failed",
                    extra={
                        "event": "subprocess_error",
                        "cmd": self._cmd,
                        "model": model,
                        "returncode": returncode,
                        "duration_s": round(elapsed, 3),
                        "stderr": (stderr.decode("utf-8", errors="replace") if stderr else "")[:500],
                    },
                )
            else:
                logger.info(
                    "Subprocess complete",
                    extra={
                        "event": "subprocess_complete",
                        "cmd": self._cmd,
                        "model": model,
                        "returncode": returncode,
                        "duration_s": round(elapsed, 3),
                    },
                )

            return AIBackendResult(
                stdout=stdout.decode("utf-8", errors="replace") if stdout else "",
                stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
                returncode=returncode,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.warning(
                "Subprocess timeout",
                extra={
                    "event": "subprocess_timeout",
                    "cmd": self._cmd,
                    "model": model,
                    "timeout_s": effective_timeout,
                    "elapsed_s": round(elapsed, 3),
                },
            )
            self.cancel()
            return AIBackendResult(
                stderr=f"Timeout: el prompt tardó más de {effective_timeout}s.",
                returncode=-1,
                timed_out=True,
            )
        finally:
            self._current_process = None

    def cancel(self) -> None:
        if self._current_process is None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._current_process.pid)],
                    capture_output=True,
                )
            else:
                self._current_process.terminate()
        except Exception:
            log_exception(
                "subprocess_kill_failed",
                module=__name__,
                level=logging.WARNING,
                pid=self._current_process.pid,
            )
