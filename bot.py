"""
OpenCode Telegram Bot Bridge
Runs as a daemon on Windows, receives Telegram prompts and executes
them via OpenCode CLI. All MCPs and SDD orchestrator are available.
Supports model switching (/model), prompt cancellation (/cancel),
and enhanced session status (/status).
"""

import asyncio
import logging
import os
import signal
import nest_asyncio

from datetime import datetime, timezone

from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

from config import (
    BASE_DIR, BOT_TOKEN, ALLOWED_CHAT_IDS,
    OPENCODE_WORKDIR, OPENCODE_TIMEOUT,
    OPENAI_API_KEY,
    CONNECTIVITY_CHECK_INTERVAL, CONNECTIVITY_FIRST_CHECK_DELAY,
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


# ── Connection state tracking ──────────────────────────────────────────
class ConnectionState:
    """Tracks network connectivity to avoid spamming the terminal."""

    def __init__(self) -> None:
        self._connected: bool = True           # assume connected at startup
        self._consecutive_failures: int = 0
        self._first_failure_at: datetime | None = None

    def mark_failure(self, error: Exception) -> None:
        """Register a network failure and log appropriately."""
        self._consecutive_failures += 1
        if self._connected:
            self._connected = False
            self._first_failure_at = datetime.now(timezone.utc)
            logger.warning(
                "=" * 55
            )
            logger.warning(
                "  ⚠️  CONEXIÓN PERDIDA — sin acceso a internet"
            )
            logger.warning(
                f"  Motivo: {error.__class__.__name__}"
            )
            logger.warning(
                "  El bot reintentará automáticamente cuando vuelva la red."
            )
            logger.warning(
                "  No es necesario reiniciar. 😌"
            )
            logger.warning(
                "=" * 55
            )
        # Every 10th retry, give a quiet ping so you know it's still trying
        elif self._consecutive_failures % 10 == 0:
            elapsed = ""
            if self._first_failure_at:
                delta = datetime.now(timezone.utc) - self._first_failure_at
                mins = int(delta.total_seconds() // 60)
                elapsed = f" (~{mins} min sin conexión)"
            logger.info(
                f"  🔄 Reintentando conexión... "
                f"(intento {self._consecutive_failures}){elapsed}"
            )

    def mark_recovery(self) -> None:
        """Register that connectivity has been restored."""
        if not self._connected:
            elapsed = ""
            if self._first_failure_at:
                delta = datetime.now(timezone.utc) - self._first_failure_at
                mins = int(delta.total_seconds() // 60)
                secs = int(delta.total_seconds() % 60)
                if mins > 0:
                    elapsed = f" (tras {mins} min {secs} s)"
                else:
                    elapsed = f" (tras {secs} s)"
            logger.warning(
                "=" * 55
            )
            logger.warning(
                f"  ✅ CONEXIÓN RESTABLECIDA{elapsed}"
            )
            logger.warning(
                f"  El bot sigue funcionando normalmente."
            )
            logger.warning(
                "=" * 55
            )
            self._connected = True
            self._consecutive_failures = 0
            self._first_failure_at = None


_connection_state = ConnectionState()


class _NetworkErrorFilter(logging.Filter):
    """Drops log records containing NetworkError/ConnectError tracebacks.

    Applied to the telegram.ext logger so that the library's internal
    retry-loop tracebacks never reach the console. Our ConnectionState
    handler shows clean banners instead.
    """

    _DROP_PATTERNS = (
        "NetworkError",
        "ConnectError",
        "getaddrinfo failed",
        "Failed run number",       # retry-loop debug spam
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(p in msg for p in self._DROP_PATTERNS):
            return False
        # Also check exception info
        if record.exc_info and record.exc_info[1] is not None:
            ex_msg = str(record.exc_info[1])
            if any(p in ex_msg for p in ("NetworkError", "ConnectError", "getaddrinfo")):
                return False
        return True


def _suppress_network_tracebacks() -> None:
    """Suppress python-telegram-bot's noisy internal retry-loop logging.

    IMPORTANT: python-telegram-bot v22.7 uses `get_logger(__name__)` which
    transforms module paths (e.g. ``telegram.ext._updater`` →
    ``telegram.ext.Updater``, ``telegram.ext._utils.networkloop`` →
    ``telegram.ext``).  Targeting the raw module names does NOT work.
    """
    nf = _NetworkErrorFilter()

    # ── telegram.ext.Updater: source of "Exception happened while polling" ──
    # This logger writes at ERROR level via logger.exception() in
    # `default_error_callback` (_updater.py:371).  Must be CRITICAL
    # to suppress, but we also add the filter as a safety net.
    te_updater = logging.getLogger("telegram.ext.Updater")
    te_updater.setLevel(logging.CRITICAL)
    te_updater.addFilter(nf)

    # ── telegram.ext: parent logger used by network_retry_loop ──
    # The retry loop writes DEBUG-level "Failed run number … Retrying"
    # messages.  Set to WARNING to silence those while keeping real
    # errors visible.
    te_root = logging.getLogger("telegram.ext")
    te_root.setLevel(logging.WARNING)
    te_root.addFilter(nf)

    # ── httpx / httpcore noise ──
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Error handler ──────────────────────────────────────────────────────
async def error_handler(update: object | None, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — shows clean terminal output for network issues."""

    error = context.error
    if error is None:
        return

    # ── Network errors: clean friendly message ──
    if isinstance(error, (NetworkError, TimedOut)):
        _connection_state.mark_failure(error)
        return  # don't re-raise; python-telegram-bot retries automatically

    # ── Any other error: log full traceback for debugging ──
    logger.error(
        "Exception while handling an update:", exc_info=context.error
    )


# ── Connectivity monitor (background asyncio task, no extra deps) ──
async def _connectivity_monitor(app: Application, stop_event: asyncio.Event) -> None:
    """Background task: periodically pings Telegram to detect connection loss/recovery.

    Uses a simple asyncio loop instead of JobQueue to avoid the
    ``python-telegram-bot[job-queue]`` dependency (APScheduler).
    """
    await asyncio.sleep(CONNECTIVITY_FIRST_CHECK_DELAY)  # first check after 10s
    while not stop_event.is_set():
        try:
            await app.bot.get_me()
            _connection_state.mark_recovery()
        except (NetworkError, TimedOut):
            _connection_state.mark_failure(
                RuntimeError("sin conexión a internet")
            )
        except Exception:
            pass  # ignore other transient errors

        # Wait 30s between checks, checking stop_event every second
        for _ in range(CONNECTIVITY_CHECK_INTERVAL):
            if stop_event.is_set():
                return
            await asyncio.sleep(1)


def build_application() -> Application:
    """Build and configure the Application without running it."""

    # ── Suppress python-telegram-bot's internal traceback spam ──
    # When the network drops, the library's network_retry_loop logs a
    # full traceback on every retry. We handle it cleanly in our error_handler.
    _suppress_network_tracebacks()

    logger.info("Starting OpenCode Telegram Bot Bridge")
    logger.info("Workdir: %s", OPENCODE_WORKDIR)
    logger.info("Timeout: %ds", OPENCODE_TIMEOUT)
    logger.info("Allowed chats: %d", len(ALLOWED_CHAT_IDS))

    application = Application.builder().token(BOT_TOKEN).build()

    # ── Register our clean error handler ──
    application.add_error_handler(error_handler)

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
    import time
    # Set START_TIME as early as possible for /status uptime
    import config
    config.START_TIME = time.time()

    app = build_application()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # ── Shared shutdown event: used by both signal handler and connectivity monitor ──
    stop_event = asyncio.Event()

    # ── Background connectivity monitor (asyncio task, no extra deps) ──
    connectivity_task = asyncio.create_task(
        _connectivity_monitor(app, stop_event),
        name="connectivity_monitor",
    )
    logger.info("Connectivity monitor started (check interval: 30s)")

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
    # Cancel connectivity monitor task
    connectivity_task.cancel()
    try:
        await connectivity_task
    except asyncio.CancelledError:
        pass
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
