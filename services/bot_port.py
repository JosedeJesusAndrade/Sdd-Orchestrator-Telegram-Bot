"""BotPort Protocol — structural subtyping for message transport.

Any object with these 4 methods IS a BotPort.
Decouples business logic from the Telegram library.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class MessageInfo:
    """Minimal message info — replaces PTB's Message object."""
    chat_id: int
    message_id: int
    text: str


class BotPort(Protocol):
    """Protocol for message transport backends.

    Contract: ``send_message`` is a PURE transport call. It sends exactly
    what it is given and RAISES on failure. Formatting policy (notably the
    MarkdownV2→plain fallback) belongs to the formatting layer
    (``MessageSender``), which owns the retry and calls this transport for
    each attempt. Transports MUST NOT silently rewrite text or switch parse
    mode on failure.
    """

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> MessageInfo:
        """Send one message. Raises on failure — no fallback, no retry."""
        ...
    
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str,
    ) -> MessageInfo | None:
        ...
    
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        ...
    
    async def get_me(self) -> dict[str, Any]:
        ...
