"""Unit tests for SessionService (Fase B).

Covers the observable behavior of merge/discover/adopt/delete/enrich plus
the B14 cache-invalidation contract for every mutating operation.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from config import OPENCODE_CMD
from services.session_service import (
    DiscoveredSession,
    InvalidSessionNameError,
    SessionListSnapshot,
    SessionNotDiscoveredError,
    SessionService,
)
from services.session_store import SessionExistsError, SessionInfo, SessionStore


def _session(
    name: str = "default",
    real_id: str | None = None,
    prompt_count: int = 0,
    title: str | None = None,
) -> SessionInfo:
    return SessionInfo(
        name=name,
        real_id=real_id,
        title=title or name,
        created="2026-05-19T00:00:00+00:00",
        last_used=None,
        prompt_count=prompt_count,
    )


class FakeStore:
    """In-memory SessionStore double that records every mutation."""

    def __init__(self, sessions: list[SessionInfo] | None = None) -> None:
        self.sessions = list(sessions or [])
        self.created: list[tuple[Any, str]] = []
        self.updated_ids: list[tuple[Any, str]] = []
        self.updated_titles: list[tuple[Any, str, str]] = []
        self.deleted: list[tuple[Any, str]] = []
        self.switched: list[tuple[Any, str]] = []
        self.delete_real_id: str | None = None

    async def list_sessions(self, conversation_id: Any) -> list[SessionInfo]:
        return list(self.sessions)

    async def create_session(self, conversation_id: Any, name: str) -> SessionInfo:
        info = _session(name)
        self.sessions.append(info)
        self.created.append((conversation_id, name))
        return info

    async def switch_session(self, conversation_id: Any, name: str) -> SessionInfo:
        self.switched.append((conversation_id, name))
        return next(s for s in self.sessions if s.name == name)

    async def update_session_id(self, conversation_id: Any, real_id: str) -> None:
        self.updated_ids.append((conversation_id, real_id))

    async def update_session_title(self, conversation_id: Any, name: str, title: str) -> None:
        self.updated_titles.append((conversation_id, name, title))

    async def delete_session(self, conversation_id: Any, name: str) -> str | None:
        self.deleted.append((conversation_id, name))
        self.sessions = [s for s in self.sessions if s.name != name]
        return self.delete_real_id


def _service(store: FakeStore) -> SessionService:
    return SessionService(cast(SessionStore, store))


@pytest.fixture
def invalidations(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(
        "services.session_service.invalidate_opencode_sessions_cache",
        lambda: calls.append(True),
    )
    return calls


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, sessions: list[dict[str, Any]]) -> list[bool]:
    calls: list[bool] = []

    async def fake_fetch() -> list[dict[str, Any]]:
        calls.append(True)
        return sessions

    monkeypatch.setattr("services.session_service.fetch_opencode_sessions", fake_fetch)
    return calls


# ── list_with_discovered ───────────────────────────────────────────────


async def test_list_with_discovered_merges_stored_and_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([_session("mine", "ses_1", 3), _session("fresh", None)])
    _patch_fetch(
        monkeypatch,
        [
            {"id": "ses_1", "title": "Mine", "updated": "10:00"},
            {"id": "ses_2", "title": "Two", "updated": "11:00"},
            {"id": "ses_9"},
        ],
    )

    snapshot = await _service(store).list_with_discovered(42)

    assert isinstance(snapshot, SessionListSnapshot)
    assert [s.name for s in snapshot.sessions] == ["mine", "fresh"]
    assert snapshot.unadopted == [
        DiscoveredSession(id="ses_2", title="Two", updated="11:00"),
        DiscoveredSession(id="ses_9", title="ses_9"),
    ]


async def test_list_with_discovered_without_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore([_session("mine", "ses_1")])
    _patch_fetch(monkeypatch, [])

    snapshot = await _service(store).list_with_discovered(42)

    assert [s.name for s in snapshot.sessions] == ["mine"]
    assert snapshot.unadopted == []


async def test_list_with_discovered_swallows_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([_session("mine", "ses_1")])

    async def failing_fetch() -> list[dict[str, Any]]:
        raise RuntimeError("opencode missing")

    monkeypatch.setattr("services.session_service.fetch_opencode_sessions", failing_fetch)

    snapshot = await _service(store).list_with_discovered(42)

    assert [s.name for s in snapshot.sessions] == ["mine"]
    assert snapshot.unadopted == []


# ── discover ───────────────────────────────────────────────────────────


async def test_discover_annotates_adoption_status(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore([_session("mine", "ses_1", 7)])
    _patch_fetch(
        monkeypatch,
        [
            {"id": "ses_1", "title": "One", "updated": "10:00"},
            {"id": "ses_2"},
        ],
    )

    discovered = await _service(store).discover(42)

    assert discovered == [
        DiscoveredSession(
            id="ses_1",
            title="One",
            updated="10:00",
            adopted_name="mine",
            prompt_count=7,
        ),
        DiscoveredSession(id="ses_2", title="ses_2", adopted_name=None, prompt_count=0),
    ]


async def test_discover_returns_empty_without_opencode_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, [])

    assert await _service(FakeStore()).discover(42) == []


# ── adopt ──────────────────────────────────────────────────────────────


async def test_adopt_valid_session_updates_store_and_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore()
    _patch_fetch(monkeypatch, [{"id": "ses_abc", "title": "Adopted title"}])

    await _service(store).adopt(42, "ses_abc", "mine")

    assert store.created == [(42, "mine")]
    assert store.updated_ids == [(42, "ses_abc")]
    assert store.updated_titles == [(42, "mine", "Adopted title")]
    assert invalidations == [True]


async def test_adopt_unknown_id_raises_without_store_mutation(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore()
    fetch_calls = _patch_fetch(monkeypatch, [])

    with pytest.raises(SessionNotDiscoveredError) as exc:
        await _service(store).adopt(42, "ses_missing", "mine")

    assert exc.value.real_id == "ses_missing"
    assert fetch_calls == [True]
    assert store.created == []
    assert invalidations == []


@pytest.mark.parametrize("name", ["", "bad name", "bad.name", "x" * 31])
async def test_adopt_invalid_name_raises_before_discovery(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    store = FakeStore()
    fetch_calls = _patch_fetch(monkeypatch, [{"id": "ses_abc", "title": "t"}])

    with pytest.raises(InvalidSessionNameError) as exc:
        await _service(store).adopt(42, "ses_abc", name)

    assert exc.value.name == name
    assert fetch_calls == []
    assert store.created == []


async def test_adopt_duplicate_name_raises_without_mutation(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore([_session("mine", "ses_old")])
    _patch_fetch(monkeypatch, [{"id": "ses_new", "title": "New"}])

    with pytest.raises(SessionExistsError):
        await _service(store).adopt(42, "ses_new", "mine")

    assert store.created == []
    assert store.updated_ids == []
    assert store.updated_titles == []
    assert invalidations == []


# ── delete ─────────────────────────────────────────────────────────────


async def test_delete_removes_session_and_cleans_up_opencode(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore([_session("mine", "ses_1")])
    store.delete_real_id = "ses_1"
    exec_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Proc:
        async def wait(self) -> int:
            return 0

    async def fake_exec(*args: Any, **kwargs: Any) -> _Proc:
        exec_calls.append((args, kwargs))
        return _Proc()

    monkeypatch.setattr("services.session_service.asyncio.create_subprocess_exec", fake_exec)

    result = await _service(store).delete(42, "mine")

    assert result == "ses_1"
    assert store.deleted == [(42, "mine")]
    assert store.sessions == []
    assert invalidations == [True]
    assert len(exec_calls) == 1
    args, kwargs = exec_calls[0]
    assert args == (OPENCODE_CMD, "session", "delete", "ses_1")
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE


async def test_delete_without_real_id_skips_opencode(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore([_session("mine", None)])
    exec_calls: list[tuple[Any, ...]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> None:
        exec_calls.append(args)

    monkeypatch.setattr("services.session_service.asyncio.create_subprocess_exec", fake_exec)

    result = await _service(store).delete(42, "mine")

    assert result is None
    assert store.deleted == [(42, "mine")]
    assert exec_calls == []
    assert invalidations == [True]


async def test_delete_swallows_opencode_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore([_session("mine", "ses_1")])
    store.delete_real_id = "ses_1"

    async def failing_exec(*args: Any, **kwargs: Any) -> None:
        raise OSError("opencode missing")

    monkeypatch.setattr("services.session_service.asyncio.create_subprocess_exec", failing_exec)

    result = await _service(store).delete(42, "mine")

    assert result == "ses_1"
    assert store.deleted == [(42, "mine")]
    assert invalidations == [True]


# ── enrich ─────────────────────────────────────────────────────────────


async def test_enrich_returns_db_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[tuple[str, str | None]] = []

    async def fake_query(sql: str, allowed_pattern: str | None = None) -> list[dict[str, Any]]:
        queries.append((sql, allowed_pattern))
        if "COUNT(*)" in sql:
            return [{"count": 12}]
        return [{"created_at": "2026-05-19T00:00:00Z", "model": "deepseek/flash"}]

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    data = await _service(FakeStore()).enrich("ses_123")

    assert data == {
        "message_count": 12,
        "created_at": "2026-05-19T00:00:00Z",
        "model": "deepseek/flash",
    }
    assert len(queries) == 2
    assert all(pattern is not None for _, pattern in queries)


async def test_enrich_without_db_rows_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_query(sql: str, allowed_pattern: str | None = None) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    assert await _service(FakeStore()).enrich("ses_123") == {}


@pytest.mark.parametrize("real_id", [None, "", "bad id", "bad-id", "drop;table"])
async def test_enrich_skips_ids_that_fail_the_db_pattern(
    monkeypatch: pytest.MonkeyPatch, real_id: str | None
) -> None:
    calls: list[str] = []

    async def fake_query(sql: str, allowed_pattern: str | None = None) -> list[dict[str, Any]]:
        calls.append(sql)
        return []

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    assert await _service(FakeStore()).enrich(real_id) == {}
    assert calls == []


async def test_enrich_swallows_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_query(sql: str, allowed_pattern: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    monkeypatch.setattr("services.session_service.query_opencode_db", failing_query)

    assert await _service(FakeStore()).enrich("ses_123") == {}


# ── create / switch (B14 cache invalidation) ───────────────────────────


async def test_create_delegates_and_invalidates_cache(invalidations: list[bool]) -> None:
    store = FakeStore()

    session = await _service(store).create(42, "new-session")

    assert session.name == "new-session"
    assert store.created == [(42, "new-session")]
    assert invalidations == [True]


async def test_switch_delegates_and_invalidates_cache(invalidations: list[bool]) -> None:
    store = FakeStore([_session("mine", "ses_1"), _session("other", "ses_2")])

    session = await _service(store).switch(42, "other")

    assert session.name == "other"
    assert store.switched == [(42, "other")]
    assert invalidations == [True]
