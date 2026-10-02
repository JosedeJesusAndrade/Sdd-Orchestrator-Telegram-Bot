"""Configuration for Telegram-OpenCode Bridge bot."""
import os
import sys
import logging
import logging.handlers
from pathlib import Path
from dotenv import load_dotenv

# ─── Paths ───
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

# ─── Logging ───
# This bootstrap block is intentionally defined ABOVE the ALLOWED_CHAT_IDS
# parse so the logger exists before the fail-fast check below. Without the
# move, a logger.critical() at import time would be silently dropped (no
# handlers attached yet → only logging.lastResort → stderr). SAFE: nothing
# between here and the parse reads LOG_DIR/LOG_FILE/START_TIME, and the
# deferred `from utils.logging import ...` in setup_logger() is stdlib-only
# (no circular import).
LOG_DIR = Path(__file__).resolve().parent
# Env override (BOT_LOG_FILE) lets tests/ETC redirect the log elsewhere;
# the default stays the production bot.log.
LOG_FILE = Path(os.getenv("BOT_LOG_FILE", LOG_DIR / "bot.log"))
START_TIME = None  # set at startup

# Swallowed-exception logging verbosity — THE one knob controlling every
# log_exception() record (namespace `opencode_bot.swallowed.*`). Set it to
# logging.WARNING to silence all recovered-exception traces at once; DEBUG
# (default) keeps them visible in development. Deliberately a module constant
# (no env override): one typed, greppable place to flip.
#
# END-TO-END: this knob only works because the handlers below are set to DEBUG
# (they never filter). Verbosity is controlled by LOGGERS, not handlers — the
# `opencode_bot` logger at INFO sets normal verbosity, and this knob controls
# swallowed verbosity. If a handler were set to INFO it would drop DEBUG
# records regardless of this value (the #358 gotcha).
SWALLOWED_LOG_LEVEL = logging.DEBUG


def setup_logger() -> logging.Logger:
    """Configure rotating file + console logger with structured key=value format.

    Why two different handlers?
      - Console: minimal prefix (timestamp + level + message). Interactive
        use — no need to grep by request_id, just scan the terminal.
      - File: includes [req=...] for correlation and full logger name for
        forensic search via `grep event=session_created bot.log`.

    Both handlers use KeyValueFormatter to render extra={} fields as
    `key=value` pairs before the message. See utils/logging.py for details.
    """
    logger = logging.getLogger("opencode_bot")
    logger.setLevel(logging.INFO)

    # Import from utils here to avoid circular import (config is loaded
    # very early, before utils may be fully initialized).
    from utils.logging import KeyValueFormatter, RequestIdFilter

    # ── Console handler (no request_id — too noisy for interactive use) ──
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_fmt = KeyValueFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # ── File handler (with request_id for forensic correlation) ──
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(RequestIdFilter())
    file_fmt = KeyValueFormatter(
        "%(asctime)s [%(levelname)s] [req=%(request_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    # One-knob switch for swallowed-exception logging (see SWALLOWED_LOG_LEVEL).
    # Children (`opencode_bot.swallowed.<module>`) inherit this level.
    logging.getLogger("opencode_bot.swallowed").setLevel(SWALLOWED_LOG_LEVEL)

    return logger


logger = setup_logger()

# ─── Telegram ───
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")


def parse_allowed_chat_ids(raw: str) -> list[int]:
    """Parse a CSV of chat IDs into a list of ints.

    Pure and side-effect free so the contract is unit-testable without
    importing/initializing the whole bot. Raises ValueError on any
    non-numeric entry; the caller turns that into a fatal SystemExit.
    """
    return [int(cid.strip()) for cid in raw.split(",") if cid.strip()]


try:
    ALLOWED_CHAT_IDS = parse_allowed_chat_ids(ALLOWED_CHAT_IDS_RAW)
    if not ALLOWED_CHAT_IDS:
        # An empty allow-list is a fatal misconfiguration: the bot would start
        # and reject every chat silently. Fail fast alongside the parse error.
        raise ValueError("ALLOWED_CHAT_IDS produced an empty allow-list")
except ValueError as e:
    logger.critical(
        "ALLOWED_CHAT_IDS inválido: se esperaba una lista CSV de enteros "
        "(ej. '123456789,987654321'). Valor recibido: %r. El bot no puede "
        "iniciar sin una allow-list válida.",
        ALLOWED_CHAT_IDS_RAW,
        extra={
            "event": "config_allowed_chats_parse_error",
            "raw": ALLOWED_CHAT_IDS_RAW,
        },
        exc_info=True,
    )
    raise SystemExit(1) from e

# ─── OpenCode ───
OPENCODE_WORKDIR = os.getenv("OPENCODE_WORKDIR", str(BASE_DIR))
OPENCODE_TIMEOUT = int(os.getenv("OPENCODE_TIMEOUT", "300"))


def resolve_opencode_cmd() -> str:
    """Find opencode executable."""
    import shutil
    known_npm_path = os.path.expanduser(r"~\AppData\Roaming\npm\opencode.cmd")
    candidates = [
        ("OPENCODE_CMD env var", os.getenv("OPENCODE_CMD")),
        ("shutil.which('opencode')", shutil.which("opencode")),
        ("shutil.which('opencode.cmd')", shutil.which("opencode.cmd")),
        ("known npm path", known_npm_path),
    ]
    for source, path in candidates:
        if path and os.path.isfile(path):
            return path
    return "opencode"


OPENCODE_CMD = os.getenv("OPENCODE_CMD") or resolve_opencode_cmd()

# ─── OpenAI ───
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ─── Models ───
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
MODEL_ALIASES = {
    "deepseek": {
        "pro": "deepseek/deepseek-v4-pro",
        "flash": "deepseek/deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    },
    "minimax": {
        "m3": "minimax/MiniMax-M3",
        "m27": "minimax/MiniMax-M2.7",
        "m27-fast": "minimax/MiniMax-M2.7-highspeed",
        "minimax-m3": "minimax/MiniMax-M3",
        "minimax-m27": "minimax/MiniMax-M2.7",
        "minimax-m27-highspeed": "minimax/MiniMax-M2.7-highspeed",
    },
}

def resolve_model(alias_or_full: str) -> str:
    for provider, aliases in MODEL_ALIASES.items():
        if alias_or_full in aliases:
            return aliases[alias_or_full]
    return alias_or_full

# ─── Sessions ───
SESSION_DB = Path(__file__).resolve().parent / "sessions.json"
SESSIONS_PATH = SESSION_DB  # alias for SessionStore

# ── Constants  ──────────────────────────────────────
DEFAULT_SESSION_NAME = "default"
TELEGRAM_MAX_MESSAGE_LENGTH = 4000
PROGRESS_UPDATE_INTERVAL = 5       # seconds between "procesando..." updates
CONNECTIVITY_CHECK_INTERVAL = 30   # seconds between connectivity pings
CONNECTIVITY_FIRST_CHECK_DELAY = 10  # seconds before first connectivity check
INTERNAL_SUBPROCESS_TIMEOUT = 10   # seconds for opencode session list / db queries

# ── DI container key (used by AppContainer injection) ───────────────────
CONTAINER_KEY = "container"
