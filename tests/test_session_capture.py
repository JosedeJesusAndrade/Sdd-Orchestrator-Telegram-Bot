"""Regression tests for OpenCode session-ID capture.

Root cause: ``execute()`` used the default output format, whose stdout only
contains assistant text â€” never the ``ses_...`` ID. The regex in
``PromptService._capture_session_id`` could therefore never match, so every
bot-created session stayed ``real_id=None`` and each prompt started cold.

Fix: run with ``--format json`` (NDJSON), rebuild the assistant text exactly
as the default format rendered it, and return the ``sessionID`` in
``AIBackendResult.session_id`` so PromptService can persist it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import pytest

from services.ai_backend import AIBackendResult
from services.chat_view import ConversationId
from services.opencode_cli_backend import (
    OpenCodeCLIBackend,
    _parse_json_output,
)
from services.prompt_service import PromptService
from services.session_store import SessionInfo

SID = "ses_eb412dc8fffekyxb7vlcWUaIMH"

# Real lines observed from `opencode run --format json` (1.19.1).
REAL_EVENTS = (
    '{"type":"step_start","timestamp":1792275201682,"sessionID":"'
    + SID
    + '","part":{"id":"prt_1","messageID":"msg_1","sessionID":"'
    + SID
    + '","type":"step-start"}}\n'
    '{"type":"text","timestamp":1792275202384,"sessionID":"'
    + SID
    + '","part":{"id":"prt_2","messageID":"msg_1","sessionID":"'
    + SID
    + '","type":"text","text":"Hola"}}\n'
    '{"type":"step_finish","timestamp":1792275202655,"sessionID":"'
    + SID
    + '","part":{"id":"prt_3","reason":"stop","type":"step-finish"}}\n'
)


# â”€â”€ Parser â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_parse_json_output_extracts_text_and_session_id() -> None:
    text, session_id = _parse_json_output(REAL_EVENTS)

    assert text == "Hola\n"
    assert session_id == SID


def test_parse_json_output_joins_multiple_text_parts() -> None:
    events = (
        '{"type":"text","sessionID":"' + SID + '","part":{"type":"text","text":"PASOUNO"}}\n'
        '{"type":"step_finish","sessionID":"' + SID + '","part":{"type":"step-finish"}}\n'
        '{"type":"text","sessionID":"' + SID + '","part":{"type":"text","text":"PASODOS"}}\n'
    )

    text, session_id = _parse_json_output(events)

    # Default format printed "PASOUNO\nPASODOS\n" (verified against the CLI).
    assert text == "PASOUNO\nPASODOS\n"
    assert session_id == SID


def test_parse_json_output_ignores_non_text_events() -> None:
    events = (
        '{"type":"tool_use","sessionID":"'
        + SID
        + '","part":{"type":"tool","tool":"bash","state":{"output":"hola"}}}\n'
        '{"type":"text","sessionID":"' + SID + '","part":{"type":"text","text":"Listo."}}\n'
    )

    assert _parse_json_output(events) == ("Listo.\n", SID)


def test_parse_json_output_falls_back_to_raw_stdout_on_malformed_input(caplog) -> None:
    raw = "esto no es ndjson\n"

    with caplog.at_level(logging.WARNING, logger="services.opencode_cli_backend"):
        text, session_id = _parse_json_output(raw)

    assert text == raw
    assert session_id is None
    assert any(
        getattr(record, "event", "") == "json_output_parse_failed" for record in caplog.records
    )


def test_parse_json_output_drift_still_recovers_the_ses_id(caplog) -> None:
    """#3: a ``--format json`` drift must not lose session continuity.

    The parser cannot read the NDJSON, but the CLI still prints the ``ses_...``
    id in the raw output, so the rollback adopts it instead of starting cold.
    """
    raw = "opencode drift output\nsesión: ses_deadbeef1234\nlisto\n"

    with caplog.at_level(logging.WARNING, logger="services.opencode_cli_backend"):
        text, session_id = _parse_json_output(raw)

    assert text == raw
    assert session_id == "ses_deadbeef1234"


def test_parse_json_output_uses_the_regex_when_events_omit_the_id() -> None:
    """Parsing succeeded but the event shape has no id: keep continuity."""
    events = (
        '{"type":"text","part":{"type":"text","text":"Hola"},"meta":{"session":"ses_abc12345"}}\n'
    )

    text, session_id = _parse_json_output(events)

    assert text == "Hola\n"
    assert session_id == "ses_abc12345"


def test_parse_json_output_empty_stdout_is_silent(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="services.opencode_cli_backend"):
        assert _parse_json_output("") == ("", None)
    assert caplog.records == []


# â”€â”€ Command builder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return (self._stdout, self._stderr)


class _CaptureExec:
    def __init__(self, stdout: bytes = b"") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout = stdout

    async def __call__(self, *cmd_parts: str, **kwargs: Any) -> _FakeProc:
        self.calls.append(cmd_parts)
        return _FakeProc(stdout=self._stdout)


async def test_command_includes_format_json(monkeypatch) -> None:
    capture = _CaptureExec()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    backend = OpenCodeCLIBackend("opencode", workdir=".")
    await backend.execute(prompt="hola", model="m", session_id=None)

    cmd = capture.calls[0]
    assert cmd[:4] == ("opencode", "run", "--format", "json")


async def test_command_includes_session_when_continuing(monkeypatch) -> None:
    capture = _CaptureExec()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    backend = OpenCodeCLIBackend("opencode", workdir=".")
    await backend.execute(prompt="hola", model="m", session_id=SID)

    cmd = capture.calls[0]
    assert "--continue" in cmd
    assert cmd[cmd.index("--session") + 1] == SID


async def test_execute_returns_parsed_text_and_session(monkeypatch) -> None:
    capture = _CaptureExec(stdout=REAL_EVENTS.encode("utf-8"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    backend = OpenCodeCLIBackend("opencode", workdir=".")
    result = await backend.execute(prompt="hola", model="m", session_id=None)

    assert result.stdout == "Hola\n"
    assert result.session_id == SID
    assert result.returncode == 0


async def test_execute_persists_the_drift_ses_id_through_the_backend(monkeypatch) -> None:
    """End-to-end (#3): drift stdout → the backend still returns the id."""
    drift = f"drift sin ndjson, sesión {SID}\n"
    capture = _CaptureExec(stdout=drift.encode("utf-8"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    backend = OpenCodeCLIBackend("opencode", workdir=".")
    result = await backend.execute(prompt="hola", model="m", session_id=None)

    assert result.stdout == drift
    assert result.session_id == SID


async def test_execute_without_any_ses_id_returns_none(monkeypatch) -> None:
    capture = _CaptureExec(stdout=b"drift sin identificador\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    backend = OpenCodeCLIBackend("opencode", workdir=".")
    result = await backend.execute(prompt="hola", model="m", session_id=None)

    assert result.session_id is None


# â”€â”€ PromptService capture + continuity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _session(real_id: str | None) -> SessionInfo:
    return SessionInfo(
        name="default",
        real_id=real_id,
        title="default",
        created="",
        last_used=None,
        prompt_count=0,
    )


class _RecordingStore:
    def __init__(self, active: SessionInfo | None) -> None:
        self.active = active
        self.updated: list[tuple[ConversationId, str]] = []
        self.increments = 0

    async def get_model(self, conversation_id: ConversationId) -> str:
        return "test/model"

    async def get_active_session(self, conversation_id: ConversationId) -> SessionInfo | None:
        return self.active

    async def get_chat_setting(
        self, conversation_id: ConversationId, key: str, default: Any = None
    ) -> Any:
        return default

    async def increment_prompt_count(self, conversation_id: ConversationId) -> int:
        self.increments += 1
        return self.increments

    async def update_session_id(self, conversation_id: ConversationId, real_id: str) -> None:
        self.updated.append((conversation_id, real_id))
        if self.active is not None:
            self.active = replace(self.active, real_id=real_id)


class _RecordingBackend:
    supports_live_progress = False

    def __init__(self, results: list[AIBackendResult]) -> None:
        self._results = results
        self.session_ids: list[str | None] = []

    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        agent: str | None = None,
        workdir: str | None = None,
        source_label: str = "",
        timeout: float | None = None,
        on_progress: Any = None,
    ) -> AIBackendResult:
        self.session_ids.append(session_id)
        return self._results.pop(0)

    def cancel(self) -> None:
        return None


class _Factory:
    def __init__(self, backend: _RecordingBackend) -> None:
        self._backend = backend

    def create(self, provider: str | None = None, **kwargs: Any) -> _RecordingBackend:
        return self._backend


class _View:
    source_label = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def start_progress(self, text: str) -> _Handle:
        return _Handle()


class _Handle:
    async def update(self, text: str) -> None:
        return None

    async def finish(self, text: str) -> None:
        return None

    async def error(self, text: str) -> None:
        return None


async def test_capture_persists_session_id_when_active_session_has_none() -> None:
    store = _RecordingStore(_session(real_id=None))
    backend = _RecordingBackend([AIBackendResult(stdout="Hola\n", session_id=SID)])
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())

    assert store.updated == [(1, SID)]


async def test_capture_logs_the_full_session_id(caplog: pytest.LogCaptureFixture) -> None:
    """Finding B: the ``ses_`` id must be traceable from ``session_id_captured``.

    It is NOT a secret, so it is logged FULL while the chat id stays masked.
    """
    store = _RecordingStore(_session(real_id=None))
    backend = _RecordingBackend([AIBackendResult(stdout="Hola\n", session_id=SID)])
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    with caplog.at_level(logging.INFO, logger="opencode_bot.services.prompt_service"):
        await service.execute(conversation_id=123456789, prompt_text="hola", view=_View())

    records = [
        record for record in caplog.records if getattr(record, "event", "") == "session_id_captured"
    ]
    assert len(records) == 1
    assert getattr(records[0], "session_id", None) == SID
    assert getattr(records[0], "chat_id_masked", None) != "123456789"


async def test_capture_does_nothing_without_active_session() -> None:
    store = _RecordingStore(active=None)
    backend = _RecordingBackend([AIBackendResult(stdout="Hola\n", session_id=SID)])
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())

    assert store.updated == []


async def test_capture_does_not_overwrite_adopted_session() -> None:
    store = _RecordingStore(_session(real_id="ses_adopted"))
    backend = _RecordingBackend([AIBackendResult(stdout="Hola\n", session_id=SID)])
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())

    assert store.updated == []


async def test_second_prompt_continues_captured_session() -> None:
    store = _RecordingStore(_session(real_id=None))
    backend = _RecordingBackend(
        [
            AIBackendResult(stdout="Hola\n", session_id=SID),
            AIBackendResult(stdout="Sigo aquÃ­\n", session_id=SID),
        ]
    )
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())
    await service.execute(conversation_id=1, prompt_text="Â¿y ahora?", view=_View())

    assert backend.session_ids == [None, SID]
    assert store.updated == [(1, SID)]
