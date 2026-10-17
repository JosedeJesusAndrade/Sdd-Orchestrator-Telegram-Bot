"""Flet implementations of the ChatView / ProgressHandle ports.

The core (``PromptService``) reaches this module only through the PTB-free
protocols in ``services.chat_view``. Every method is ``async`` and mutates
Flet controls on the single Flet 1.0 asyncio loop: no blocking, no threads.

Presentation lives in ``flet_app.components.message_bubble``; this adapter
only decides *where* rendered turns go (the conversation column).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import flet as ft

from flet_app.components.message_bubble import (
    assistant_message,
    build_markdown,
    user_message,
)
from flet_app.core.design_system import DS
from services.chat_view import ChatView, ProgressHandle

MessageRole = Literal["user", "assistant"]


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
