"""Admin handlers: /test_md, /session_preview.

Architecture change (Week 2):
  Replaced send_telegram_mdv2 import with MessageSender via bot.py.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config import DEFAULT_SESSION_NAME, logger
from persistence.sessions import load_session_map_safe, fetch_opencode_sessions
from utils.logging import mask_chat_id
from handlers import authorized


@authorized
async def test_md_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test command: sends a hardcoded MarkdownV2 message to verify API works."""
    chat_id = update.effective_chat.id

    test_msg = (
        "**bold** _italic_ `inline code`\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "Plain text with get\\_user variable"
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=test_msg,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await update.message.reply_text(
            "Test message sent with MarkdownV2. Check if formatting works."
        )
    except Exception as e:
        logger.warning("MarkdownV2 test failed: %s: %s", type(e).__name__, e)
        await update.message.reply_text(
            "MarkdownV2 test FAILED. Check bot logs for details."
        )
        await context.bot.send_message(chat_id=chat_id, text=test_msg)


@authorized
async def session_preview_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Debug: show raw session state from both sessions.json and OpenCode."""
    chat_id = update.effective_chat.id

    smap = await load_session_map_safe()
    chat_sessions = smap.get(str(chat_id), {})

    lines = ["\U0001f4cb *Sesiones del Bot*"]

    if chat_sessions.get("sessions"):
        active_name = chat_sessions.get("active", DEFAULT_SESSION_NAME)
        for name, info in chat_sessions["sessions"].items():
            marker = "\U0001f7e2" if name == active_name else "\u26aa"
            oc_id = info.get("id", "?")[:20] + "..."
            lines.append(
                "{} *{}* → `{}` (prompts: {})".format(
                    marker, name, oc_id, info.get("prompt_count", 0),
                )
            )
    else:
        lines.append("\u26aa No hay sesiones mapeadas aún")

    lines.append("")
    lines.append("\U0001f4cb *Sesiones OpenCode (raw)*")
    try:
        raw = await fetch_opencode_sessions()
        for s in raw[:10]:
            lines.append(
                "\u2022 `{}...` {}".format(s["id"][:20], s["title"][:40])
            )
    except Exception as e:
        lines.append("\u274c Error: {}".format(e))

    msg = "\n".join(lines)

    # Use MessageSender from bot.py
    import bot
    await bot.message_sender.send_formatted(chat_id, msg)
