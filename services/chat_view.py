"""ChatView — frontend-agnostic port for conversation delivery.

ETC seam: ``PromptService`` depends on this abstraction, never on Telegram.
Telegram is ONE implementation (``TelegramChatView`` in
``services.telegram_chat_view``); a future Flet frontend can implement the
same Protocol without touching the core.

This module is deliberately PTB-free: it imports only the standard library.
Importing it must NOT put ``telegram`` into ``sys.modules`` — the concrete
Telegram adapter lives in ``services.telegram_chat_view``.

The progress handle is intentionally OPAQUE: the core only knows
"update/finish/error in place" — it never sees a frontend-specific
``message_id`` or chat-edit operation.

Contract:
    ChatView.source_label -> str
        Frontend identity prepended to the engine prompt (e.g. "📱 Telegram").
    ChatView.send(text) -> bool
        Deliver a final message; True if it reached the user.
    ChatView.start_progress(text) -> ProgressHandle
        Open an in-place status placeholder and return its handle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# A conversation is identified by the frontend, not the core.
# Telegram supplies its int chat id; a Flet frontend may supply a
# synthetic ``int | str`` id. The core treats it as opaque.
ConversationId = int | str


@runtime_checkable
class ProgressHandle(Protocol):
    """Opaque handle for an in-place progress message."""

    async def update(self, text: str) -> None:
        """Progress tick — replace the placeholder text."""
        ...

    async def finish(self, text: str) -> None:
        """Success completion — replace the placeholder text."""
        ...

    async def error(self, text: str) -> None:
        """Failure/cancel completion — replace the placeholder text."""
        ...


@runtime_checkable
class ChatView(Protocol):
    """A frontend conversation surface."""

    source_label: str

    async def send(self, text: str) -> bool:
        """Deliver a fire-and-forget final message.

        Returns True if the message was delivered.
        """
        ...

    async def start_progress(self, text: str) -> ProgressHandle:
        """Open an in-place progress placeholder and return its handle."""
        ...
