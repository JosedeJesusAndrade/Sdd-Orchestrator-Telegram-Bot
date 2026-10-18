"""Tests for ``TelegramPermissionPresenter`` — no live Telegram, no server.

Covers the plain-text render (patterns/command/always), the three-button
keyboard payload, fan-out to every configured chat, message-id bookkeeping,
the resolve edit (outcome + keyboard removal), and graceful edit failures.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from telegram import Bot, InlineKeyboardMarkup

from services.permissions import PermissionDecision, PermissionRequest
from services.telegram_permission_presenter import (
    RESULT_PERMITTED,
    RESULT_PERMITTED_ALWAYS,
    RESULT_REJECTED,
    RESULT_TIMED_OUT,
    TelegramPermissionPresenter,
    build_keyboard,
    format_permission_request,
)


def _request(
    request_id: str = "per_1",
    *,
    patterns: tuple[str, ...] = ("git commit *",),
    metadata: dict[str, Any] | None = None,
    always: tuple[str, ...] = ("git commit *",),
) -> PermissionRequest:
    return PermissionRequest(
        id=request_id,
        session_id="ses_presenter_test",
        permission="bash",
        patterns=patterns,
        metadata=metadata if metadata is not None else {"command": 'git commit -m "x"'},
        always=always,
        message_id="msg_1",
        call_id="call_1",
    )


class _FakeMessage:
    def __init__(self, message_id: int, text: str) -> None:
        self.message_id = message_id
        self.text = text


class _RecordingBot:
    """Minimal PTB Bot double: records sends/edits, can fail selected chats."""

    def __init__(self, *, fail_chats: set[int] | None = None, fail_edit: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self._fail_chats = fail_chats or set()
        self._fail_edit = fail_edit
        self._next_id = 100

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> _FakeMessage:
        if chat_id in self._fail_chats:
            raise RuntimeError(f"chat {chat_id} unreachable")
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return _FakeMessage(self._next_id, text)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> _FakeMessage:
        if self._fail_edit:
            raise RuntimeError("message not found")
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return _FakeMessage(message_id, text)


def _presenter(
    bot: _RecordingBot, chat_ids: list[int] | None = None
) -> TelegramPermissionPresenter:
    return TelegramPermissionPresenter(cast(Bot, bot), chat_ids or [7])


# ── Text rendering ────────────────────────────────────────────────────────


def test_format_includes_action_pattern_command_and_always() -> None:
    text = format_permission_request(_request())

    assert text == (
        "🔐 Permiso requerido\n"
        "\n"
        "Acción: bash\n"
        "Patrón: git commit *\n"
        'Comando: git commit -m "x"\n'
        "Siempre: git commit *"
    )


def test_format_lists_multiple_patterns_and_omits_missing_fields() -> None:
    text = format_permission_request(
        _request(patterns=("git commit *", "git push *"), metadata={}, always=())
    )

    assert "Patrones:\n  • git commit *\n  • git push *" in text
    assert "Comando:" not in text
    assert "Siempre:" not in text


def test_keyboard_has_the_three_callback_payloads() -> None:
    keyboard = build_keyboard("per_42")

    assert len(keyboard.inline_keyboard) == 1
    row = keyboard.inline_keyboard[0]
    assert [button.text for button in row] == ["✅ Permitir", "🔁 Siempre", "❌ Rechazar"]
    assert [button.callback_data for button in row] == [
        "perm:per_42:once",
        "perm:per_42:always",
        "perm:per_42:reject",
    ]


# ── show() ────────────────────────────────────────────────────────────────


async def test_show_sends_plain_text_with_keyboard() -> None:
    bot = _RecordingBot()
    presenter = _presenter(bot)
    request = _request()

    await presenter.show(request)

    assert len(bot.sent) == 1
    call = bot.sent[0]
    assert call["chat_id"] == 7
    assert call["text"] == format_permission_request(request)
    # Plain text on purpose: shell patterns would break MarkdownV2.
    assert call["parse_mode"] is None
    assert isinstance(call["reply_markup"], InlineKeyboardMarkup)
    assert [button.callback_data for button in call["reply_markup"].inline_keyboard[0]] == [
        "perm:per_1:once",
        "perm:per_1:always",
        "perm:per_1:reject",
    ]


async def test_show_fans_out_and_stores_every_message_id() -> None:
    bot = _RecordingBot()
    presenter = _presenter(bot, [1, 2, 3])
    request = _request()

    await presenter.show(request)

    assert [call["chat_id"] for call in bot.sent] == [1, 2, 3]
    assert presenter._sent[request.id] == [
        (1, 101, format_permission_request(request)),
        (2, 102, format_permission_request(request)),
        (3, 103, format_permission_request(request)),
    ]


async def test_show_continues_when_one_chat_fails(caplog: pytest.LogCaptureFixture) -> None:
    bot = _RecordingBot(fail_chats={2})
    presenter = _presenter(bot, [1, 2, 3])

    with caplog.at_level(logging.WARNING):
        await presenter.show(_request())

    assert [call["chat_id"] for call in bot.sent] == [1, 3]
    assert any(
        getattr(record, "event", "") == "permission_prompt_send_failed" for record in caplog.records
    )


# ── resolve() ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (PermissionDecision(reply="once"), RESULT_PERMITTED),
        (PermissionDecision(reply="always"), RESULT_PERMITTED_ALWAYS),
        (PermissionDecision(reply="reject"), RESULT_REJECTED),
        (
            PermissionDecision(reply="reject", message="timeout", timed_out=True),
            RESULT_TIMED_OUT,
        ),
    ],
)
async def test_resolve_edits_message_and_removes_keyboard(
    decision: PermissionDecision, expected: str
) -> None:
    bot = _RecordingBot()
    presenter = _presenter(bot, [1, 2])
    request = _request()

    await presenter.show(request)
    original_text = format_permission_request(request)
    await presenter.resolve(request.id, decision)

    assert len(bot.edits) == 2
    for edit, chat_id in zip(bot.edits, (1, 2), strict=True):
        assert edit["chat_id"] == chat_id
        assert edit["text"] == f"{original_text}\n\n{expected}"
        assert isinstance(edit["reply_markup"], InlineKeyboardMarkup)
        assert len(edit["reply_markup"].inline_keyboard) == 0

    # Consumed: a second resolve (e.g. timeout after a click) is a no-op.
    assert request.id not in presenter._sent


async def test_resolve_unknown_request_is_a_noop() -> None:
    bot = _RecordingBot()
    presenter = _presenter(bot)

    await presenter.resolve("per_unknown", PermissionDecision(reply="once"))

    assert bot.edits == []


async def test_edit_failure_is_swallowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    bot = _RecordingBot(fail_edit=True)
    presenter = _presenter(bot)
    request = _request()

    await presenter.show(request)
    with caplog.at_level(logging.DEBUG):
        await presenter.resolve(request.id, PermissionDecision(reply="once"))

    assert any(
        getattr(record, "event", "") == "permission_prompt_edit_failed" for record in caplog.records
    )
