"""Unit tests for SessionService (Fase B).

Covers the observable behavior of merge/discover/adopt/delete/enrich plus
the B14 cache-invalidation contract for every mutating operation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

from config import OPENCODE_CMD
from services.agent_sessions import ConversationSummary
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


class FakeCatalog:
    """Catalog double: only the real ids in ``sessions`` exist."""

    def __init__(self, sessions: list[ConversationSummary] | None = None) -> None:
        self.by_id = {s.real_id: s for s in sessions or []}
        self.calls: list[str] = []

    async def get_session(self, real_id: str) -> ConversationSummary | None:
        self.calls.append(real_id)
        return self.by_id.get(real_id)


def _summary(real_id: str, title: str = "Adopted title") -> ConversationSummary:
    return ConversationSummary(
        real_id=real_id,
        title=title,
        created_at=1,
        updated_at=2,
        message_count=0,
        directory="C:/project",
        agent="build",
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


async def test_adopt_with_catalog_accepts_sessions_outside_the_cli_list(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore()
    catalog = FakeCatalog([_summary("ses_outside", title="Older session")])
    fetch_calls = _patch_fetch(monkeypatch, [])

    service = SessionService(cast(SessionStore, store), catalog=catalog)
    await service.adopt(42, "ses_outside", "old-one")

    assert catalog.calls == ["ses_outside"]
    assert fetch_calls == []
    assert store.created == [(42, "old-one")]
    assert store.updated_ids == [(42, "ses_outside")]
    assert store.updated_titles == [(42, "old-one", "Older session")]
    assert invalidations == [True]


async def test_adopt_with_catalog_unknown_id_raises_without_mutation(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore()
    catalog = FakeCatalog([])
    fetch_calls = _patch_fetch(monkeypatch, [])

    service = SessionService(cast(SessionStore, store), catalog=catalog)
    with pytest.raises(SessionNotDiscoveredError) as exc:
        await service.adopt(42, "ses_missing", "mine")

    assert exc.value.real_id == "ses_missing"
    assert catalog.calls == ["ses_missing"]
    assert fetch_calls == []
    assert store.created == []
    assert invalidations == []


async def test_adopt_without_catalog_keeps_the_cli_validation(
    monkeypatch: pytest.MonkeyPatch, invalidations: list[bool]
) -> None:
    store = FakeStore()
    fetch_calls = _patch_fetch(monkeypatch, [{"id": "ses_from_cli", "title": "From CLI"}])

    await _service(store).adopt(42, "ses_from_cli", "mine")

    assert fetch_calls == [True]
    assert store.created == [(42, "mine")]
    assert store.updated_titles == [(42, "mine", "From CLI")]


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


async def test_enrich_uses_real_columns_and_normalizes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, dict[str, Any]]] = []

    async def fake_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        queries.append((sql, kwargs))
        if "COUNT(*)" in sql:
            return [{"count": 12}]
        return [
            {
                "time_created": 1779148800000,
                "model": '{"id":"deepseek-v4-pro","providerID":"deepseek","variant":"max"}',
            }
        ]

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    data = await _service(FakeStore()).enrich("ses_123")

    assert data == {
        "message_count": 12,
        "created_at": "2026-05-19T00:00:00+00:00",
        "model": "deepseek/deepseek-v4-pro",
    }
    assert len(queries) == 2
    session_sql = next(sql for sql, _ in queries if "FROM session" in sql)
    assert "time_created" in session_sql
    assert "created_at" not in session_sql
    assert all(kwargs["raise_on_error"] is True for _, kwargs in queries)


async def test_enrich_without_db_rows_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    assert await _service(FakeStore()).enrich("ses_123") == {}


async def test_enrich_tolerates_a_null_or_unparseable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        if "COUNT(*)" in sql:
            return [{"count": 1}]
        return [{"time_created": None, "model": "not-json"}]

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    data = await _service(FakeStore()).enrich("ses_123")

    assert data == {"message_count": 1, "model": "not-json"}


async def test_enrich_tolerates_a_model_without_provider_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        if "COUNT(*)" in sql:
            return [{"count": 1}]
        return [{"time_created": 0, "model": '{"variant":"max"}'}]

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    data = await _service(FakeStore()).enrich("ses_123")

    assert data == {
        "message_count": 1,
        "created_at": "1970-01-01T00:00:00+00:00",
    }


@pytest.mark.parametrize("real_id", [None, "", "bad id", "bad-id", "drop;table"])
async def test_enrich_skips_ids_that_fail_the_db_pattern(
    monkeypatch: pytest.MonkeyPatch, real_id: str | None
) -> None:
    calls: list[str] = []

    async def fake_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        return []

    monkeypatch.setattr("services.session_service.query_opencode_db", fake_query)

    assert await _service(FakeStore()).enrich(real_id) == {}
    assert calls == []


async def test_enrich_swallows_db_errors_at_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_query(sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    logged: list[dict[str, Any]] = []

    def fake_log(event: str, **fields: Any) -> None:
        logged.append({"event": event, **fields})

    monkeypatch.setattr("services.session_service.query_opencode_db", failing_query)
    monkeypatch.setattr("services.session_service.log_exception", fake_log)

    assert await _service(FakeStore()).enrich("ses_123") == {}
    assert logged == [
        {
            "event": "oc_db_enrich_failed",
            "module": "services.session_service",
            "level": logging.WARNING,
            "real_id": "ses_123",
        }
    ]


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
