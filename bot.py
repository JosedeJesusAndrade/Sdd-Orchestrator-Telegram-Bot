"""
OpenCode Telegram Bot Bridge
Runs as a daemon on Windows, receives Telegram prompts and executes
them via OpenCode CLI. All MCPs and SDD orchestrator are available.
Supports model switching (/model), prompt cancellation (/cancel),
and enhanced session status (/status).
"""

import json
import os
import re
import sys
import time
import asyncio
import shutil
import subprocess
import logging
import logging.handlers
import signal
import nest_asyncio
from pathlib import Path

nest_asyncio.apply()
from datetime import datetime, timezone

from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_DB = Path(__file__).resolve().parent / "sessions.json"
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)


def resolve_opencode_cmd() -> str:
    known_npm_path = r"C:\Users\marie\AppData\Roaming\npm\opencode.cmd"
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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")
OPENCODE_WORKDIR = os.getenv("OPENCODE_WORKDIR", str(BASE_DIR))
OPENCODE_TIMEOUT = int(os.getenv("OPENCODE_TIMEOUT", "300"))
OPENCODE_CMD = os.getenv("OPENCODE_CMD") or resolve_opencode_cmd()
LOG_DIR = Path(__file__).resolve().parent
LOG_FILE = LOG_DIR / "bot.log"

try:
    ALLOWED_CHAT_IDS = [
        int(cid.strip())
        for cid in ALLOWED_CHAT_IDS_RAW.split(",")
        if cid.strip()
    ]
except ValueError:
    ALLOWED_CHAT_IDS = []

START_TIME = time.time()
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"

