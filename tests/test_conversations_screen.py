"""Tests for the conversations screen, history rendering and menu wiring.

The harness mirrors ``test_flet_render.py``: a ``FakePage`` records updates and
background tasks, and a fake service verifies the exact calls the screen makes.
No real Flet client, no network, no OpenCode.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from typing import cast

import flet as ft

from flet_app.components.top_bar import build_top_bar
from flet_app.flet_chat_view import render_history
from flet_app.screens.chat_screen import build_chat_screen
from flet_app.screens.conversations_screen import (
    ConversationsScreen,
    message_count_label,
    relative_time,
)
from services.agent_sessions import ConversationSummary, Message


class FakePage:
    """Minimal stand-in for ``ft.Page``: records updates and scheduled tasks."""

    def __init__(self) -> None:
        self.update_calls = 0
        self.pending: list[asyncio.Future[object] | object] = []

    def update(self, *controls: ft.Control) -> None:
        self.update_calls += 1

    def run_task(
        self,
        handler: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        self.pending.append(handler(*args, **kwargs))


class FakeService:
    """Duck-typed ``ConversationsService`` with queued results/errors."""

    def __init__(
        self,
        *,
        items: list[ConversationSummary] | None = None,
        messages: list[Message] | None = None,
        list_error: Exception | None = None,
        open_error: Exception | None = None,
        active_name: str = "saludo-desde-la-app-flet",
    ) -> None:
        self.items = items or []
        self.messages_result = messages or []
        self.list_error = list_error
        self.open_error = open_error
        self.active_name = active_name
        self.list_calls: list[str] = []
        self.open_calls: list[tuple[str, str, str]] = []
        self.message_calls: list[tuple[str, int, int | None]] = []

    async def list_conversations(self, conversation_id: str) -> list[ConversationSummary]:
        self.list_calls.append(conversation_id)
        if self.list_error is not None:
            raise self.list_error
        return self.items

    async def open(self, conversation_id: str, real_id: str, title: str) -> str:
        self.open_calls.append((conversation_id, real_id, title))
        if self.open_error is not None:
            raise self.open_error
        return self.active_name

    async def messages(
        self,
        real_id: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[Message]:
        self.message_calls.append((real_id, limit, before))
        return self.messages_result


class GatedService(FakeService):
    """Fake whose list call blocks until the test releases it."""

    def __init__(self, items: list[ConversationSummary]) -> None:
        super().__init__(items=items)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def list_conversations(self, conversation_id: str) -> list[ConversationSummary]:
        self.list_calls.append(conversation_id)
        self.started.set()
        await self.release.wait()
        return self.items


def _summary(
    *,
    real_id: str = "ses_abc123",
    title: str = "Saludo desde la app Flet",
    message_count: int = 3,
    is_active: bool = True,
    adopted_name: str | None = "saludo-desde-la-app-flet",
    updated_at: int | None = None,
) -> ConversationSummary:
    now_ms = int(time.time() * 1000)
    return ConversationSummary(
        real_id=real_id,
        title=title,
        created_at=now_ms - 60_000,
        updated_at=now_ms - 120_000 if updated_at is None else updated_at,
        message_count=message_count,
        directory="C:/Users/marie/Desktop/mono/python/Sdd-Orchestrator-Telegram-Bot",
        agent="opencode",
        is_active=is_active,
        adopted_name=adopted_name,
    )


def _message(role: str, text: str) -> Message:
    return Message(
        id=f"msg_{role}_{abs(hash(text))}",
        role=role,
        text=text,
        timestamp=int(time.time() * 1000),
    )


def _walk(control: object) -> Iterator[object]:
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from _walk(content)
    children = getattr(control, "controls", None)
    if children:
        for child in children:
            yield from _walk(child)


def _texts(control: object) -> list[str]:
    return [c.value for c in _walk(control) if isinstance(c, ft.Text) and c.value is not None]


def _build_screen(
    service: FakeService,
    *,
    on_back: Callable[[], None] | None = None,
    on_open: Callable[[ConversationSummary, list[Message], str], None] | None = None,
) -> tuple[ConversationsScreen, FakePage]:
    page = FakePage()
    screen = ConversationsScreen(
        cast(ft.Page, page),
        service,  # type: ignore[arg-type]
        "flet",
        on_back=on_back,
        on_open=on_open,
    )
    return screen, page


async def test_load_shows_loading_then_renders_the_list() -> None:
    item = _summary()
    service = GatedService([item])
    screen, page = _build_screen(service)

    task = asyncio.create_task(screen.load())
    await service.started.wait()

    assert screen.state == "loading"
    assert "Cargando conversaciones…" in _texts(screen.state_body)

    service.release.set()
    await task

    assert screen.state == "list"
    assert screen.items == [item]
    assert screen.tiles[0].data is item
    assert service.list_calls == ["flet"]

    texts = _texts(screen.state_body)
    assert item.title in texts
    assert any("3 mensajes" in text for text in texts)
    assert item.adopted_name in texts
    assert page.update_calls == 2


async def test_load_renders_the_empty_state() -> None:
    service = FakeService(items=[])
    screen, _ = _build_screen(service)

    await screen.load()

    assert screen.state == "empty"
    assert "No hay conversaciones todavía." in _texts(screen.state_body)


async def test_load_renders_the_error_state_with_retry() -> None:
    service = FakeService(list_error=RuntimeError("la base no responde"))
    screen, _ = _build_screen(service)

    await screen.load()

    assert screen.state == "error"
    texts = _texts(screen.state_body)
    assert "la base no responde" in texts
    assert any(isinstance(c, ft.FilledButton) for c in _walk(screen.state_body))


async def test_tile_tap_opens_the_conversation_and_loads_messages() -> None:
    item = _summary()
    messages = [_message("user", "hola"), _message("assistant", "¿qué tal?")]
    opened: list[tuple[ConversationSummary, list[Message], str]] = []
    service = FakeService(items=[item], messages=messages)
    screen, page = _build_screen(service, on_open=lambda *args: opened.append(args))

    screen.set_items([item])
    tile = cast(ft.Container, screen.tiles[0])
    on_click = cast(Callable[[object], None], tile.on_click)
    on_click(None)

    assert len(page.pending) == 1
    result = await cast(asyncio.Future[object], page.pending[0])

    assert result == (messages, service.active_name)
    assert service.open_calls == [("flet", item.real_id, item.title)]
    assert service.message_calls == [(item.real_id, 50, None)]
    assert opened == [(item, messages, service.active_name)]


async def test_open_error_renders_the_error_state() -> None:
    service = FakeService(open_error=RuntimeError("no se pudo adoptar"))
    screen, _ = _build_screen(service)

    result = await screen.open(_summary())

    assert result is None
    assert screen.state == "error"
    assert "no se pudo adoptar" in _texts(screen.state_body)


def test_render_history_clears_and_fills_the_chat_column() -> None:
    chat = build_chat_screen(model="deepseek/deepseek-flash", workdir=".")
    chat.conversation.controls.append(ft.Text("turno viejo"))

    rendered = render_history(
        chat.conversation,
        [_message("user", "hola"), _message("assistant", "**¿qué tal?**")],
    )

    assert rendered == 2
    assert len(chat.conversation.controls) == 2
    assert isinstance(chat.conversation.controls[0], ft.Row)
    assert isinstance(chat.conversation.controls[1], ft.Container)
    assert "turno viejo" not in _texts(chat.conversation)


def test_render_history_skips_blank_text_and_adds_a_placeholder() -> None:
    chat = build_chat_screen(model="deepseek/deepseek-flash", workdir=".")
    chat.conversation.controls.append(ft.Text("turno viejo"))

    rendered = render_history(
        chat.conversation,
        [_message("user", "   "), _message("assistant", "")],
    )

    assert rendered == 0
    assert len(chat.conversation.controls) == 1
    assert "Esta conversación no tiene mensajes de texto para mostrar." in _texts(chat.conversation)


def test_top_bar_hamburger_invokes_the_menu_callback() -> None:
    calls: list[str] = []
    top_bar = build_top_bar("deepseek/deepseek-flash", on_menu=lambda: calls.append("menu"))

    menu = next(
        c
        for c in _walk(top_bar.control)
        if isinstance(c, ft.IconButton) and c.icon == ft.Icons.MENU
    )
    assert isinstance(menu, ft.IconButton)
    assert menu.tooltip == "Conversaciones"
    cast(Callable[[object], None], menu.on_click)(None)

    assert calls == ["menu"]


def test_relative_time_labels() -> None:
    now = 1_800_000_000_000

    assert relative_time(now - 30_000, now_ms=now) == "ahora"
    assert relative_time(now - 5 * 60_000, now_ms=now) == "hace 5 min"
    assert relative_time(now - 3 * 3_600_000, now_ms=now) == "hace 3 h"
    assert relative_time(now - 2 * 86_400_000, now_ms=now) == "hace 2 d"
    assert relative_time(now - 30 * 86_400_000, now_ms=now).count("/") == 2


def test_message_count_label() -> None:
    assert message_count_label(1) == "1 mensaje"
    assert message_count_label(3) == "3 mensajes"
