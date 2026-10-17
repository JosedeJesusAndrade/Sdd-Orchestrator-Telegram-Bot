"""Flet implementations of the ChatView / ProgressHandle ports.

The core (``PromptService``) reaches this module only through the PTB-free
protocols in ``services.chat_view``. Every method is ``async`` and mutates
Flet controls on the single Flet 1.0 asyncio loop: no blocking, no threads.
"""

from __future__ import annotations

import flet as ft

from services.chat_view import ChatView, ProgressHandle


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

    def __init__(self, page: ft.Page, conversation_column: ft.Column) -> None:
        self._page = page
        self._column = conversation_column

    def _append(self, control: ft.Control) -> None:
        self._column.controls.append(control)
        self._page.update()

    async def send(self, text: str) -> bool:
        if not text:
            return False
        try:
            self._append(
                ft.Markdown(
                    value=text,
                    selectable=True,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                )
            )
        except Exception:
            return False
        return True

    async def start_progress(self, text: str) -> ProgressHandle:
        control = ft.Text(value=text, color=ft.Colors.ON_SURFACE_VARIANT)
        self._append(control)
        return FletProgressHandle(self._page, control)
