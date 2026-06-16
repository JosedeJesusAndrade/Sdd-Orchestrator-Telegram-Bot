"""Unit tests for SessionStore."""
import pytest
import tempfile
import asyncio
from pathlib import Path
from services.session_store import SessionStore


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test_sessions.json"
        store = SessionStore(path)
        yield store


def test_get_model_default(tmp_store):
    """get_model returns DEFAULT_MODEL for unknown chat."""
    model = asyncio.run(tmp_store.get_model(12345))
    assert model == "deepseek/deepseek-v4-pro"


def test_set_and_get_model(tmp_store):
    """set_model persists and get_model retrieves."""
    asyncio.run(tmp_store.set_model(12345, "test-model"))
    model = asyncio.run(tmp_store.get_model(12345))
    assert model == "test-model"


def test_create_session(tmp_store):
    """create_session adds a named session."""
    session = asyncio.run(tmp_store.create_session(12345, "test-session"))
    assert session.name == "test-session"
    assert session.is_active is True
    assert session.real_id is None
    assert session.prompt_count == 0


def test_list_sessions(tmp_store):
    """list_sessions returns all sessions for a chat."""
    asyncio.run(tmp_store.create_session(12345, "session-a"))
    asyncio.run(tmp_store.create_session(12345, "session-b"))
    sessions = asyncio.run(tmp_store.list_sessions(12345))
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"session-a", "session-b"}


def test_increment_prompt_count(tmp_store):
    """increment_prompt_count increases counter."""
    asyncio.run(tmp_store.create_session(12345, "test"))
    count = asyncio.run(tmp_store.increment_prompt_count(12345))
    assert count == 1
    count = asyncio.run(tmp_store.increment_prompt_count(12345))
    assert count == 2


def test_chat_settings(tmp_store):
    """get/set_chat_setting persists per-chat config."""
    asyncio.run(tmp_store.set_chat_setting(12345, "model", "custom-model"))
    model = asyncio.run(tmp_store.get_chat_setting(12345, "model"))
    assert model == "custom-model"

    # Default returned for unknown key
    timeout = asyncio.run(tmp_store.get_chat_setting(12345, "timeout", 300))
    assert timeout == 300
