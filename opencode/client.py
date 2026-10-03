"""OpenCode CLI client for running prompts and managing sessions."""
import asyncio
import json
import re
import subprocess
from typing import Any

from config import OPENCODE_CMD, INTERNAL_SUBPROCESS_TIMEOUT
from utils.logging import get_module_logger

logger = get_module_logger(__name__)


async def query_opencode_db(sql: str, allowed_pattern: str | None = None) -> list[dict[str, Any]]:
    """Execute a SQL query against opencode.db via CLI. Returns list of dicts."""
    if allowed_pattern:
        match = re.search(r'WHERE\s+(\w+)\s*=\s*[\'"](\w+)[\'"]', sql)
        if match:
            identifier = match.group(2)
            if not re.match(allowed_pattern, identifier):
                logger.warning(
                    "Blocked unsafe SQL query",
                    extra={
                        "event": "sql_blocked",
                        "sql_preview": sql[:100].replace("\n", " "),
                    },
                )
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
                timeout=INTERNAL_SUBPROCESS_TIMEOUT,
            )
        )
        if result.returncode != 0:
            logger.warning(
                "DB query failed",
                extra={
                    "event": "db_query_failed",
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip()[:100],
                },
            )
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except Exception as e:
        logger.warning(
            "DB query error",
            extra={
                "event": "db_query_error",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return []

