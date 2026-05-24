"""
OpenCode Telegram Bot Bridge
Runs as a daemon on Windows, receives Telegram prompts and executes
them via OpenCode CLI. All MCPs and SDD orchestrator are available.
Supports model switching (/model), prompt cancellation (/cancel),
and enhanced session status (/status).
"""

import asyncio
import os
import signal
import nest_asyncio

from datetime import datetime, timezone

from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
)

from config import (
    BASE_DIR, BOT_TOKEN, ALLOWED_CHAT_IDS,
    OPENCODE_WORKDIR, OPENCODE_TIMEOUT,
    OPENAI_API_KEY,
    logger,
)
from persistence.sessions import (
    load_session_map_safe, save_session_map_atomic,
    fetch_opencode_sessions,
)
from opencode.client import query_opencode_db

from handlers import current_model

from handlers.messages import handle_message, handle_voice
from handlers.commands import (
    start_command, help_command, status_command,
    model_command, cancel_command, new_command, open_command,
)
from handlers.sessions import session_command
from handlers.admin import test_md_command, session_preview_command


def build_application() -> Application:
    """Build and configure the Application without running it."""
    logger.info("Starting OpenCode Telegram Bot Bridge")
    logger.info("Workdir: %s", OPENCODE_WORKDIR)
    logger.info("Timeout: %ds", OPENCODE_TIMEOUT)
    logger.info("Allowed chats: %d", len(ALLOWED_CHAT_IDS))

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("session_preview", session_preview_command))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("open", open_command))
    application.add_handler(CommandHandler("test_md", test_md_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    if not OPENAI_API_KEY:
        logger.warning("Voice handler registered but OPENAI_API_KEY is not set — "
                       "voice transcription will be skipped.")

    return application


async def run_bot() -> None:
    """Run the bot with proper signal handling for clean shutdown."""
    app = build_application()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Discovering existing OpenCode sessions...")
    sessions = await fetch_opencode_sessions()
    logger.info("Found %d existing OpenCode sessions", len(sessions))
    for s in sessions[:5]:
        logger.info("  %s | %s | %s", s["id"], s["title"][:50], s["updated"])

    if os.getenv("EXPLORE_DB", "").lower() in ("1", "true", "yes"):
        logger.info("Exploring OpenCode DB schema...")
        try:
            tables = await query_opencode_db("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            logger.info(f"OpenCode DB tables: {[t.get('name', '?') for t in tables]}")
            for table in tables:
                tname = table.get("name", "")
                if tname:
                    cols = await query_opencode_db(f"PRAGMA table_info({tname})", allowed_pattern=r'^[a-zA-Z0-9_]+$')
                    col_names = [c.get("name", "?") for c in cols]
                    logger.info(f"  {tname}: {', '.join(col_names)}")
        except Exception as e:
            logger.info(f"DB schema exploration failed: {e}")
    else:
        logger.info("DB schema exploration skipped (set EXPLORE_DB=1 to enable)")

    smap = await load_session_map_safe()
    for cid_str, data in smap.items():
        if "model" in data:
            current_model[int(cid_str)] = data["model"]
    logger.info(f"Restored model preferences for {len(current_model)} chats from sessions.json")

    logger.info("Bot is running. Press Ctrl+C to stop.")

    stop_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Received shutdown signal...")
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda s, f: signal_handler())
        signal.signal(signal.SIGTERM, lambda s, f: signal_handler())

    await stop_event.wait()

    logger.info("Persisting model preferences before shutdown...")
    smap = await load_session_map_safe()
    for cid, model in current_model.items():
        smap.setdefault(str(cid), {})["model"] = model
    await save_session_map_atomic(smap)
    logger.info("Model preferences persisted")

    logger.info("Shutting down gracefully...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    nest_asyncio.apply()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(run_bot())
