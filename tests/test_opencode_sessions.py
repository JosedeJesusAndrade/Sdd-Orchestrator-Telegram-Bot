"""Tests for the OpenCode history implementation.

Two layers:
  - A real tmp sqlite DB executes the SQL builders (schema subset from the
    verified OpenCode 1.19.1 investigation).
  - A fake ``query_opencode_db`` exercises validation, escaping, clamping,
    row→DTO mapping, and error propagation without an OpenCode install.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from opencode.client import OpenCodeDBError, _normalize_sql, query_opencode_db
from opencode.sessions import (
    HISTORY_QUERY_TIMEOUT,
    OpenCodeSessions,
    _build_get_session_sql,
    _build_list_sql,
    _build_messages_sql,
    _rows_to_messages,
    _sql_quote,
)
from services.agent_sessions import (
    ConversationSummary,
    InvalidSessionIdError,
    Message,
    MessagePart,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    parent_id TEXT,
    directory TEXT,
    title TEXT,
    agent TEXT,
    model TEXT,
    time_created INTEGER,
    time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);
"""


def _execute(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _insert_session(
    conn: sqlite3.Connection,
    *,
    real_id: str,
    title: str,
    directory: str = "C:/project",
    parent_id: str | None = None,
    updated: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO session (id, parent_id, directory, title, agent, "
        "time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (real_id, parent_id, directory, title, "build", updated, updated),
    )


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    session_id: str,
    created: int,
    role: str,
    parts: list[tuple[str, str, str]],
) -> None:
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (message_id, session_id, created, created, f'{{"role":"{role}"}}'),
    )
    for part_id, part_type, text in parts:
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                part_id,
                message_id,
                session_id,
                created,
                created,
                f'{{"type":"{part_type}","text":"{text}"}}',
            ),
        )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(tmp_path / "opencode.db")
    conn.executescript(_SCHEMA)

    _insert_session(conn, real_id="ses_root1", title="Root one", updated=3000)
    _insert_session(conn, real_id="ses_root2", title="Root two", updated=5000)
    _insert_session(conn, real_id="ses_child", title="Child", parent_id="ses_root1", updated=9999)
    _insert_session(conn, real_id="ses_other", title="Other", directory="C:/other", updated=8000)

    _insert_message(
        conn,
        message_id="msg_a",
        session_id="ses_root1",
        created=100,
        role="user",
        parts=[("part_a1", "text", "Hello")],
    )
    _insert_message(
        conn,
        message_id="msg_b",
        session_id="ses_root1",
        created=200,
        role="assistant",
        parts=[
            ("part_b1", "text", "First"),
            ("part_b2", "text", "Second"),
            ("part_b3", "tool", "noise"),
        ],
    )
    _insert_message(
        conn,
        message_id="msg_c",
        session_id="ses_root1",
        created=300,
        role="assistant",
        parts=[("part_c1", "tool", "only tools")],
    )
    _insert_message(
        conn,
        message_id="msg_d",
        session_id="ses_root1",
        created=400,
        role="user",
        parts=[("part_d1", "text", "Last")],
    )
    _insert_message(
        conn,
        message_id="msg_e",
        session_id="ses_root2",
        created=500,
        role="user",
        parts=[("part_e1", "text", "Other root")],
    )

    yield conn
    conn.close()


# ── SQL real: list ─────────────────────────────────────────────────────


def test_list_sql_returns_roots_for_the_directory_only(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_list_sql("C:/project", 50))

    assert [row["real_id"] for row in rows] == ["ses_root2", "ses_root1"]
    assert [row["message_count"] for row in rows] == [1, 4]
    assert all(row["directory"] == "C:/project" for row in rows)
    assert all(row["agent"] == "build" for row in rows)


def test_list_sql_respects_the_limit(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_list_sql("C:/project", 1))

    assert [row["real_id"] for row in rows] == ["ses_root2"]


def test_list_sql_uses_forward_slash_directory(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_list_sql(Path("C:/project").as_posix(), 50))

    assert len(rows) == 2


# ── SQL real: messages ─────────────────────────────────────────────────


def test_messages_sql_returns_text_parts_in_ascending_order(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_messages_sql("ses_root1", 50))

    assert [row["message_id"] for row in rows] == ["msg_a", "msg_b", "msg_b", "msg_d"]
    assert [row["part_type"] for row in rows] == ["text", "text", "text", "text"]
    assert [row["text"] for row in rows] == ["Hello", "First", "Second", "Last"]
    assert [row["role"] for row in rows] == ["user", "assistant", "assistant", "user"]


