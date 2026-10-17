"""OpenCode implementation of the read-only agent-session seam.

All queries go through ``query_opencode_db`` (the tested CLI adapter). SQL is
interpolated because the CLI offers no parameter binding: every ``real_id`` is
validated with :func:`is_valid_real_id` and every interpolated string is
quote-escaped with the SQL-standard ``''`` rule. ``allowed_pattern`` is NOT used
because its guard cannot see aliased predicates (``s.directory``, ``m.session_id``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import BASE_DIR
from opencode.client import query_opencode_db
from services.agent_sessions import (
    ConversationSummary,
    InvalidSessionIdError,
    Message,
    MessagePart,
    is_valid_real_id,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
HISTORY_QUERY_TIMEOUT = 20.0

# ── SQL builders (pure; exercised directly against a tmp sqlite DB) ────


def _sql_quote(value: str) -> str:
    """Escape a literal for single-quoted SQL interpolation."""
    return value.replace("'", "''")


def _clamp_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, limit))


def _build_list_sql(directory: str, limit: int = DEFAULT_LIMIT) -> str:
    return (
        "SELECT s.id AS real_id, "
        "s.title AS title, "
        "s.time_created AS created_at, "
        "s.time_updated AS updated_at, "
        "(SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS message_count, "
        "s.directory AS directory, "
        "s.agent AS agent "
        "FROM session s "
        "WHERE s.parent_id IS NULL "
        f"AND s.directory = '{_sql_quote(directory)}' "
        "ORDER BY s.time_updated DESC "
        f"LIMIT {_clamp_limit(limit)}"
    )


def _build_messages_sql(real_id: str, limit: int = DEFAULT_LIMIT, before: int | None = None) -> str:
    before_clause = f"\n      AND m.time_created < {int(before)}" if before is not None else ""
    return (
        "WITH page AS (\n"
        "    SELECT m.id, m.time_created, m.data\n"
        "    FROM message m\n"
        f"    WHERE m.session_id = '{_sql_quote(real_id)}'\n"
        "      AND EXISTS (\n"
        "          SELECT 1 FROM part tp\n"
        "          WHERE tp.message_id = m.id\n"
        "            AND json_extract(tp.data, '$.type') = 'text'\n"
        "      )"
        f"{before_clause}\n"
        "    ORDER BY m.time_created DESC, m.id DESC\n"
        f"    LIMIT {_clamp_limit(limit)}\n"
        ")\n"
        "SELECT page.id AS message_id,\n"
        "       page.time_created AS message_time,\n"
        "       json_extract(page.data, '$.role') AS role,\n"
        "       p.id AS part_id,\n"
        "       p.time_created AS part_time,\n"
        "       json_extract(p.data, '$.type') AS part_type,\n"
        "       json_extract(p.data, '$.text') AS text\n"
        "FROM page\n"
        "JOIN part p ON p.message_id = page.id\n"
        "WHERE json_extract(p.data, '$.type') = 'text'\n"
        "ORDER BY page.time_created, page.id, p.time_created, p.id"
    )


def _build_get_session_sql(real_id: str) -> str:
    return (
        "SELECT s.id AS real_id, "
        "s.title AS title, "
        "s.time_created AS created_at, "
        "s.time_updated AS updated_at, "
        "(SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS message_count, "
        "s.directory AS directory, "
        "s.agent AS agent "
        "FROM session s "
        f"WHERE s.id = '{_sql_quote(real_id)}' "
        "AND s.parent_id IS NULL "
        "LIMIT 1"
    )


# ── Row mapping ────────────────────────────────────────────────────────


def _to_summary(row: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        real_id=str(row.get("real_id") or ""),
        title=str(row.get("title") or ""),
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
        message_count=int(row.get("message_count") or 0),
        directory=str(row.get("directory") or ""),
        agent=str(row.get("agent") or ""),
    )


def _rows_to_messages(rows: list[dict[str, Any]]) -> list[Message]:
    """Group ordered part rows into messages, preserving SQL order."""
    grouped: dict[str, tuple[str, int, list[MessagePart]]] = {}

    for row in rows:
        message_id = row.get("message_id")
        if message_id is None:
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue

        key = str(message_id)
        entry = grouped.get(key)
        if entry is None:
            role = row.get("role")
            timestamp = row.get("message_time")
            entry = (
                str(role) if isinstance(role, str) else "",
                int(timestamp or 0),
                [],
            )
            grouped[key] = entry

        part_type = row.get("part_type")
        entry[2].append(
            MessagePart(
                type=str(part_type) if isinstance(part_type, str) else "text",
                text=text,
            )
        )

    return [
        Message(
            id=message_id,
            role=role,
            text="\n\n".join(part.text for part in parts),
            timestamp=timestamp,
            parts=tuple(parts),
        )
        for message_id, (role, timestamp, parts) in grouped.items()
    ]


# ── Implementation ─────────────────────────────────────────────────────


class OpenCodeSessions:
    """Read-only OpenCode history source backed by ``opencode.db``."""

    def __init__(self, default_directory: str | Path = BASE_DIR) -> None:
        self._default_directory = Path(default_directory)

    def _directory(self, directory: str | None) -> str:
        return Path(directory or self._default_directory).as_posix()

    @staticmethod
    def _validate(real_id: str) -> None:
        if not is_valid_real_id(real_id):
            raise InvalidSessionIdError(real_id)

    async def list_sessions(
        self, directory: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[ConversationSummary]:
        sql = _build_list_sql(self._directory(directory), limit)
        rows = await query_opencode_db(
            sql,
            raise_on_error=True,
            timeout=HISTORY_QUERY_TIMEOUT,
        )
        return [_to_summary(row) for row in rows]

    async def get_messages(
        self, real_id: str, limit: int = DEFAULT_LIMIT, before: int | None = None
    ) -> list[Message]:
        self._validate(real_id)
        sql = _build_messages_sql(real_id, limit, before)
        rows = await query_opencode_db(
            sql,
            raise_on_error=True,
            timeout=HISTORY_QUERY_TIMEOUT,
        )
        return _rows_to_messages(rows)

    async def get_session(self, real_id: str) -> ConversationSummary | None:
        self._validate(real_id)
        sql = _build_get_session_sql(real_id)
        rows = await query_opencode_db(
            sql,
            raise_on_error=True,
            timeout=HISTORY_QUERY_TIMEOUT,
        )
        return _to_summary(rows[0]) if rows else None
