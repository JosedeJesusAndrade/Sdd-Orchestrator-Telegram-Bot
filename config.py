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

# ─── Telegram ───
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")

try:
    ALLOWED_CHAT_IDS = [
        int(cid.strip())
        for cid in ALLOWED_CHAT_IDS_RAW.split(",")
        if cid.strip()
    ]
except ValueError:
    ALLOWED_CHAT_IDS = []

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
    "pro": "deepseek/deepseek-v4-pro",
    "flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
}

# ─── Sessions ───
SESSION_DB = Path(__file__).resolve().parent / "sessions.json"
SESSIONS_PATH = SESSION_DB  # alias for SessionStore

# ── Constants (extracted magic values) ──────────────────────────────────────
DEFAULT_SESSION_NAME = "default"
TELEGRAM_MAX_MESSAGE_LENGTH = 4000
PROGRESS_UPDATE_INTERVAL = 5       # seconds between "procesando..." updates
CONNECTIVITY_CHECK_INTERVAL = 30   # seconds between connectivity pings
CONNECTIVITY_FIRST_CHECK_DELAY = 10  # seconds before first connectivity check
INTERNAL_SUBPROCESS_TIMEOUT = 10   # seconds for opencode session list / db queries

# ─── Logging ───
LOG_DIR = Path(__file__).resolve().parent
LOG_FILE = LOG_DIR / "bot.log"
START_TIME = None  # set at startup


def setup_logger() -> logging.Logger:
    """Configure rotating file + console logger."""
    logger = logging.getLogger("opencode_bot")
    logger.setLevel(logging.INFO)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # File handler
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
