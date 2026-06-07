"""OpenCode CLI client for running prompts and managing sessions."""
import asyncio
import json
import re
import subprocess
import sys

from ..config import OPENCODE_CMD, logger


async def query_opencode_db(sql: str, allowed_pattern: str = None) -> list[dict]:
    """Execute a SQL query against opencode.db via CLI. Returns list of dicts."""
    if allowed_pattern:
        match = re.search(r'WHERE\s+(\w+)\s*=\s*[\'"](\w+)[\'"]', sql)
        if match:
            identifier = match.group(2)
            if not re.match(allowed_pattern, identifier):
                logger.warning(f"Blocked potentially unsafe SQL query: {sql[:100]}")
                return []
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [OPENCODE_CMD, "db", sql, "--format", "json"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        )
        if result.returncode != 0:
            logger.warning(f"DB query failed: {result.stderr.strip()[:100]}")
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except Exception as e:
        logger.warning(f"DB query error: {e}")
        return []


def run_opencode(
    cmd: list[str],
    workdir: str,
    timeout: int,
    chat_id: int = None,
    current_process: dict = None,
    process_status: dict = None,
) -> tuple:
    """Run opencode with proper timeout via subprocess.Popen.

    Returns (stdout: str, stderr: str, exitcode: int, timed_out: bool).
    Stores process in current_process for /cancel support when chat_id is given.
    """
    if current_process is None:
        current_process = {}
    if process_status is None:
        process_status = {}

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=workdir,
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    if chat_id is not None:
        current_process[chat_id] = process

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, process.returncode, False
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
            )
        else:
            process.kill()
        process.wait()
        return (
            "",
            "Timeout: el prompt tard\u00f3 m\u00e1s de {} segundos.".format(timeout),
            -1,
            True,
        )
    finally:
        if chat_id is not None:
            current_process.pop(chat_id, None)
            process_status.pop(chat_id, None)
