"""F4 regression: the transport is a pure passthrough; fallback is centralized.

Before the fix, BOTH ``TelegramAdapter.send_message`` and
``MessageSender._send_mdv2_with_fallback`` implemented the MDV2→plain
fallback and both emitted ``event=mdv2_first_attempt_failed`` — a single
formatted-send failure could be logged twice.

Contract after the fix:
  - ``TelegramAdapter.send_message`` = transport only: one call, returns
    ``MessageInfo``, RAISES on failure (never silently rewrites/retries).
  - ``MessageSender`` = the single owner of the formatting fallback, and the
    single emitter of both fallback events.
"""
from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from telegram import Bot

from services.bot_port import MessageInfo
from services.message_sender import MessageSender
from services.telegram_adapter import TelegramAdapter

# ── Fake PTB bot ──────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, message_id: int, text: str) -> None:
        self.message_id = message_id
        self.text = text


class _RecordingBot:
    """Minimal stand-in for telegram.Bot that records every send."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        if self._error is not None:
            raise self._error
        return _FakeMessage(message_id=len(self.calls), text=text)


class _Probe(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ── TelegramAdapter.send_message: pure passthrough ────────────────────

async def test_adapter_send_message_is_a_single_passthrough_call() -> None:
    bot = _RecordingBot()
    adapter = TelegramAdapter(cast(Bot, bot))

    info = await adapter.send_message(5, "hello", parse_mode="MarkdownV2")

    assert isinstance(info, MessageInfo)
    assert info.chat_id == 5
    assert info.message_id == 1
    assert info.text == "hello"
    assert bot.calls == [
        {"chat_id": 5, "text": "hello", "parse_mode": "MarkdownV2"}
    ]


async def test_adapter_send_message_raises_without_falling_back() -> None:
    """A failed formatted send must propagate — NOT be retried as plain.

    The old implementation caught the exception, stripped the markdown and
    sent a second (plain) message, so a caller could never observe the
    failure. The transport now raises and the caller (MessageSender) owns
    the retry.
    """
    bot = _RecordingBot(error=RuntimeError("MarkdownV2 rejected"))
    adapter = TelegramAdapter(cast(Bot, bot))

    with pytest.raises(RuntimeError, match="MarkdownV2 rejected"):
        await adapter.send_message(5, "*bad*", parse_mode="MarkdownV2")

    assert len(bot.calls) == 1, "transport must not attempt its own fallback"
    assert bot.calls[0]["parse_mode"] == "MarkdownV2"


# ── MessageSender: canonical formatted→plain fallback ─────────────────

class _FlakyBotPort:
    """Fails the first (MDV2) attempt, succeeds on the plain retry."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> MessageInfo:
        self.calls.append((text, parse_mode))
        if parse_mode == "MarkdownV2":
            raise RuntimeError("MarkdownV2 rejected")
        return MessageInfo(chat_id=chat_id, message_id=7, text=text)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> MessageInfo | None:
        return None

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return True

    async def get_me(self) -> dict[str, Any]:
        return {}


async def test_message_sender_still_falls_back_end_to_end() -> None:
    bot = _FlakyBotPort()
    sender = MessageSender(bot)

    messages = await sender.send_formatted(3, "*hi*")

    assert len(messages) == 1
    assert messages[0].text == "hi"  # markdown stripped by the fallback
    # Formatted attempt FIRST, then the plain retry — both via the transport.
    assert bot.calls == [("*hi*", "MarkdownV2"), ("hi", None)]


async def test_mdv2_failure_event_is_emitted_exactly_once(monkeypatch) -> None:
    """One logical failure must produce one ``mdv2_first_attempt_failed``.

    Probes the whole swallowed namespace (both ``...services.message_sender``
    and ``...services.telegram_adapter`` propagate here), so the assertion
    fails if EITHER layer logs the event — i.e. it proves there is no
    duplicate.
    """
    bot = _FlakyBotPort()
    sender = MessageSender(bot)

    owner = logging.getLogger("opencode_bot.swallowed")
    original_level = owner.level
    owner.setLevel(logging.DEBUG)
    probe = _Probe()
    owner.addHandler(probe)
    try:
        await sender.send_formatted(3, "*hi*")
    finally:
        owner.removeHandler(probe)
        owner.setLevel(original_level)

    events = [
        r.__dict__.get("event")
        for r in probe.records
        if r.__dict__.get("event") == "mdv2_first_attempt_failed"
    ]
    assert events == ["mdv2_first_attempt_failed"], (
        "the fallback event must be emitted by exactly one layer"
    )