def test_messages_sql_excludes_messages_without_text_parts(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_messages_sql("ses_root1", 50))

    assert all(row["message_id"] != "msg_c" for row in rows)


def test_messages_sql_returns_the_tail_page(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_messages_sql("ses_root1", 2))

    assert [row["message_id"] for row in rows] == ["msg_b", "msg_b", "msg_d"]


def test_messages_sql_before_cursor_is_exclusive(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_messages_sql("ses_root1", 50, before=400))

    assert [row["message_id"] for row in rows] == ["msg_a", "msg_b", "msg_b"]


def test_messages_sql_before_cursor_can_return_a_single_turn(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_messages_sql("ses_root1", 50, before=200))

    assert [row["message_id"] for row in rows] == ["msg_a"]


# ── SQL real: catalog ──────────────────────────────────────────────────


def test_catalog_sql_returns_a_root_session(db: sqlite3.Connection) -> None:
    rows = _execute(db, _build_get_session_sql("ses_root1"))

    assert len(rows) == 1
    assert rows[0]["real_id"] == "ses_root1"
    assert rows[0]["title"] == "Root one"
    assert rows[0]["message_count"] == 4


def test_catalog_sql_excludes_children_and_unknown_ids(db: sqlite3.Connection) -> None:
    assert _execute(db, _build_get_session_sql("ses_child")) == []
    assert _execute(db, _build_get_session_sql("ses_missing")) == []


# ── Plumbing: fake query ───────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, kwargs))
        if self.error is not None:
            raise self.error
        return self.rows


async def test_list_sessions_maps_rows_to_dtos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeQuery(
        [
            {
                "real_id": "ses_abc",
                "title": "Title",
                "created_at": "1000",
                "updated_at": 2000,
                "message_count": "7",
                "directory": tmp_path.as_posix(),
                "agent": "build",
            }
        ]
    )
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    sessions = await OpenCodeSessions(tmp_path).list_sessions()

    assert sessions == [
        ConversationSummary(
            real_id="ses_abc",
            title="Title",
            created_at=1000,
            updated_at=2000,
            message_count=7,
            directory=tmp_path.as_posix(),
            agent="build",
        )
    ]


async def test_list_sessions_uses_as_posix_and_passes_history_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeQuery()
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    await OpenCodeSessions(tmp_path).list_sessions()

    sql, kwargs = fake.calls[0]
    assert f"s.directory = '{tmp_path.as_posix()}'" in sql
    assert "s.parent_id IS NULL" in sql
    assert kwargs == {
        "raise_on_error": True,
        "timeout": HISTORY_QUERY_TIMEOUT,
    }


async def test_list_sessions_clamps_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeQuery()
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)
    sessions = OpenCodeSessions("C:/project")

    await sessions.list_sessions(limit=999)
    await sessions.list_sessions(limit=0)

    assert fake.calls[0][0].endswith("LIMIT 200")
    assert fake.calls[1][0].endswith("LIMIT 1")


async def test_list_sessions_escapes_quotes_in_the_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuery()
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    await OpenCodeSessions("C:/project").list_sessions(directory="C:/O'Brien")

    assert "s.directory = 'C:/O''Brien'" in fake.calls[0][0]
    assert _sql_quote("O'Brien") == "O''Brien"


async def test_get_messages_maps_and_joins_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuery(
        [
            {
                "message_id": "m1",
                "message_time": "100",
                "role": "user",
                "part_id": "p1",
                "part_time": 100,
                "part_type": "text",
                "text": "Hola",
            },
            {
                "message_id": "m1",
                "message_time": 100,
                "role": "user",
                "part_id": "p2",
                "part_time": 101,
                "part_type": "text",
                "text": "   ",
            },
            {
                "message_id": "m2",
                "message_time": 200,
                "role": "assistant",
                "part_id": "p3",
                "part_time": 200,
                "part_type": "text",
                "text": "Uno",
            },
            {
                "message_id": "m2",
                "message_time": 200,
                "role": "assistant",
                "part_id": "p4",
                "part_time": 201,
                "part_type": "text",
                "text": "Dos",
            },
            {
                "message_id": "m3",
                "message_time": 300,
                "role": "assistant",
                "part_id": "p5",
                "part_time": 300,
                "part_type": "text",
                "text": "",
            },
        ]
    )
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    messages = await OpenCodeSessions().get_messages("ses_abc", limit=10, before=999)

    assert messages == [
        Message(
            id="m1",
            role="user",
            text="Hola",
            timestamp=100,
            parts=(MessagePart(type="text", text="Hola"),),
        ),
        Message(
            id="m2",
            role="assistant",
            text="Uno\n\nDos",
            timestamp=200,
            parts=(
                MessagePart(type="text", text="Uno"),
                MessagePart(type="text", text="Dos"),
            ),
        ),
    ]
    sql, kwargs = fake.calls[0]
    assert "m.time_created < 999" in sql
    assert kwargs["raise_on_error"] is True


