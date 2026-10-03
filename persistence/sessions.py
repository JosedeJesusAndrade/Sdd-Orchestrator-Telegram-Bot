"""Session persistence layer for Telegram-OpenCode bridge.
Handles sessions.json with lock protection, TTL cache, and atomic writes."""

import json
import logging
import re
import shutil
import time
import asyncio
import subprocess
from pathlib import Path
from typing import Any

from config import SESSION_DB as SESSION_DB, OPENCODE_CMD, DEFAULT_SESSION_NAME, INTERNAL_SUBPROCESS_TIMEOUT
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

# Lock to prevent race conditions on sessions.json (multiple handlers)
session_lock = asyncio.Lock()

# Module-level cache for sessions.json to reduce filesystem I/O (P3)
_session_map_cache: dict[str, Any] | None = None
_session_map_cache_time: float = 0
SESSION_MAP_CACHE_TTL = 2.0  # seconds

# P4: TTL cache for fetch_opencode_sessions() to avoid redundant subprocess calls
_opencode_sessions_cache: list[dict[str, Any]] | None = None
_opencode_sessions_cache_time: float = 0
SESSION_LIST_CACHE_TTL = 10.0  # seconds


def invalidate_opencode_sessions_cache() -> None:
    """Invalidate the TTL cache for fetch_opencode_sessions (P4)."""
    global _opencode_sessions_cache, _opencode_sessions_cache_time
    _opencode_sessions_cache = None
    _opencode_sessions_cache_time = 0


