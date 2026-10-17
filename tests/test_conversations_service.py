"""Tests for ConversationsService: the history/bot-state composer."""

from __future__ import annotations

from typing import Any, cast

import pytest

from services.agent_sessions import (
    ConversationSummary,
    InvalidSessionIdError,
    Message,
)
from services.conversations_service import ConversationsService
from services.session_service import SessionNotDiscoveredError, SessionService
from services.session_store import SessionInfo, SessionStore


def _summary(real_id: str, title: str = "Title", updated: int = 100) -> ConversationSummary:
    return ConversationSummary(
        real_id=real_id,
        title=title,
        created_at=1,
        updated_at=updated,
        message_count=2,
        directory="C:/project",
        agent="build",
    )


def _session(name: str, real_id: str | None = None, is_active: bool = False) -> SessionInfo:
    return SessionInfo(
        name=name,
        real_id=real_id,
        title=name,
        created="2026-05-19T00:00:00+00:00",
        last_used=None,
        prompt_count=0,
        is_active=is_active,
    )


class FakeAgentSessions:
    def __init__(
        self,
        summaries: list[ConversationSummary] | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        self.summaries = list(summaries or [])
        self.messages_result = list(messages or [])
        self.list_calls: list[dict[str, Any]] = []
        self.message_calls: list[dict[str, Any]] = []

    async def list_sessions(
        self, directory: str | None = None, limit: int = 50
    ) -> list[ConversationSummary]:
        self.list_calls.append({"directory": directory, "limit": limit})
        return list(self.summaries)

    async def get_messages(
        self, real_id: str, limit: int = 50, before: int | None = None
    ) -> list[Message]:
        self.message_calls.append({"real_id": real_id, "limit": limit, "before": before})
        return list(self.messages_result)


class FakeStore:
    def __init__(self, sessions: list[SessionInfo] | None = None) -> None:
        self.sessions = list(sessions or [])
        self.created: list[tuple[Any, str]] = []
        self.updated_ids: list[tuple[Any, str]] = []
        self.updated_titles: list[tuple[Any, str, str]] = []
        self.switched: list[tuple[Any, str]] = []

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


class FakeCatalog:
    def __init__(self, summaries: list[ConversationSummary]) -> None:
        self.by_id = {summary.real_id: summary for summary in summaries}
        self.calls: list[str] = []

    async def get_session(self, real_id: str) -> ConversationSummary | None:
        self.calls.append(real_id)
        return self.by_id.get(real_id)


def _service(
    agent: FakeAgentSessions,
    store: FakeStore,
    *,
    catalog: FakeCatalog | None = None,
) -> ConversationsService:
    session_service = SessionService(cast(SessionStore, store), catalog=catalog)
    return ConversationsService(agent, session_service, cast(SessionStore, store))


# ── list_conversations ─────────────────────────────────────────────────


async def test_list_merges_active_and_adopted_state() -> None:
    agent = FakeAgentSessions([_summary("ses_1"), _summary("ses_2")])
    store = FakeStore(
        [
            _session("mine", "ses_1", is_active=True),
            _session("orphan", "ses_gone"),
        ]
    )

    result = await _service(agent, store).list_conversations(42)

    assert [summary.real_id for summary in result] == ["ses_1", "ses_2"]
    assert result[0].adopted_name == "mine"
    assert result[0].is_active is True
    assert result[1].adopted_name is None
    assert result[1].is_active is False


async def test_list_only_returns_conversations_visible_in_the_agent() -> None:
    agent = FakeAgentSessions([_summary("ses_1")])
    store = FakeStore([_session("only-in-store", "ses_2", is_active=True)])

    result = await _service(agent, store).list_conversations(42)

    assert [summary.real_id for summary in result] == ["ses_1"]
    assert result[0].is_active is False


async def test_list_does_not_use_the_directory_argument() -> None:
    agent = FakeAgentSessions([])

    await _service(agent, FakeStore()).list_conversations(42)

    assert agent.list_calls == [{"directory": None, "limit": 50}]


# ── open ───────────────────────────────────────────────────────────────


async def test_open_switches_when_the_real_id_is_already_adopted() -> None:
    store = FakeStore([_session("mine", "ses_1")])
    agent = FakeAgentSessions()
    catalog = FakeCatalog([_summary("ses_1", title="Ignored")])

    name = await _service(agent, store, catalog=catalog).open(42, "ses_1", "Ignored")

    assert name == "mine"
    assert store.switched == [(42, "mine")]
    assert store.created == []
    assert catalog.calls == []


async def test_open_adopts_with_a_slug_when_new() -> None:
    store = FakeStore()
    agent = FakeAgentSessions()
    catalog = FakeCatalog([_summary("ses_new", title="Saludo desde la app Flet")])

    name = await _service(agent, store, catalog=catalog).open(
        42, "ses_new", "Saludo desde la app Flet"
    )

    assert name == "saludo-desde-la-app-flet"
    assert store.created == [(42, name)]
    assert store.updated_ids == [(42, "ses_new")]
    assert store.updated_titles == [(42, name, "Saludo desde la app Flet")]
    assert catalog.calls == ["ses_new"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Canción de otoño", "cancion-de-otono"),
        ("  Hola, mundo!  ", "hola-mundo"),
        ("a" * 40, "a" * 30),
    ],
)
async def test_open_slugifies_titles(title: str, expected: str) -> None:
    store = FakeStore()
    catalog = FakeCatalog([_summary("ses_new", title=title)])

    name = await _service(FakeAgentSessions(), store, catalog=catalog).open(42, "ses_new", title)

    assert name == expected


async def test_open_falls_back_to_the_id_when_the_title_has_no_ascii() -> None:
    store = FakeStore()
    catalog = FakeCatalog([_summary("ses_ABCDEF", title="¡¡!!")])

    name = await _service(FakeAgentSessions(), store, catalog=catalog).open(
        42, "ses_ABCDEF", "¡¡!!"
    )

    assert name == "ses-abcdef"


async def test_open_resolves_slug_collisions() -> None:
    store = FakeStore(
        [
            _session("saludo", "ses_a"),
            _session("saludo-2", "ses_b"),
        ]
    )
    catalog = FakeCatalog([_summary("ses_c", title="Saludo")])

    name = await _service(FakeAgentSessions(), store, catalog=catalog).open(42, "ses_c", "Saludo")

    assert name == "saludo-3"
    assert store.created == [(42, "saludo-3")]


async def test_open_rejects_invalid_real_ids_before_touching_state() -> None:
    store = FakeStore()
    catalog = FakeCatalog([])

    with pytest.raises(InvalidSessionIdError):
        await _service(FakeAgentSessions(), store, catalog=catalog).open(42, "bad-id", "Title")

    assert store.created == []
    assert store.switched == []
    assert catalog.calls == []


async def test_open_propagates_unknown_catalog_ids() -> None:
    store = FakeStore()
    catalog = FakeCatalog([])

    with pytest.raises(SessionNotDiscoveredError):
        await _service(FakeAgentSessions(), store, catalog=catalog).open(42, "ses_missing", "Title")

    assert store.created == []


# ── messages ───────────────────────────────────────────────────────────


async def test_messages_delegates_arguments_to_the_agent() -> None:
    expected = [Message(id="m1", role="user", text="hi", timestamp=1)]
    agent = FakeAgentSessions(messages=expected)

    result = await _service(agent, FakeStore()).messages("ses_1", limit=5, before=100)

    assert result == expected
    assert agent.message_calls == [{"real_id": "ses_1", "limit": 5, "before": 100}]
