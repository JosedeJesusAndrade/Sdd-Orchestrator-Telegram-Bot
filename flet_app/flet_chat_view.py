"""Flet implementations of the ChatView / ProgressHandle ports.

The core (``PromptService``) reaches this module only through the PTB-free
protocols in ``services.chat_view``. Every method is ``async`` and mutates
Flet controls on the single Flet 1.0 asyncio loop: no blocking, no threads.

Presentation lives in ``flet_app.components.message_bubble``; this adapter
only decides *where* rendered turns go (the conversation column).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Literal

import flet as ft

from flet_app.components.message_bubble import (
    assistant_message,
    build_markdown,
    user_message,
)
from flet_app.core.design_system import DS
from services.agent_sessions import Message
from services.chat_view import ChatView, ProgressHandle

MessageRole = Literal["user", "assistant"]

_EMPTY_HISTORY_TEXT = "Esta conversación no tiene mensajes de texto para mostrar."


def render_history(column: ft.Column, messages: Iterable[Message]) -> int:
    """Clear ``column`` and render stored messages with the chat atoms.

    ``Message.role == "user"`` gets the user bubble; every other role renders
    as assistant markdown. Blank text is skipped; an all-blank history leaves
    one muted placeholder so the screen is never empty. Returns the number of
    rendered turns.
    """
    column.controls.clear()
    rendered = 0
    for message in messages:
        text = (message.text or "").strip()
        if not text:
            continue
        content = build_markdown(text)
        bubble = user_message(content) if message.role == "user" else assistant_message(content)
        column.controls.append(bubble)
        rendered += 1

    if rendered == 0:
        placeholder = ft.Text(
            value=_EMPTY_HISTORY_TEXT,
            size=DS.typography.small_size,
            color=DS.colors.text_secondary,
            italic=True,
        )
        column.controls.append(assistant_message(placeholder))

    return rendered


class FletProgressHandle(ProgressHandle):
    """In-place progress row backed by one ``ft.Text`` control."""

    def __init__(self, page: ft.Page, control: ft.Text) -> None:
        self._page = page
        self._control = control

    async def _set_text(self, text: str) -> None:
        self._control.value = text
        self._page.update()

    async def update(self, text: str) -> None:
        await self._set_text(text)

    async def finish(self, text: str) -> None:
        await self._set_text(text)

    async def error(self, text: str) -> None:
        await self._set_text(text)


class FletChatView(ChatView):
    """Render conversation turns as Markdown in a scrollable Column."""

    source_label = "📱 Flet"

    def __init__(
        self,
        page: ft.Page,
        conversation_column: ft.Column,
        *,
        on_first_message: Callable[[], None] | None = None,
    ) -> None:
        self._page = page
        self._column = conversation_column
        self._on_first_message = on_first_message
        self._started = False

    def _append(self, control: ft.Control) -> None:
        if not self._started:
            self._started = True
            if self._on_first_message is not None:
                self._on_first_message()
        self._column.controls.append(control)
        self._page.update()

    async def send(self, text: str, *, role: MessageRole = "assistant") -> bool:
        if not text:
            return False
        try:
            content = build_markdown(text)
            bubble = user_message(content) if role == "user" else assistant_message(content)
            self._append(bubble)
        except Exception:
            return False
        return True

    async def start_progress(self, text: str) -> ProgressHandle:
        control = ft.Text(
            value=text,
            size=DS.typography.small_size,
            color=DS.colors.text_secondary,
            italic=True,
        )
        self._append(assistant_message(control))
        return FletProgressHandle(self._page, control)
