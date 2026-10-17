"""Regression tests for the Flet conversation render pipeline (no client).

The Slice 1 screen used to mount the conversation column with
``visible=False`` and reveal it in the SAME page patch that appended the
first message. Some Flet clients never built that subtree, so the prompt
ran to completion while the screen stayed empty (user turn, progress row
and answer all invisible).

These tests pin the invariant that fixes it: the conversation column is
attached and visible from the very first frame, and the view appends every
turn to that exact column and flushes with ``page.update()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import flet as ft

from flet_app.flet_chat_view import FletChatView
from flet_app.screens.chat_screen import ChatScreen, build_chat_screen

_REPO = str(Path(__file__).resolve().parents[1])


class FakePage:
    """Minimal stand-in for ``ft.Page``: records update() calls only."""

    def __init__(self) -> None:
        self.update_calls = 0

    def update(self, *controls: ft.Control) -> None:
        self.update_calls += 1


def _build_view() -> tuple[FletChatView, ChatScreen, FakePage]:
    chat = build_chat_screen(model="deepseek/deepseek-flash", workdir=_REPO)
    page = FakePage()
    view = FletChatView(
        cast(ft.Page, page),
        chat.conversation,
        on_first_message=chat.reveal_conversation,
    )
    return view, chat, page


def test_conversation_is_mounted_visible_before_any_message() -> None:
    """The scrollable column must never start hidden (root-cause regression)."""
    _, chat, _ = _build_view()

    assert chat.conversation_holder.visible is True
    assert chat.conversation_holder.content is chat.conversation
    assert chat.conversation.controls == []
    assert chat.greeting.visible is True


async def test_send_appends_to_the_attached_visible_column() -> None:
    view, chat, page = _build_view()

    sent = await view.send("hola", role="user")

    assert sent is True
    assert len(chat.conversation.controls) == 1
    # The SAME column instance that is attached to the screen received it.
    assert chat.conversation_holder.content is chat.conversation
    assert chat.conversation_holder.visible is True
    assert chat.greeting.visible is False
    assert page.update_calls == 1


async def test_progress_row_is_appended_and_finished_in_place() -> None:
    view, chat, page = _build_view()

    handle = await view.start_progress("\u23f3 OpenCode procesando...")
    assert len(chat.conversation.controls) == 1

    await handle.finish("\u2705 Completado.")

    assert len(chat.conversation.controls) == 1, "progress is edited in place"
    assert page.update_calls == 2