async def test_get_messages_clamps_limit_and_validates_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuery()
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)
    sessions = OpenCodeSessions()

    await sessions.get_messages("ses_abc", limit=999)

    assert "LIMIT 200" in fake.calls[0][0]
    with pytest.raises(InvalidSessionIdError):
        await sessions.get_messages("not-a-session")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("real_id", ["bad", "ses_", "ses_a-b", "ses_a;drop"])
async def test_get_session_rejects_invalid_ids_without_querying(
    monkeypatch: pytest.MonkeyPatch, real_id: str
) -> None:
    fake = _FakeQuery()
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    with pytest.raises(InvalidSessionIdError):
        await OpenCodeSessions().get_session(real_id)

    assert fake.calls == []


async def test_get_session_returns_none_or_a_dto(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeQuery(
        [
            {
                "real_id": "ses_abc",
                "title": "Title",
                "created_at": 1000,
                "updated_at": 2000,
                "message_count": 3,
                "directory": "C:/project",
                "agent": "build",
            }
        ]
    )
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)
    sessions = OpenCodeSessions()

    summary = await sessions.get_session("ses_abc")

    assert summary == ConversationSummary(
        real_id="ses_abc",
        title="Title",
        created_at=1000,
        updated_at=2000,
        message_count=3,
        directory="C:/project",
        agent="build",
    )

    fake.rows = []
    assert await sessions.get_session("ses_abc") is None


async def test_db_errors_propagate_when_the_caller_opts_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuery(error=OpenCodeDBError("boom"))
    monkeypatch.setattr("opencode.sessions.query_opencode_db", fake)

    with pytest.raises(OpenCodeDBError):
        await OpenCodeSessions().list_sessions()


def test_rows_to_messages_skips_blank_text() -> None:
    messages = _rows_to_messages(
        [
            {
                "message_id": "m1",
                "message_time": 1,
                "role": "user",
                "part_type": "text",
                "text": "ok",
            },
            {
                "message_id": "m1",
                "message_time": 1,
                "role": "user",
                "part_type": "text",
                "text": "",
            },
            {
                "message_id": "m2",
                "message_time": 2,
                "role": "assistant",
                "part_type": "text",
                "text": None,
            },
        ]
    )

    assert [message.id for message in messages] == ["m1"]
    assert messages[0].text == "ok"


# ── SQL normalization (CLI transport) ──────────────────────────────────


def test_normalize_sql_flattens_whitespace_outside_literals() -> None:
    sql = "SELECT 1,\n    'a  b',\n    'it''s'\n-- trailing comment\nFROM t /* block */"

    assert _normalize_sql(sql) == "SELECT 1, 'a  b', 'it''s' FROM t"


def test_messages_sql_has_no_comment_placeholder_and_normalizes_to_one_line() -> None:
    sql = _build_messages_sql("ses_root1", 50)
    normalized = _normalize_sql(sql)

    assert "/*" not in sql
    assert "m.time_created <" not in sql
    assert "\n" not in normalized
    assert _build_messages_sql("ses_root1", 50, before=400).count("/*") == 0

    with_before = _normalize_sql(_build_messages_sql("ses_root1", 50, before=400))
    assert "\n" not in with_before
    assert "m.time_created < 400" in with_before


def test_normalized_messages_sql_still_parses_against_sqlite(db: sqlite3.Connection) -> None:
    rows = _execute(db, _normalize_sql(_build_messages_sql("ses_root1", 50)))

    assert [row["message_id"] for row in rows] == ["msg_a", "msg_b", "msg_b", "msg_d"]


async def test_query_opencode_db_sends_single_line_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr("opencode.client.subprocess.run", fake_run)

    await query_opencode_db("SELECT 1,\n  'a  b'\nFROM t")

    sent = captured["argv"][2]
    assert sent == "SELECT 1, 'a  b' FROM t"
    assert "\n" not in sent


def test_opencode_sessions_module_is_ptb_free() -> None:
    script = (
        "import opencode.sessions, sys; print('PTB_FREE=' + str('telegram' not in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "PTB_FREE=True" in result.stdout
