"""MessageSender: unified Telegram message delivery with formatting support.

Architecture rationale:
  Previously, every handler called `send_telegram_mdv2` (formatting/markdown.py)
  directly. That function:
    - Mixed formatting logic (escape/clean) with delivery logic (bot.send_message)
    - Duplicated try/catch fallback patterns across 4+ call sites
    - Had no abstraction for "reply" vs "send" vs "edit"

  MessageSender centralizes ALL message delivery. Handlers no longer know
  about ParseMode, markdown escaping, or fallback logic — they just call
  `sender.send_formatted(chat_id, text)`.

  Why a class (not functions)?
    - Holds the Bot instance, avoiding repeated param passing
    - Testable: inject a mock Bot
    - Single place for logging/error handling policy
"""

from __future__ import annotations

import logging
from telegram import Bot, Update
from telegram.constants import ParseMode
from formatting.markdown import split_message

logger = logging.getLogger(__name__)


class MessageSender:
    """Unified message sending for Telegram.

    Handles MarkdownV2 formatting with automatic plain-text fallback
    and long-message splitting. All handlers use this instead of
    calling context.bot.send_message() directly.
    """

    def __init__(self, bot: Bot) -> None:
        """Initialize with a telegram.Bot instance.

        Args:
            bot: The bot instance from the telegram Application.
        """
        self._bot = bot

    async def send_formatted(self, chat_id: int, text: str) -> list:
        """Send text with MarkdownV2 formatting.

        Auto-splits long messages. Falls back to plain text if MDV2 fails.
        Returns list of sent Message objects (empty if all failed).
        """
        messages = []
        for fragment in split_message(text):
            msg = await self._send_mdv2_with_fallback(chat_id, fragment)
            if msg is not None:
                messages.append(msg)
        return messages

    async def send_plain(self, chat_id: int, text: str):
        """Send a plain text message (no formatting, no escaping)."""
        try:
            return await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("Failed to send plain message to %s: %s", chat_id, e)
            return None

    async def edit_message(self, chat_id: int, message_id: int, text: str):
        """Edit an existing message. Silently deletes on failure.

        Why delete on edit failure? Telegram sometimes rejects edits
        (e.g., no change, message too old). Deleting is better than
        leaving a stale "Processing..." message.
        """
        try:
            return await self._bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
            )
        except Exception:
            try:
                await self._bot.delete_message(
                    chat_id=chat_id, message_id=message_id,
                )
            except Exception:
                pass
            return None

    async def reply_formatted(self, update: Update, text: str) -> list:
        """Reply to the message in the update with formatted text."""
        return await self.send_formatted(update.effective_chat.id, text)

    async def reply_plain(self, update: Update, text: str):
        """Reply to the message in the update with plain text."""
        return await self.send_plain(update.effective_chat.id, text)

    # ── Internal ────────────────────────────────────────────────────

    async def _send_mdv2_with_fallback(self, chat_id: int, text: str):
        """Try MarkdownV2, fall back to stripped plain text.

        Telegram's MarkdownV2 parser is strict — a single unescaped character
        in the wrong place rejects the entire message. Rather than trying to
        perfectly escape everything (impossible with AI-generated output),
        we attempt MDV2 first, then strip all markdown chars and retry plain.

        The stripping is intentionally crude (remove *, `, #, _) because
        at this point we've already failed gracefully — the user still
        gets the content, just without formatting.
        """
        try:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            clean = text.replace('*', '').replace('`', '').replace('#', '').replace('_', '')
            clean = clean.replace('\\', '')
            try:
                return await self._bot.send_message(chat_id=chat_id, text=clean)
            except Exception as e:
                logger.error("MDV2 fallback also failed for %s: %s", chat_id, e)
                return None
