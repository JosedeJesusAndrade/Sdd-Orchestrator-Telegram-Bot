"""OpenCode CLI backend — executes prompts via subprocess."""
from __future__ import annotations
import asyncio
import os
import subprocess
import logging
from services.ai_backend import AIBackendResult

logger = logging.getLogger(__name__)


class OpenCodeCLIBackend:
    """Executes OpenCode prompts via CLI subprocess."""
    
    def __init__(self, opencode_cmd: str, workdir: str, timeout: int = 300):
        self._cmd = opencode_cmd
        self._workdir = workdir
        self._timeout = timeout
        self._current_process: subprocess.Popen | None = None
    
    async def execute(
        self, prompt: str, model: str, session_id: str | None, workdir: str,
    ) -> AIBackendResult:
        cmd_parts = [self._cmd, "run"]
        if model:
            cmd_parts.extend(["--model", model])
        if session_id:
            cmd_parts.extend(["--continue", "--session", session_id])
        cmd_parts.append(prompt)
        
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
        self._current_process = proc
        
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout,
            )
            return AIBackendResult(
                stdout=stdout.decode("utf-8", errors="replace") if stdout else "",
                stderr=stderr.decode("utf-8", errors="replace") if stderr else "",
                returncode=proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            self.cancel()
            return AIBackendResult(
                stderr=f"Timeout: el prompt tardó más de {self._timeout}s.",
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
            pass
