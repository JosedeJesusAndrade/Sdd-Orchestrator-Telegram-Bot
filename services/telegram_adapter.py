"""TelegramAdapter — BotPort implemented with python-telegram-bot.

This is the ONLY class in the entire project that imports telegram.Bot.
"""
from __future__ import annotations
import logging
from telegram import Bot
from services.bot_port import MessageInfo
from utils.logging import log_exception


class TelegramAdapter:
    """Wraps telegram.Bot to implement the BotPort protocol."""
    
    def __init__(self, bot: Bot):
        self._bot = bot
    
    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> MessageInfo:
        """Pure transport passthrough — one Telegram call, no formatting policy.

        The transport sends EXACTLY the bytes it is given. Any failure
        propagates as an exception; it is NOT silently retried with a
        different parse mode. The MarkdownV2→plain fallback is owned by
        ``MessageSender`` (the formatting layer), which calls this method
        for both the formatted and the plain attempt.
        """
        msg = await self._bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode,
        )
        return MessageInfo(chat_id=chat_id, message_id=msg.message_id, text=msg.text or text)
    
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str,
    ) -> MessageInfo | None:
        try:
            msg = await self._bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
            )
            return MessageInfo(chat_id=chat_id, message_id=msg.message_id, text=msg.text or text)
        except Exception:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                log_exception(
                    "edit_delete_fallback_failed",
                    module=__name__,
                    level=logging.DEBUG,
                )
            return None
    
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception as e:
            log_exception(
                "message_delete_failed",
                module=__name__,
                level=logging.DEBUG,
                error_type=type(e).__name__,
            )
            return False
    
    async def get_me(self) -> dict:
        user = await self._bot.get_me()
        return {"id": user.id, "username": user.username, "first_name": user.first_name}
