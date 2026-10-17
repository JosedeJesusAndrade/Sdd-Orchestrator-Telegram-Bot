"""Tests for the agent-agnostic session-history seam."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from services.agent_sessions import (
    REAL_ID_PATTERN,
    AgentSessions,
    ConversationSummary,
    InvalidSessionIdError,
    Message,
    MessagePart,
    SessionCatalog,
    is_valid_real_id,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Dataclasses ────────────────────────────────────────────────────────


def test_conversation_summary_has_agent_only_defaults() -> None:
    summary = ConversationSummary(
        real_id="ses_abc",
        title="Title",
        created_at=1,
        updated_at=2,
        message_count=3,
        directory="C:/project",
        agent="build",
    )

    assert summary.is_active is False
    assert summary.adopted_name is None


def test_message_parts_default_to_empty_tuple() -> None:
    message = Message(id="msg_1", role="user", text="hi", timestamp=10)

    assert message.parts == ()


def test_history_dataclasses_are_frozen() -> None:
    summary = ConversationSummary(
        real_id="ses_abc",
        title="Title",
        created_at=1,
        updated_at=2,
        message_count=3,
        directory="C:/project",
        agent="build",
    )
    part = MessagePart(type="text", text="hi")

    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.__setattr__("title", "changed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.__setattr__("text", "changed")


# ── Id validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["ses_abc", "ses_ABC123", "ses_a"])
def test_is_valid_real_id_accepts_opencode_ids(value: str) -> None:
    assert is_valid_real_id(value) is True
    assert REAL_ID_PATTERN.match(value) is not None


@pytest.mark.parametrize(
    "value",
    ["", "abc", "ses_", "ses_a-b", "ses_a;drop", "ses_a b", "SES_abc", "ses_abc\n"],
)
def test_is_valid_real_id_rejects_everything_else(value: str) -> None:
    assert is_valid_real_id(value) is False


def test_invalid_session_id_error_carries_the_id() -> None:
    error = InvalidSessionIdError("bad id")

    assert isinstance(error, ValueError)
    assert error.real_id == "bad id"


# ── Protocol conformance ───────────────────────────────────────────────


class _FakeHistory:
    async def list_sessions(
        self, directory: str | None = None, limit: int = 50
    ) -> list[ConversationSummary]:
        return []

    async def get_messages(
        self, real_id: str, limit: int = 50, before: int | None = None
    ) -> list[Message]:
        return []


class _FakeCatalog:
    async def get_session(self, real_id: str) -> ConversationSummary | None:
        return None


def test_fake_history_satisfies_agent_sessions() -> None:
    assert isinstance(_FakeHistory(), AgentSessions)


def test_fake_catalog_satisfies_session_catalog() -> None:
    assert isinstance(_FakeCatalog(), SessionCatalog)


# ── PTB-free guarantee ─────────────────────────────────────────────────


def test_agent_sessions_module_is_ptb_free() -> None:
    script = (
        "import services.agent_sessions, sys; "
        "print('PTB_FREE=' + str('telegram' not in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "PTB_FREE=True" in result.stdout
