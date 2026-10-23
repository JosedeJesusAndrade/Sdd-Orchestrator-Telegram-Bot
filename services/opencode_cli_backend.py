"""OpenCode CLI backend — executes prompts via subprocess.

Phase 2 logging: every subprocess execution emits structured events:
  - INFO    event=subprocess_start     (cmd, model)
  - INFO    event=subprocess_complete  (cmd, model, returncode, duration_s)
  - WARNING event=subprocess_timeout   (cmd, timeout_s)
  - ERROR   event=subprocess_error     (cmd, model, returncode, stderr)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from typing import Any

from services.ai_backend import AIBackendResult
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)


def _part_error_message(part: dict[str, Any]) -> str | None:
    """Best-effort extraction of an ``error`` event's human message."""
    error = part.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            data_message = data.get("message")
            if isinstance(data_message, str):
                return data_message
        error_message = error.get("message")
        if isinstance(error_message, str):
            return error_message
    message = part.get("message")
    return message if isinstance(message, str) else None


def _parse_json_output(raw_stdout: str) -> tuple[str, str | None]:
    """Extract assistant text + sessionID from ``opencode run --format json``.

    The CLI emits NDJSON: one JSON event per line. Each event carries the
    top-level ``sessionID``; assistant text lives in ``part.text`` of
    ``type == "text"`` events. The default (non-JSON) format prints each text
    part followed by a newline, so the reconstruction joins parts with ``\\n``
    and keeps one trailing newline.

    Robustness: if the output is not valid NDJSON (CLI version drift), the
    raw stdout is returned as the response text with ``session_id=None`` so
    the caller still shows *something* instead of crashing.
    """
    if not raw_stdout.strip():
        return "", None

    texts: list[str] = []
    error_texts: list[str] = []
    session_id: str | None = None

    try:
        for line in raw_stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            event = json.loads(stripped)
            if not isinstance(event, dict):
                raise ValueError("event is not a JSON object")

            event_session = event.get("sessionID")
            if not session_id and isinstance(event_session, str) and event_session:
                session_id = event_session

            part = event.get("part")
            if not isinstance(part, dict):
                continue

            part_session = part.get("sessionID")
            if not session_id and isinstance(part_session, str) and part_session:
                session_id = part_session

            event_type = event.get("type")
            if event_type == "text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif event_type == "error":
                message = _part_error_message(part)
                if message:
                    error_texts.append(message)
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning(
            "Could not parse JSON output; falling back to raw stdout",
            extra={"event": "json_output_parse_failed", "cmd": "opencode"},
        )
        return raw_stdout, None

    if texts:
        return "\n".join(texts) + "\n", session_id
    if error_texts:
        return "\n".join(error_texts) + "\n", session_id
    return "", session_id


class OpenCodeCLIBackend:
    """Executes OpenCode prompts via CLI subprocess."""

    #: One-shot subprocess output cannot be streamed incrementally.
    supports_live_progress = False

    def __init__(self, opencode_cmd: str, workdir: str, timeout: int = 300):
        self._cmd = opencode_cmd
        self._workdir = workdir
        self._timeout = timeout
        self._current_process: asyncio.subprocess.Process | None = None

    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        agent: str | None = None,
        workdir: str | None = None,
        source_label: str = "",
        timeout: float | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
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
        cmd_parts = [self._cmd, "run", "--format", "json"]
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
                proc.communicate(),
                timeout=effective_timeout,
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
                        "stderr": (stderr.decode("utf-8", errors="replace") if stderr else "")[
                            :500
                        ],
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

            raw_stdout = stdout.decode("utf-8", errors="replace") if stdout else ""
            response_text, session_id = _parse_json_output(raw_stdout)

            return AIBackendResult(
                stdout=response_text,
                stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
                returncode=returncode,
                session_id=session_id,
            )
        except TimeoutError:
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
