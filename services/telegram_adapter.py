"""TelegramAdapter — BotPort implemented with python-telegram-bot.

This is the ONLY class in the entire project that imports telegram.Bot.
"""
from __future__ import annotations
import logging
from telegram import Bot
from services.bot_port import MessageInfo

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Wraps telegram.Bot to implement the BotPort protocol."""
    
    def __init__(self, bot: Bot):
        self._bot = bot
    
    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> MessageInfo:
        try:
            msg = await self._bot.send_message(
                chat_id=chat_id, text=text, parse_mode=parse_mode,
            )
            return MessageInfo(chat_id=chat_id, message_id=msg.message_id, text=msg.text or text)
        except Exception:
            clean = text.replace('*', '').replace('`', '').replace('#', '').replace('_', '').replace('\\', '')
            try:
                msg = await self._bot.send_message(chat_id=chat_id, text=clean)
                return MessageInfo(chat_id=chat_id, message_id=msg.message_id, text=clean)
            except Exception as e:
                logger.error("send_message fallback failed for %s: %s", chat_id, e)
                raise
    
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
                pass
            return None
    
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False
    
    async def get_me(self) -> dict:
        user = await self._bot.get_me()
        return {"id": user.id, "username": user.username, "first_name": user.first_name}