logger = logging.getLogger("opencode_bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
console_handler.setFormatter(console_fmt)
logger.addHandler(console_handler)

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

logger.info("OpenCode cmd resolved to: %s", OPENCODE_CMD)

if not BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN not set in .env")
    sys.exit(1)

if not ALLOWED_CHAT_IDS:
    logger.warning("ALLOWED_CHAT_IDS is empty — bot will not respond to anyone")

# Track opencode sessions per chat_id
# {chat_id: {"session_name": str, "session_id": str|None, "first_message": datetime, "last_used": datetime, "prompt_count": int}}
active_sessions: dict[int, dict] = {}
SESSION_TIMEOUT_MINUTES = 30

# Per-chat model preference (persists across session resets via /new)
current_model: dict[int, str] = {}

# Track currently running subprocess per chat_id (for /cancel support)
current_process: dict[int, subprocess.Popen] = {}

# Track cancel requests to suppress output after kill
cancel_requests: set[int] = set()


def mask_chat_id(chat_id: int) -> str:
    s = str(chat_id)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _filter_stderr(stderr: str) -> str:
    """Filter stderr to only include meaningful response lines (not metadata)."""
    if not stderr or not stderr.strip():
        return ""
    lines = []
    for line in stderr.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if 'build' in lower and chr(183) in stripped:
            continue
        if stripped.startswith('[INFO]') or stripped.startswith('[DEBUG]'):
            continue
        if stripped.startswith('[WARN'):
            continue
        lines.append(stripped)
    return '\n'.join(lines)


def _remove_tool_traces(text: str) -> str:
    """Remove tool call trace lines (single and multi-line JSON).
    
    Detects lines starting with ⚙ (or its cp1252-mangled form âš™) 
    and skips them plus any multi-line JSON that follows.
    """
    result = []
    in_tool_trace = False
    brace_depth = 0
    
    for line in text.split('\n'):
        stripped = line.strip()
        
        # Detect start of tool trace: ⚙ (U+2699) or âš™ (mangled UTF-8 via cp1252)
        if stripped.startswith('⚙') or stripped.startswith('âš™'):
            in_tool_trace = True
            brace_depth = 0
        
        if in_tool_trace:
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth <= 0 and ('{' in stripped or '}' in stripped):
                in_tool_trace = False
            continue
        
        result.append(line)
    
    return '\n'.join(result)


def clean_opencode_output(text: str) -> str:
    """Remove ANSI escape codes and clean up terminal output for Telegram."""
    # 1. Remove ANSI escape sequences (ESC + CSI codes)
    ansi_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_pattern.sub('', text)

    # 2. Remove build lines (model info like "> build · deepseek-v4-pro")
    text = re.sub(r'^> build .*$', '', text, flags=re.MULTILINE)

    # 3. Remove tool call traces (single and multi-line JSON with ⚙ marker)
    text = _remove_tool_traces(text)

    # 4. Clean up trailing --- separator (leftover when stderr was only tool traces)
    text = re.sub(r'\n*---\s*$', '', text)

    # 5. Replace Unicode symbols with text equivalents
    text = text.replace('\u2731', '\u2192')

    # 6. Collapse multiple blank lines into one
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 7. Split into lines, strip whitespace
    lines = [line.strip() for line in text.split('\n')]

    # 8. Trim everything before first line starting with ">" (opencode logo/header)
    for i, line in enumerate(lines):
        if line.startswith('>'):
            lines = lines[i:]
            break

    # 9. Remove empty lines and filter out noise
    filtered = []
    for line in lines:
        if not line:
            continue
        lower = line.lower()
        if 'auto-rejecting' in lower or 'permission requested' in lower:
            continue
        if 'user rejected permission' in lower:
            continue
        filtered.append(line)

    return '\n'.join(filtered)


def telegramify_markdown(text: str) -> str:
    """Convert markdown to Telegram MarkdownV2 compatible format.

    Telegram MarkdownV2 supports: *bold*, _italic_, __underline__, ~strikethrough~,
    ||spoiler||, `code`, ```pre```, [links](url)

    It does NOT support: tables, HTML, images.
    """
    lines = text.split('\n')
    result = []
    in_table = False
    table_lines = []

    def flush_table():
        nonlocal table_lines
        if table_lines:
            result.append('```')
            result.extend(table_lines)
            result.append('```')
            table_lines = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        next_stripped = lines[i + 1].strip() if i + 1 < len(lines) else ""

        # Detect table: line starts/ends with | AND next line is separator (|---|)
        if stripped.startswith('|') and stripped.endswith('|'):
            if not in_table:
                if next_stripped.startswith('|') and '---' in next_stripped:
                    in_table = True
            if in_table:
                # Skip separator lines (|---|---|)
                if not all(c in '|-: ' for c in stripped.replace('|', '-')):
                    clean = stripped.strip('|').strip()
                    table_lines.append("│ {clean} │".format(clean=clean))
                continue
        else:
            if in_table:
                flush_table()
                in_table = False
            result.append(line)

    if in_table:
        flush_table()

    final = '\n'.join(result)
    final = re.sub(r'\|[-:\s|]+\|', '', final)

    return final


def split_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    remaining = text
    total = (len(text) + max_len - 1) // max_len

    idx = 1
    while remaining:
        if len(remaining) <= max_len:
            parts.append(f"(parte {idx}/{total})\n{remaining}")
            break

        chunk = remaining[:max_len]
        split_at = max(
            chunk.rfind("\n\n"),
            chunk.rfind(". "),
            chunk.rfind("\n"),
            chunk.rfind(" "),
        )

        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len

        chunk = remaining[: split_at + 1].rstrip()
        parts.append(f"(parte {idx}/{total})\n{chunk}")
        remaining = remaining[split_at + 1:].lstrip()
        idx += 1

    return parts


def authorize(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


def load_session_map() -> dict:
    """Load {chat_id: {active, sessions: {name: {id, title, created, last_used, prompt_count}}}}"""
    if SESSION_DB.exists():
        try:
            return json.loads(SESSION_DB.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_session_map(data: dict):
    """Save session mapping to disk."""
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    SESSION_DB.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_opencode_session_list(output: str) -> list[dict]:
    """Parse 'opencode session list' output into list of {id, title, updated}."""
    sessions = []
    for line in output.strip().split('\n'):
        # Format: ses_XXX  Title (with spaces)   HH:MM
        # or:      ses_XXX  Title                HH:MM · DD/M/YYYY
        match = re.match(r'(ses_\w+)\s{2,}(.+?)\s{2,}(\d{2}:\d{2}(?:\s·\s\d{1,2}/\d{1,2}/\d{4})?)', line)
        if match:
            sessions.append({
                "id": match.group(1),
                "title": match.group(2).strip(),
                "updated": match.group(3).strip(),
            })
    return sessions


async def fetch_opencode_sessions() -> list[dict]:
    """Run 'opencode session list' and parse output."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [OPENCODE_CMD, "session", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        )

        if result.returncode != 0:
            logger.warning(f"opencode session list failed: {result.stderr.strip()}")
            return []

        return parse_opencode_session_list(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("opencode session list timed out")
        return []
    except Exception as e:
        logger.error(f"Failed to fetch opencode sessions: {e}")
        return []


async def query_opencode_db(sql: str) -> list[dict]:
    """Execute a SQL query against opencode.db via CLI. Returns list of dicts."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [OPENCODE_CMD, "db", sql, "--format", "json"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        )
        if result.returncode != 0:
            logger.warning(f"DB query failed: {result.stderr.strip()[:100]}")
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except Exception as e:
        logger.warning(f"DB query error: {e}")
        return []


def run_opencode(cmd: list[str], workdir: str, timeout: int, chat_id: int = None) -> tuple:
    """Run opencode with proper timeout via subprocess.Popen.

    Returns (stdout: str, stderr: str, exitcode: int, timed_out: bool).
    Stores process in current_process for /cancel support when chat_id is given.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=workdir,
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    if chat_id is not None:
        current_process[chat_id] = process

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, process.returncode, False
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
            )
        else:
            process.kill()
        process.wait()
        return (
            "",
            "Timeout: el prompt tard\u00f3 m\u00e1s de {} segundos.".format(timeout),
            -1,
            True,
        )
    finally:
        if chat_id is not None:
            current_process.pop(chat_id, None)


def _relative_time(past_dt: datetime) -> str:
    """Return a human-readable relative time string in Spanish."""
    seconds = int((datetime.now(timezone.utc) - past_dt).total_seconds())
    if seconds < 60:
        return "hace {} seg".format(seconds)
    minutes = seconds // 60
    if minutes < 60:
        return "hace {} min".format(minutes)
    hours = minutes // 60
    minutes_rem = minutes % 60
    if hours < 24:
        if minutes_rem:
            return "hace {}h {}min".format(hours, minutes_rem)
        return "hace {}h".format(hours)
    days = hours // 24
    hours_rem = hours % 24
    return "hace {}d {}h".format(days, hours_rem)


def _assemble_response(raw_stdout: str, raw_stderr: str) -> str:
    """Build the final response from stdout and stderr, filtering metadata."""
    stdout_text = raw_stdout.strip() if raw_stdout else ""
    meaningful_stderr = _filter_stderr(raw_stderr)

    if stdout_text:
        response = stdout_text
        if meaningful_stderr:
            response += "\n\n---\n" + meaningful_stderr
    else:
        response = meaningful_stderr if meaningful_stderr else raw_stderr.strip()

    return clean_opencode_output(response)


async def progress_updater(context, chat_id: int, message_id: int, stop_event: asyncio.Event):
    """Update the 'processing...' message every 5 seconds with elapsed time."""
    seconds = 0
    while not stop_event.is_set():
        await asyncio.sleep(5)
        seconds += 5
        if not stop_event.is_set():
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="\u23f3 OpenCode procesando... ({}s)".format(seconds),
                )
            except Exception:
                pass


async def _process_prompt(
    update: Update, chat_id: int, prompt: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Core prompt processing: session management, opencode execution, response delivery."""
    now = datetime.now(timezone.utc)

    # Sync with sessions.json (SOURCE OF TRUTH for session state)
    smap = load_session_map()
    chat_data = smap.setdefault(str(chat_id), {})
    chat_sessions = chat_data.setdefault("sessions", {})
    session_name = chat_data.get("active", "default")

    # Ensure default session exists in sessions.json
    if "default" not in chat_sessions:
        chat_sessions["default"] = {
            "id": None,
            "title": "default",
            "created": now.isoformat(),
            "last_used": None,
            "prompt_count": 0,
        }
    if not chat_data.get("active"):
        chat_data["active"] = "default"
        save_session_map(smap)

    # Extract session data from sessions.json (source of truth)
    session_entry = chat_sessions.get(session_name, {})
    real_session_id = session_entry.get("id")
    has_real_id = bool(real_session_id)

    # Get model preference (persists across session resets)
    model = current_model.get(chat_id, DEFAULT_MODEL)

    # Detect stale in-memory session (switched to different named session)
    mem_session = active_sessions.get(chat_id)
    if mem_session and mem_session.get("session_name") != session_name:
        logger.info(
            "In-memory session '%s' stale after switch to '%s', clearing",
            mem_session.get("session_name"), session_name,
        )
        active_sessions.pop(chat_id, None)
        mem_session = None

    # Create or update in-memory session synced with sessions.json
    if not mem_session:
        # No valid in-memory session — build from sessions.json
        active_sessions[chat_id] = {
            "session_name": session_name,
            "session_id": real_session_id,
            "first_message": now,
            "last_used": now,
            "prompt_count": session_entry.get("prompt_count", 0),
        }
        session = active_sessions[chat_id]
        if has_real_id:
            logger.info(
                "Session %s: continuing from sessions.json (model=%s) ID %s...",
                session_name, model, real_session_id[:20],
            )
        else:
            logger.info(
                "Session for %s: first message (model=%s)",
                mask_chat_id(chat_id), model,
            )
    else:
        # Existing in-memory session — update timestamps and sync
        session = mem_session
        session["last_used"] = now
        session["prompt_count"] = session.get("prompt_count", 0) + 1
        # Resync session_id from sessions.json
        session["session_id"] = real_session_id
        logger.info("Session %s: continuing (model=%s)", session_name, model)

    # Track whether this is a new session (no real OpenCode ID yet)
    is_new_session = not has_real_id

    truncated = prompt[:100] + "..." if len(prompt) > 100 else prompt
    logger.info(
        "Request from %s | prompt=%r | len=%d | model=%s",
        mask_chat_id(chat_id),
        truncated,
        len(prompt),
        model,
    )

    # Build command — use --continue/--session only when we have a real session ID
    oc_cmd = OPENCODE_CMD if OPENCODE_CMD else "opencode"
    cmd = [oc_cmd, "run"]
    if model:
        cmd.extend(["--model", model])
    if has_real_id and real_session_id:
        cmd.extend(["--continue", "--session", real_session_id])
        logger.info("Continuing session %s (%s...)", session_name, real_session_id[:20])
    else:
        logger.info("New session '%s', will capture ID after execution", session_name)
    cmd.append(prompt)

    processing_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="\u23f3 OpenCode procesando..."
    )

    stop_event = asyncio.Event()
    updater_task = asyncio.create_task(
        progress_updater(context, chat_id, processing_msg.message_id, stop_event)
    )

    start_ts = time.time()
    try:
        # Run opencode in a thread so the event loop stays responsive for /cancel
        loop = asyncio.get_running_loop()
        stdout, stderr, exitcode, timed_out = await loop.run_in_executor(
            None,
            run_opencode,
            cmd,
            OPENCODE_WORKDIR,
            OPENCODE_TIMEOUT,
            chat_id,
        )

        # Check if cancelled during execution
        if chat_id in cancel_requests:
            cancel_requests.discard(chat_id)
            return

        duration = time.time() - start_ts

        if timed_out:
            logger.error(
                "Timeout for %s | duration=%.1fs | prompt=%r",
                mask_chat_id(chat_id),
                duration,
                truncated,
            )
            response = stderr
        else:
            response = _assemble_response(stdout, stderr)

            if not response.strip():
                response = "OpenCode no produjo salida."

            if exitcode != 0:
                logger.warning(
                    "Session %s: non-zero exit %d, session preserved for retry",
                    session_name,
                    exitcode,
                )

        # Capture real session ID for new sessions
        if exitcode == 0 and not timed_out and is_new_session:
            try:
                all_sessions = await fetch_opencode_sessions()
                if not all_sessions:
                    await asyncio.sleep(1)
                    all_sessions = await fetch_opencode_sessions()
                if all_sessions:
                    real_id = all_sessions[0]["id"]
                    smap = load_session_map()
                    chat_data = smap.setdefault(str(chat_id), {})
                    chat_sessions = chat_data.setdefault("sessions", {})
                    chat_sessions[session_name] = {
                        "id": real_id,
                        "title": prompt[:50],
                        "created": datetime.now(timezone.utc).isoformat(),
                        "last_used": datetime.now(timezone.utc).isoformat(),
                        "prompt_count": 1,
                    }
                    chat_data["active"] = session_name
                    save_session_map(smap)
                    if active_sessions.get(chat_id):
                        active_sessions[chat_id]["session_id"] = real_id
                        active_sessions[chat_id]["prompt_count"] = 1
                    logger.info("Captured session: %s -> %s", session_name, real_id)
                else:
                    logger.warning("Could not capture session ID (no sessions found)")
            except Exception as e:
                logger.warning("Failed to capture session ID: %s", e)

        # Update prompt count in sessions.json for continuing sessions
        if exitcode == 0 and not timed_out and has_real_id:
            try:
                smap = load_session_map()
                chat_data = smap.get(str(chat_id), {})
                active_name = chat_data.get("active", "default")
                if active_name in chat_data.get("sessions", {}):
                    chat_data["sessions"][active_name]["prompt_count"] = active_sessions[chat_id]["prompt_count"]
                    chat_data["sessions"][active_name]["last_used"] = datetime.now(timezone.utc).isoformat()
                    save_session_map(smap)
            except Exception as e:
                logger.warning("Failed to update sessions.json: %s", e)

        logger.info(
            "Completed for %s | duration=%.1fs | response_len=%d | exit=%d",
            mask_chat_id(chat_id),
            duration,
            len(response),
            exitcode,
        )

        # Prepend new-session header for first message in a brand-new session
        if is_new_session:
            response = "\U0001f195 Nueva sesi\u00f3n '{name}' iniciada\n\n".format(name=session_name) + response

        for fragment in split_message(response):
            fragment = telegramify_markdown(fragment)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=fragment,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e:
                logger.debug(f"MarkdownV2 parse failed for chat {mask_chat_id(chat_id)}: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=fragment
                    )
                except Exception as e2:
                    logger.error(f"Failed to send message to {mask_chat_id(chat_id)}: {e2}")

    except subprocess.TimeoutExpired:
        # Safety net: run_opencode handles timeouts internally,
        # but the executor could surface this too
        current_process.pop(chat_id, None)
        duration = time.time() - start_ts
        logger.error(
            "Timeout (outer) for %s | duration=%.1fs | prompt=%r",
            mask_chat_id(chat_id),
            duration,
            truncated,
        )
        timeout_msg = "El comando excedi\u00f3 el tiempo l\u00edmite de {}s.".format(OPENCODE_TIMEOUT)
        timeout_msg = telegramify_markdown(timeout_msg)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=timeout_msg,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=timeout_msg
            )

    except Exception as e:
        current_process.pop(chat_id, None)
        duration = time.time() - start_ts
        logger.exception(
            "Error for %s | duration=%.1fs",
            mask_chat_id(chat_id),
            duration,
        )
        error_msg = "Error al ejecutar OpenCode: {}".format(e)
        error_msg = telegramify_markdown(error_msg)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_msg,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_msg
            )

    finally:
        stop_event.set()
        try:
            await updater_task
        except Exception:
            pass

        dur = time.time() - start_ts
        cmpl_text = "\u2705 [{name}] Completado ({dur:.0f}s)".format(name=session_name, dur=dur)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=processing_msg.message_id,
                text=cmpl_text,
            )
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
            except Exception:
                pass


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /start from %s", mask_chat_id(chat_id))
        return

    await update.message.reply_text(
        "OpenCode Bot listo.\n\n"
        "Env\u00eda cualquier mensaje y se ejecutar\u00e1 con OpenCode CLI.\n"
        "Soporta m\u00faltiples sesiones con /session new|list|switch|delete|info\n"
        "/help \u2014 ver todos los comandos\n"
        "/status \u2014 estado de la sesi\u00f3n\n"
        "/new \u2014 reiniciar sesi\u00f3n activa"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /help from %s", mask_chat_id(chat_id))
        return

    await update.message.reply_text(
        "Comandos disponibles:\n\n"
        "/open &lt;prompt&gt; - Enviar prompt al orquestador\n"
        "/model pro|flash - Cambiar modelo\n"
        "/cancel - Cancelar prompt en ejecuci\u00f3n\n"
        "/status - Ver estado de la sesi\u00f3n\n"
        "/new - Reiniciar sesi\u00f3n activa\n"
        "/session new &lt;nombre&gt; - Crear sesi\u00f3n nombrada\n"
        "/session list - Listar todas las sesiones\n"
        "/session switch &lt;nombre&gt; - Cambiar sesi\u00f3n activa\n"
        "/session delete &lt;nombre&gt; - Eliminar sesi\u00f3n\n"
        "/session info [nombre] - Detalles de una sesi\u00f3n\n"
        "/session discover - Descubrir sesiones OpenCode existentes\n"
        "/session adopt &lt;id&gt; &lt;nombre&gt; - Adoptar una sesi\u00f3n OpenCode\n"
        "/help - Este mensaje\n\n"
        "Tambi\u00e9n pod\u00e9s enviar cualquier mensaje directamente sin usar /open."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /status from %s", mask_chat_id(chat_id))
        return

    session = active_sessions.get(chat_id)
    model = current_model.get(chat_id, DEFAULT_MODEL)

    if session:
        session_name = session.get("session_name", "default")
        session_id = session.get("session_id")
        if session_id:
            session_id_display = session_id[:24] + "..." if len(session_id) > 24 else session_id
        else:
            session_id_display = "pendiente"
        first_msg = _relative_time(session["first_message"])
        last_used = _relative_time(session["last_used"])
        prompt_count = session.get("prompt_count", 0)
    else:
        session_name = "-"
        session_id_display = "-"
        first_msg = "N/A"
        last_used = "N/A"
        prompt_count = 0

    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = "{}h {}m {}s".format(hours, minutes, seconds)

    await update.message.reply_text(
        "\U0001f4ca Estado de la sesi\u00f3n\n"
        "\u251c\u2500 Nombre: {session_name}\n"
        "\u251c\u2500 ID OpenCode: {session_id_display}\n"
        "\u251c\u2500 Modelo: {model}\n"
        "\u251c\u2500 Primera interacci\u00f3n: {first_msg}\n"
        "\u251c\u2500 \u00daltima interacci\u00f3n: {last_used}\n"
        "\u251c\u2500 Total prompts: {prompt_count}\n"
        "\u2514\u2500 Uptime del bot: {uptime_str}".format(
            model=model,
            session_name=session_name,
            session_id_display=session_id_display,
            first_msg=first_msg,
            last_used=last_used,
            prompt_count=prompt_count,
            uptime_str=uptime_str,
        )
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /new from %s", mask_chat_id(chat_id))
        return

    # Reset active session: clear real ID so next prompt creates fresh session
    smap = load_session_map()
    chat_data = smap.get(str(chat_id), {})
    active_name = chat_data.get("active", "default")
    if active_name in chat_data.get("sessions", {}):
        chat_data["sessions"][active_name]["id"] = None
        chat_data["sessions"][active_name]["prompt_count"] = 0
        save_session_map(smap)

    active_sessions.pop(chat_id, None)
    # Model preference preserved in current_model (separate from active_sessions)
    logger.info("Session '%s' reset by /new for %s", active_name, mask_chat_id(chat_id))
    await update.message.reply_text(
        "\U0001f504 Sesi\u00f3n '{name}' reiniciada. El pr\u00f3ximo mensaje comenzar\u00e1 desde cero.".format(name=active_name)
    )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /model from %s", mask_chat_id(chat_id))
        return

    args = context.args
    if not args:
        model = current_model.get(chat_id, DEFAULT_MODEL)
        await update.message.reply_text("Modelo actual: {}".format(model))
        return

    choice = args[0].lower()
    if choice in ("pro", "deepseek/deepseek-v4-pro", "deepseek-v4-pro"):
        current_model[chat_id] = "deepseek/deepseek-v4-pro"
        await update.message.reply_text(
            "\u2705 Modelo: deepseek/deepseek-v4-pro (pensamiento profundo, SDD completo)"
        )
    elif choice in ("flash", "deepseek/deepseek-v4-flash", "deepseek-v4-flash"):
        current_model[chat_id] = "deepseek/deepseek-v4-flash"
        await update.message.reply_text(
            "\u2705 Modelo: deepseek/deepseek-v4-flash (r\u00e1pido, consultas simples)"
        )
    else:
        model = current_model.get(chat_id, DEFAULT_MODEL)
        await update.message.reply_text(
            "Uso: /model pro | /model flash\n"
            "Modelo actual: {}".format(model)
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /cancel from %s", mask_chat_id(chat_id))
        return

    proc = current_process.pop(chat_id, None)
    if proc is None or proc.poll() is not None:
        await update.message.reply_text(
            "No hay ning\u00fan prompt en ejecuci\u00f3n."
        )
        return

    cancel_requests.add(chat_id)
    logger.info(
        "Cancelling prompt for %s (PID %s)", mask_chat_id(chat_id), proc.pid
    )
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.kill()

    await update.message.reply_text("\u274c Prompt cancelado")


async def session_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        return

    smap = load_session_map()
    chat_sessions = smap.get(str(chat_id), {})

    lines = ["\U0001f4cb *Sesiones del Bot*"]

    if chat_sessions.get("sessions"):
        active_name = chat_sessions.get("active", "default")
        for name, info in chat_sessions["sessions"].items():
            marker = "\U0001f7e2" if name == active_name else "\u26aa"
            oc_id = info.get("id", "?")[:20] + "..."
            lines.append("{} *{}* \u2192 `{}` (prompts: {})".format(
                marker, name, oc_id, info.get("prompt_count", 0)))
    else:
        lines.append("\u26aa No hay sesiones mapeadas a\u00fan")

    lines.append("")
    lines.append("\U0001f4cb *Sesiones OpenCode (raw)*")
    try:
        raw = await fetch_opencode_sessions()
        for s in raw[:10]:
            lines.append("\u2022 `{}...` {}".format(s["id"][:20], s["title"][:40]))
    except Exception as e:
        lines.append("\u274c Error: {}".format(e))

    msg = "\n".join(lines)
    try:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text(msg)


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /session subcommands."""
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: /session [new|list|switch|delete|info|discover|adopt] [nombre]"
        )
        return

    subcommand = args[0].lower()
    name = args[1] if len(args) > 1 else None

    if subcommand == "new":
        await _session_new(update, chat_id, name)
    elif subcommand == "list":
        await _session_list(update, chat_id)
    elif subcommand == "switch":
        await _session_switch(update, chat_id, name)
    elif subcommand == "delete":
        await _session_delete(update, chat_id, name)
    elif subcommand == "info":
        await _session_info(update, chat_id, name)
    elif subcommand == "discover":
        await _session_discover(update, chat_id)
    elif subcommand == "adopt":
        real_id = args[1] if len(args) > 1 else None
        adopt_name = args[2] if len(args) > 2 else None
        await _session_adopt(update, chat_id, real_id, adopt_name)
    else:
        await update.message.reply_text(
            "Subcomando desconocido: {}. Uso: /session [new|list|switch|delete|info|discover|adopt]".format(subcommand)
        )


async def _session_new(update: Update, chat_id: int, name: str | None) -> None:
    """Create a named session (lazy: no OpenCode call yet)."""
    if not name:
        await update.message.reply_text("Uso: /session new <nombre>")
        return

    smap = load_session_map()
    chat_data = smap.setdefault(str(chat_id), {})
    chat_sessions = chat_data.setdefault("sessions", {})

    if name in chat_sessions:
        await update.message.reply_text(
            "\u26a0\ufe0f La sesi\u00f3n '{name}' ya existe. Us\u00e1 /session switch {name} para cambiarte.".format(name=name)
        )
        return

    chat_sessions[name] = {
        "id": None,
        "title": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_used": None,
        "prompt_count": 0,
    }
    chat_data["active"] = name
    save_session_map(smap)

    logger.info("Session '%s' created for %s", name, mask_chat_id(chat_id))
    await update.message.reply_text(
        "\U0001f195 Sesi\u00f3n '{name}' creada y activada.\n"
        "El pr\u00f3ximo prompt se ejecutar\u00e1 en esta sesi\u00f3n.".format(name=name)
    )


async def _session_list(update: Update, chat_id: int) -> None:
    """Show all sessions for this chat."""
    smap = load_session_map()
    chat_data = smap.get(str(chat_id), {})
    chat_sessions = chat_data.get("sessions", {})
    active_name = chat_data.get("active", "default")

    if not chat_sessions:
        await update.message.reply_text(
            "\U0001f4cb No ten\u00e9s sesiones. Us\u00e1 /session new <nombre> para crear una."
        )
        return

    lines = ["\U0001f4cb Tus sesiones:\n"]
    for name, info in chat_sessions.items():
        marker = "\U0001f7e2" if name == active_name else "\u26aa"
        oc_id = info.get("id")
        prompt_count = info.get("prompt_count", 0)

        if oc_id:
            id_display = oc_id[:16] + "..."
            last_used_str = ""
            if info.get("last_used"):
                try:
                    last_dt = datetime.fromisoformat(info["last_used"])
                    last_used_str = ", {}".format(_relative_time(last_dt))
                except Exception:
                    pass
            lines.append(
                "{marker} {name} \u2192 `{id}` ({count} prompts{lu})".format(
                    marker=marker, name=name, id=id_display,
                    count=prompt_count, lu=last_used_str
                )
            )
        else:
            lines.append(
                "{marker} {name} \u2192 (nueva, sin usar a\u00fan)".format(marker=marker, name=name)
            )

    await update.message.reply_text("\n".join(lines))


async def _session_switch(update: Update, chat_id: int, name: str | None) -> None:
    """Switch active session."""
    if not name:
        await update.message.reply_text("Uso: /session switch <nombre>")
        return

    smap = load_session_map()
    chat_data = smap.get(str(chat_id), {})
    chat_sessions = chat_data.get("sessions", {})

    if name not in chat_sessions:
        await update.message.reply_text(
            "\u274c La sesi\u00f3n '{name}' no existe.\n"
            "Us\u00e1 /session list para ver tus sesiones.".format(name=name)
        )
        return

    chat_data["active"] = name
    save_session_map(smap)

    # Clear in-memory session so next prompt picks up the new active session
    active_sessions.pop(chat_id, None)

    logger.info("Session switched to '%s' for %s", name, mask_chat_id(chat_id))
    await update.message.reply_text(
        "\U0001f500 Sesi\u00f3n cambiada a '{name}'.\n"
        "Pr\u00f3ximo prompt usar\u00e1 esta sesi\u00f3n.".format(name=name)
    )


async def _session_delete(update: Update, chat_id: int, name: str | None) -> None:
    """Delete a named session."""
    if not name:
        await update.message.reply_text("Uso: /session delete <nombre>")
        return

    smap = load_session_map()
    chat_data = smap.get(str(chat_id), {})
    chat_sessions = chat_data.get("sessions", {})

    if name not in chat_sessions:
        await update.message.reply_text(
            "\u274c La sesi\u00f3n '{name}' no existe.".format(name=name)
        )
        return

    session_info = chat_sessions.pop(name)
    was_active = chat_data.get("active") == name

    if was_active:
        chat_data["active"] = "default"
        if "default" not in chat_sessions:
            chat_sessions["default"] = {
                "id": None,
                "title": "default",
                "created": datetime.now(timezone.utc).isoformat(),
                "last_used": None,
                "prompt_count": 0,
            }

    save_session_map(smap)
    active_sessions.pop(chat_id, None)

    # Clean up in OpenCode if it had a real ID
    real_id = session_info.get("id")
    if real_id:
        try:
            proc = await asyncio.create_subprocess_exec(
                OPENCODE_CMD, "session", "delete", real_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            logger.info(
                "Deleted OpenCode session %s for chat %s", real_id, mask_chat_id(chat_id)
            )
        except Exception as e:
            logger.warning(
                "Failed to delete OpenCode session %s: %s", real_id, e
            )

    msg = "\U0001f5d1\ufe0f Sesi\u00f3n '{name}' eliminada.".format(name=name)
    if was_active:
        msg += "\nSesi\u00f3n activa cambiada a 'default'."
    await update.message.reply_text(msg)


async def _session_info(update: Update, chat_id: int, name: str | None) -> None:
    """Show detailed info about a session."""
    smap = load_session_map()
    chat_data = smap.get(str(chat_id), {})
    chat_sessions = chat_data.get("sessions", {})
    active_name = chat_data.get("active", "default")

    target_name = name if name else active_name

    if target_name not in chat_sessions:
        await update.message.reply_text(
            "\u274c La sesi\u00f3n '{name}' no existe.".format(name=target_name)
        )
        return

    info = chat_sessions[target_name]
    oc_id = info.get("id")
    title = info.get("title", target_name)
    created = info.get("created", "\u2014")
    last_used = info.get("last_used")
    prompt_count = info.get("prompt_count", 0)

    id_display = "\u2014 (sin ID a\u00fan)" if not oc_id else (
        oc_id[:24] + "..." if len(oc_id) > 24 else oc_id
    )

    last_used_str = "nunca"
    if last_used:
        try:
            last_dt = datetime.fromisoformat(last_used)
            last_used_str = _relative_time(last_dt)
        except Exception:
            last_used_str = str(last_used)

    created_display = created[:19] if created and created != "\u2014" else created

    lines = [
        "\U0001f4cb Sesi\u00f3n: {name}".format(name=target_name),
        "    ID: {id}".format(id=id_display),
        "    T\u00edtulo: {title}".format(title=title),
        "    Creada: {created}".format(created=created_display),
        "    \u00daltimo uso: {lu}".format(lu=last_used_str),
        "    Prompts: {count}".format(count=prompt_count),
    ]

    # Try to enrich with DB data (non-fatal)
    if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
        try:
            msg_rows = await query_opencode_db(
                f"SELECT COUNT(*) as count FROM message WHERE session_id = '{oc_id}'"
            )
            if msg_rows:
                lines.append("    Mensajes en BD: {cnt}".format(cnt=msg_rows[0].get('count', '?')))

            session_rows = await query_opencode_db(
                f"SELECT created_at, model FROM session WHERE id = '{oc_id}'"
            )
            if session_rows:
                row = session_rows[0]
                if row.get("created_at"):
                    lines.append("    Creada (BD): {ca}".format(ca=row['created_at']))
                if row.get("model"):
                    lines.append("    Modelo (BD): {m}".format(m=row['model']))
        except Exception:
            pass  # DB enrichment is optional, never fail on it

    await update.message.reply_text("\n".join(lines))


async def _session_discover(update: Update, chat_id: int) -> None:
    """Show all OpenCode sessions with adoption status."""
    oc_sessions = await fetch_opencode_sessions()
    smap = load_session_map()
    chat_sessions = smap.get(str(chat_id), {}).get("sessions", {})

    adopted_map = {}
    for name, info in chat_sessions.items():
        real_id = info.get("id")
        if real_id:
            adopted_map[real_id] = name

    if not oc_sessions:
        await update.message.reply_text(
            "\U0001f4cb No se encontraron sesiones OpenCode."
        )
        return

    lines = ["\U0001f4cb *Sesiones OpenCode descubiertas:*\n"]

    for s in oc_sessions[:15]:
        sid = s["id"]
        title = s["title"][:60]

        friendly_name = adopted_map.get(sid)
        if friendly_name:
            prompt_count = chat_sessions.get(friendly_name, {}).get("prompt_count", 0)
            lines.append(
                "\u2705 *{name}* \u2192 `{sid}` (ya adoptada, {count} prompts)".format(
                    name=friendly_name, sid=sid, count=prompt_count
                )
            )
        else:
            lines.append(
                "\u26aa *{title}*\n"
                "   `{sid}`\n"
                "   Para adoptar: `/session adopt {sid} <nombre>`".format(
                    title=title, sid=sid
                )
            )
        lines.append("")

    if len(oc_sessions) > 15:
        lines.append("Mostrando 15 de {} sesiones.".format(len(oc_sessions)))

    lines.append("\nUsa `/session adopt <id> <nombre>` para adoptar una sesi\u00f3n.")

    msg = "\n".join(lines)
    try:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await update.message.reply_text(msg)


async def _session_adopt(
    update: Update, chat_id: int, real_id: str | None, name: str | None
) -> None:
    """Adopt an existing OpenCode session by real ID."""
    if not real_id or not name:
        await update.message.reply_text(
            "\u274c Uso: /session adopt <id> <nombre>"
        )
        return

    if name == "<nombre>":
        await update.message.reply_text(
            "\u274c Reemplaz\u00e1 `<nombre>` con un nombre para la sesi\u00f3n.\n"
            "Ejemplo: `/session adopt {id} telegram-bot`".format(id=real_id[:24]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    if not re.match(r'^[a-zA-Z0-9_-]{1,30}$', name):
        await update.message.reply_text(
            "\u274c El nombre debe tener entre 1 y 30 caracteres y solo usar letras, n\u00fameros, guiones y guiones bajos.\n"
            "Ejemplo: `mi-sesion`, `balanceate_api`, `docs`"
        )
        return

    oc_sessions = await fetch_opencode_sessions()
    oc_by_id = {s["id"]: s for s in oc_sessions}

    if real_id not in oc_by_id:
        await update.message.reply_text(
            "\u274c La sesi\u00f3n `{id}` no existe en OpenCode.".format(id=real_id)
        )
        return

    smap = load_session_map()
    chat_data = smap.setdefault(str(chat_id), {})
    chat_sessions = chat_data.setdefault("sessions", {})

    if name in chat_sessions:
        await update.message.reply_text(
            "\u26a0\ufe0f El nombre '{name}' ya existe. "
            "Us\u00e1 /session switch {name} para usarla.".format(name=name)
        )
        return

    oc_info = oc_by_id[real_id]

    chat_sessions[name] = {
        "id": real_id,
        "title": oc_info.get("title", name),
        "created": datetime.now(timezone.utc).isoformat(),
        "last_used": None,
        "prompt_count": 0,
    }

    if not chat_data.get("active"):
        chat_data["active"] = name

    save_session_map(smap)

    logger.info(
        "Session '%s' adopted (%s) for %s", name, real_id, mask_chat_id(chat_id)
    )

    await update.message.reply_text(
        "\u2705 Sesi\u00f3n '{name}' adoptada (ID: `{id}`)".format(
            name=name, id=real_id
        )
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Message from unauthorized chat %s", mask_chat_id(chat_id))
        return

    prompt = update.message.text.strip()
    if not prompt:
        return

    # Enforce one prompt at a time per chat
    if chat_id in current_process:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Us\u00e1 /cancel para cancelarlo."
        )
        return

    await _process_prompt(update, chat_id, prompt, context)


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not authorize(chat_id):
        logger.warning("Unauthorized /open from %s", mask_chat_id(chat_id))
        return

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Uso: /open &lt;prompt&gt;")
        return

    # Enforce one prompt at a time per chat
    if chat_id in current_process:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Us\u00e1 /cancel para cancelarlo."
        )
        return

    await _process_prompt(update, chat_id, prompt, context)


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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

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

    logger.info("Exploring OpenCode DB schema (non-fatal)...")
    try:
        tables = await query_opencode_db("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        logger.info(f"OpenCode DB tables: {[t.get('name', '?') for t in tables]}")
        for table in tables:
            tname = table.get("name", "")
            if tname:
                cols = await query_opencode_db(f"PRAGMA table_info({tname})")
                col_names = [c.get("name", "?") for c in cols]
                logger.info(f"  {tname}: {', '.join(col_names)}")
    except Exception as e:
        logger.info(f"DB schema exploration skipped: {e}")

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

    logger.info("Shutting down gracefully...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(run_bot())
