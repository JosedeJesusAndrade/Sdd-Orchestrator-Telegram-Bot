"""BotPort Protocol — structural subtyping for message transport.

Any object with these 4 methods IS a BotPort.
Decouples business logic from the Telegram library.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol


@dataclass
class MessageInfo:
    """Minimal message info — replaces PTB's Message object."""
    chat_id: int
    message_id: int
    text: str


class BotPort(Protocol):
    """Protocol for message transport backends."""
    
    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> MessageInfo:
        ...
    
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str,
    ) -> MessageInfo | None:
        ...
    
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        ...
    
    async def get_me(self) -> dict:
        ...
