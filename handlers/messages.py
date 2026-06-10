"""Message handlers: text prompts and voice messages."""
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
import asyncio
import os
import sys
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from config import (
    DEFAULT_MODEL, OPENCODE_WORKDIR, OPENCODE_TIMEOUT,
    OPENCODE_CMD, OPENAI_API_KEY, DEFAULT_SESSION_NAME,
    PROGRESS_UPDATE_INTERVAL, logger,
)
from persistence.sessions import (
    load_session_map_safe, save_session_map_atomic,
    fetch_opencode_sessions, invalidate_opencode_sessions_cache,
)
from opencode.client import run_opencode
from formatting.markdown import (
    telegramify_markdown,
    minimal_escape_mdv2,
    _assemble_response,
    split_message,
    send_telegram_mdv2,
)
from utils.logging import mask_chat_id
from handlers import (
    authorized, active_sessions, current_model,
    current_process, cancel_requests, process_status,
)


async def transcribe_voice(file_path: str) -> str | None:
    """Transcribe voice audio to text using OpenAI Whisper API."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping voice transcription")
        return None

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        with open(file_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )

        return transcript.strip() if transcript else None

    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        return None


def _relative_time(past_dt: datetime) -> str:
    """Return a human-readable relative time string in Spanish."""
    seconds = int((datetime.now(timezone.utc) - past_dt).total_seconds())
    if seconds < 0:
        return "ahora"
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


async def progress_updater(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, stop_event: asyncio.Event) -> None:
    """Update the 'processing...' message every 5 seconds with elapsed time."""
    seconds = 0
    while not stop_event.is_set():
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
        seconds += PROGRESS_UPDATE_INTERVAL
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
    smap = await load_session_map_safe()
    chat_data = smap.setdefault(str(chat_id), {})
    chat_sessions = chat_data.setdefault("sessions", {})
    session_name = chat_data.get("active", DEFAULT_SESSION_NAME)

    # Ensure default session exists in sessions.json
    if DEFAULT_SESSION_NAME not in chat_sessions:
        chat_sessions[DEFAULT_SESSION_NAME] = {
            "id": None,
            "title": "default",
            "created": now.isoformat(),
            "last_used": None,
            "prompt_count": 0,
        }
    if not chat_data.get("active"):
        chat_data["active"] = DEFAULT_SESSION_NAME
        await save_session_map_atomic(smap)

    # Extract session data from sessions.json (source of truth)
    session_entry = chat_sessions.get(session_name, {})
    real_session_id = session_entry.get("id")
    has_real_id = bool(real_session_id)

    # Get model preference (persists across session resets)
    model = smap.get(str(chat_id), {}).get("model") or current_model.get(chat_id) or DEFAULT_MODEL

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
        # Immediately persist prompt count to sessions.json
        active_name = smap.get(str(chat_id), {}).get("active", DEFAULT_SESSION_NAME)
        sessions_dict = smap.get(str(chat_id), {}).get("sessions", {})
        if active_name in sessions_dict:
            sessions_dict[active_name]["prompt_count"] = session["prompt_count"]
            sessions_dict[active_name]["last_used"] = datetime.now(timezone.utc).isoformat()
            await save_session_map_atomic(smap)
        # Resync session_id from sessions.json
        session["session_id"] = real_session_id
        logger.info("Session %s: continuing (model=%s)", session_name, model)

    # Track whether this is a new session (no real OpenCode ID yet)
    is_new_session = not has_real_id

    truncated = prompt[:100] + "..." if len(prompt) > 100 else prompt
    safe_prompt = truncated
    for pattern in ['sk-', 'Bearer ', '-----BEGIN', 'token=', 'secret=']:
        if pattern.lower() in safe_prompt.lower():
            safe_prompt = "[REDACTED - possible credential]"
            break
    logger.info(
        "Request from %s | prompt=%r | len=%d | model=%s",
        mask_chat_id(chat_id),
        safe_prompt,
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
    process_status[chat_id] = "running"
    try:
        loop = asyncio.get_running_loop()
        stdout, stderr, exitcode, timed_out = await loop.run_in_executor(
            None,
            run_opencode,
            cmd,
            OPENCODE_WORKDIR,
            OPENCODE_TIMEOUT,
            chat_id,
            current_process,
            process_status,
        )

        # Check if cancelled during execution
        if process_status.get(chat_id) == "cancelling":
            process_status.pop(chat_id, None)
            cancel_requests.discard(chat_id)
            return
        process_status.pop(chat_id, None)
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
                    await save_session_map_atomic(smap)
                    invalidate_opencode_sessions_cache()
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
                chat_data = smap.get(str(chat_id), {})
                active_name = chat_data.get("active", DEFAULT_SESSION_NAME)
                if active_name in chat_data.get("sessions", {}):
                    chat_data["sessions"][active_name]["prompt_count"] = active_sessions[chat_id]["prompt_count"]
                    chat_data["sessions"][active_name]["last_used"] = datetime.now(timezone.utc).isoformat()
                    await save_session_map_atomic(smap)
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
            fragment = minimal_escape_mdv2(fragment)
            await send_telegram_mdv2(context.bot, chat_id, fragment)

    except subprocess.TimeoutExpired:
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
        timeout_msg = minimal_escape_mdv2(timeout_msg)
        await send_telegram_mdv2(context.bot, chat_id, timeout_msg)

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
        error_msg = minimal_escape_mdv2(error_msg)
        await send_telegram_mdv2(context.bot, chat_id, error_msg)

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


@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe and process as prompt."""
    chat_id = update.effective_chat.id

    voice = update.message.voice
    if not voice:
        return

    if voice.duration < 1:
        await update.message.reply_text("El audio es muy corto, no se detect\u00f3 voz.")
        return

    progress_msg = await update.message.reply_text("\U0001f3a4 Transcribiendo audio...")

    temp_path = None
    try:
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            temp_path = tmp.name
        await file.download_to_drive(temp_path)

        text = await transcribe_voice(temp_path)

        if not text:
            await progress_msg.edit_text(
                "\u274c No pude transcribir el audio. Intent\u00e1 de nuevo o escrib\u00ed el prompt."
            )
            return

        await progress_msg.edit_text(
            "\U0001f3a4 *Transcripci\u00f3n:* {text}".format(
                text=text[:200] + ("..." if len(text) > 200 else "")
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        await _process_prompt(update, chat_id, text, context)

    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        try:
            await progress_msg.edit_text(
                "\u274c Error al procesar el audio. Intent\u00e1 de nuevo."
            )
        except Exception:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    prompt = update.message.text.strip()
    if not prompt:
        return

    if chat_id in current_process:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Us\u00e1 /cancel para cancelarlo."
        )
        return

    await _process_prompt(update, chat_id, prompt, context)
