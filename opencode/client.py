"""OpenCode CLI client for running prompts and managing sessions."""

import asyncio
import json
import re
import subprocess
from typing import Any

from config import INTERNAL_SUBPROCESS_TIMEOUT, OPENCODE_CMD
from utils.logging import get_module_logger

logger = get_module_logger(__name__)


class OpenCodeDBError(RuntimeError):
    """Raised by ``query_opencode_db`` when a caller opts into hard failures."""


async def query_opencode_db(
    sql: str,
    allowed_pattern: str | None = None,
    *,
    raise_on_error: bool = False,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Execute a SQL query against opencode.db via CLI. Returns list of dicts.

    Args:
        sql: The SQL to execute. The CLI has no parameter binding, so callers
            that interpolate untrusted values MUST validate/escape them first.
        allowed_pattern: Optional guard applied to the identifier in
            ``WHERE <ident> = '<word>'``. It cannot understand aliased
            predicates; new callers should validate locally instead.
        raise_on_error: When true, failures raise ``OpenCodeDBError`` instead of
            logging and returning an empty list. Default preserves the historic
            best-effort behavior.
        timeout: Per-call subprocess timeout in seconds. ``None`` uses
            ``INTERNAL_SUBPROCESS_TIMEOUT``.
    """
    effective_timeout = INTERNAL_SUBPROCESS_TIMEOUT if timeout is None else timeout
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
                if raise_on_error:
                    raise OpenCodeDBError("Blocked unsafe SQL query")
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
                timeout=effective_timeout,
            ),
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
            if raise_on_error:
                raise OpenCodeDBError(
                    f"DB query failed with returncode {result.returncode}: "
                    f"{result.stderr.strip()[:200]}"
                )
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except OpenCodeDBError:
        raise
    except Exception as e:
        logger.warning(
            "DB query error",
            extra={
                "event": "db_query_error",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        if raise_on_error:
            raise OpenCodeDBError(f"DB query error ({type(e).__name__}): {e}") from e
        return []
