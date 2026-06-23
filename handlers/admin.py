"""Admin handlers: /test_md, /session_preview.
 
Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module.
  Direct context.bot.send_message() calls replaced with container.message_sender.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from config import DEFAULT_SESSION_NAME, CONTAINER_KEY, logger
from persistence.sessions import load_session_map_safe, fetch_opencode_sessions
from utils.logging import mask_chat_id
from handlers import authorized
from services.container import AppContainer


def _get_container(context) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return context.application.bot_data[CONTAINER_KEY]


@authorized
async def test_md_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test command: sends a hardcoded MarkdownV2 message to verify API works."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    test_msg = (
        "**bold** _italic_ `inline code`\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "Plain text with get\\_user variable"
    )

    msgs = await container.message_sender.send_formatted(chat_id, test_msg)
    if msgs:
        await update.message.reply_text(
            "Test message sent with MarkdownV2. Check if formatting works."
        )
    else:
        logger.warning("MarkdownV2 test failed: send_formatted returned empty")
        await update.message.reply_text(
            "MarkdownV2 test FAILED. Check bot logs for details."
        )
        await container.message_sender.send_plain(chat_id, test_msg)


@authorized
async def session_preview_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Debug: show raw session state from both sessions.json and OpenCode."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

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

    # Use MessageSender from container
    await container.message_sender.send_formatted(chat_id, msg)