def _backup_corrupt_session_db() -> None:
    """Move the unparseable sessions.json aside so data is not permanently lost.

    Uses shutil.move (rename): the corrupt file leaves the active path, so the
    next save writes a fresh sessions.json, while the original survives for
    forensic recovery under a timestamped sibling. Repeated corruptions produce
    distinct names and never clobber earlier backups.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = SESSION_DB.parent / f"{SESSION_DB.name}.corrupt-{stamp}"
    try:
        shutil.move(str(SESSION_DB), str(backup))
    except Exception:
        # A failed backup must not be silent. The corrupt file stays in place,
        # so nothing is destroyed even when the rename fails.
        log_exception(
            "session_backup_failed",
            module=__name__,
            level=logging.WARNING,
            path=str(SESSION_DB),
            backup_path=str(backup),
        )


def load_session_map() -> dict[str, Any]:
    """Load {chat_id: {active, sessions: {name: {id, title, created, last_used, prompt_count}}}}"""
    if SESSION_DB.exists():
        try:
            data: dict[str, Any] = json.loads(SESSION_DB.read_text(encoding="utf-8"))
            return data
        except Exception:
            # Corrupt/unreadable store: falling back to {} hides data loss, so
            # leave an ERROR trail with the path and preserve the original file.
            log_exception(
                "session_map_load_error",
                module=__name__,
                level=logging.ERROR,
                path=str(SESSION_DB),
            )
            _backup_corrupt_session_db()
            return {}
    return {}


async def save_session_map_atomic(data: dict[str, Any]) -> None:
    """Thread-safe, crash-safe save of session map. Invalidates cache."""
    global _session_map_cache, _session_map_cache_time
    loop = asyncio.get_running_loop()
    async with session_lock:
        def _write() -> None:
            SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
            tmp = SESSION_DB.with_suffix('.tmp')
            try:
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(SESSION_DB)  # atomic on most filesystems
            except Exception:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise
        await loop.run_in_executor(None, _write)
        _session_map_cache = data.copy()
        _session_map_cache_time = time.time()


async def load_session_map_safe() -> dict[str, Any]:
    """Load session map with lock protection and TTL cache."""
    global _session_map_cache, _session_map_cache_time
    now = time.time()
    if _session_map_cache is not None and (now - _session_map_cache_time) < SESSION_MAP_CACHE_TTL:
        return _session_map_cache.copy()
    loop = asyncio.get_running_loop()
    async with session_lock:
        _session_map_cache = await loop.run_in_executor(None, load_session_map)
        _session_map_cache_time = now
        return _session_map_cache.copy()


def parse_opencode_session_list(output: str) -> list[dict[str, Any]]:
    """Parse 'opencode session list' output into list of {id, title, updated}."""
    sessions = []
    for line in output.strip().split('\n'):
        # Format: ses_XXX  Title (with spaces)   HH:MM
        # or:      ses_XXX  Title                HH:MM · DD/M/YYYY
        match = re.match(r'(ses_\w+)\s{2,}(.+?)\s{2,}(\d{2}:\d{2}(?:\s·\s\d{1,2}/\d{1,2}/\d{4})?)', line)
        if match:
            sessions.append({
                "id": match.group(1),
                "title": match.group(2).strip(),
                "updated": match.group(3).strip(),
            })
    # O5: Warn if parser returned nothing but input had content (format may have changed)
    if not sessions and output.strip():
        logger.warning(
            "OpenCode session list parse empty",
            extra={
                "event": "oc_session_parse_empty",
                "output_len": len(output.strip()),
            },
        )
    return sessions


async def fetch_opencode_sessions() -> list[dict[str, Any]]:
    """Run 'opencode session list' and parse output. Cached with TTL (P4)."""
    global _opencode_sessions_cache, _opencode_sessions_cache_time
    now = time.time()
    if _opencode_sessions_cache is not None and (now - _opencode_sessions_cache_time) < SESSION_LIST_CACHE_TTL:
        return _opencode_sessions_cache

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [OPENCODE_CMD, "session", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=INTERNAL_SUBPROCESS_TIMEOUT,
            )
        )

        if result.returncode != 0:
            logger.warning(
                "OpenCode session list failed",
                extra={
                    "event": "oc_session_list_failed",
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                },
            )
            return []

        sessions = parse_opencode_session_list(result.stdout)
        _opencode_sessions_cache = sessions
        _opencode_sessions_cache_time = now
        return sessions
    except subprocess.TimeoutExpired:
        logger.warning(
            "OpenCode session list timed out",
            extra={
                "event": "oc_session_list_timeout",
                "timeout_s": INTERNAL_SUBPROCESS_TIMEOUT,
            },
        )
        return []
    except Exception as e:
        logger.error(
            "Failed to fetch OpenCode sessions",
            extra={
                "event": "oc_session_fetch_error",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return []


# --- Public API ---


async def get_chat_sessions(chat_id: int) -> dict[str, Any]:
    """Get session data for a chat. Returns {active, sessions: {name: {...}}}."""
    smap = await load_session_map_safe()
    result: dict[str, Any] = smap.get(str(chat_id), {})
    return result


async def get_active_session_id(chat_id: int) -> str | None:
    """Get the real OpenCode session ID for the active session of a chat."""
    data = await get_chat_sessions(chat_id)
    active_name = data.get("active", DEFAULT_SESSION_NAME)
    real_id: str | None = data.get("sessions", {}).get(active_name, {}).get("id")
    return real_id


async def get_model(chat_id: int, default: str) -> str:
    """Get the model preference for a chat."""
    data = await get_chat_sessions(chat_id)
    model: str = data.get("model", default)
    return model


async def set_model(chat_id: int, model: str) -> None:
    """Set the model preference for a chat."""
    smap = await load_session_map_safe()
    smap.setdefault(str(chat_id), {})["model"] = model
    await save_session_map_atomic(smap)


async def update_session(chat_id: int, session_name: str, **fields: Any) -> None:
    """Update fields of a named session."""
    smap = await load_session_map_safe()
    sessions = smap.setdefault(str(chat_id), {}).setdefault("sessions", {})
    if session_name in sessions:
        sessions[session_name].update(fields)
    await save_session_map_atomic(smap)


async def add_session(chat_id: int, session_name: str, real_id: str | None = None, title: str = "") -> None:
    """Add a new named session for a chat."""
    smap = await load_session_map_safe()
    chat_data = smap.setdefault(str(chat_id), {})
    sessions = chat_data.setdefault("sessions", {})
    from datetime import datetime, timezone
    sessions[session_name] = {
        "id": real_id,
        "title": title or session_name,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_used": None,
        "prompt_count": 0,
    }
    chat_data["active"] = session_name
    await save_session_map_atomic(smap)


async def set_active_session(chat_id: int, name: str) -> bool:
    """Switch active session. Returns False if name doesn't exist."""
    smap = await load_session_map_safe()
    chat_data = smap.get(str(chat_id), {})
    if name not in chat_data.get("sessions", {}):
        return False
    chat_data["active"] = name
    await save_session_map_atomic(smap)
    return True


async def delete_session(chat_id: int, name: str) -> str | None:
    """Delete a session. Returns the real_id if it had one, else None."""
    smap = await load_session_map_safe()
    chat_data = smap.get(str(chat_id), {})
    sessions = chat_data.get("sessions", {})
    if name not in sessions:
        return None
    real_id: str | None = sessions[name].get("id")
    del sessions[name]
    if chat_data.get("active") == name:
        chat_data["active"] = DEFAULT_SESSION_NAME
    await save_session_map_atomic(smap)
    return real_id
