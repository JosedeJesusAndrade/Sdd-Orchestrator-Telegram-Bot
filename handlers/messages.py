"""Message handlers: text prompts and voice messages.
  
Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module.

Phase 2 logging:
  - Uses per-module logger (`__name__`) instead of the root `config.logger`.
  - Voice transcription logs DEBUG events (transcribe_voice_skip,
    transcribe_voice, api_error) with structured fields.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import timedelta
from typing import cast

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config import OPENAI_API_KEY, CONTAINER_KEY
from utils.logging import get_module_logger, log_exception, mask_chat_id
from handlers import authorized
from handlers._guards import require_chat, require_message, require_text
from services.prompt_service import PromptAlreadyRunningError
from services.telegram_chat_view import TelegramChatView
from services.container import AppContainer

logger = get_module_logger(__name__)


def _get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return cast(AppContainer, context.application.bot_data[CONTAINER_KEY])


async def transcribe_voice(file_path: str, chat_id: int | None = None) -> str | None:
    """Transcribe voice audio to text using OpenAI Whisper API.

    Phase 2 logging:
      - DEBUG event=transcribe_voice_skip when API key missing (was WARNING)
      - DEBUG event=transcribe_voice on entry (with chat_id_masked)
      - ERROR event=api_error on OpenAI/HTTP failures (provider=openai)
      - ERROR event=voice_transcribe_error for other failures
    """
    masked_cid = mask_chat_id(chat_id) if chat_id is not None else "-"

    if not OPENAI_API_KEY:
        # Downgraded to DEBUG — this is a config detail, not a runtime event.
        # Operators check OPENAI_API_KEY during deployment, not at runtime.
        logger.debug(
            "OPENAI_API_KEY not set — skipping voice transcription",
            extra={"event": "transcribe_voice_skip", "chat_id_masked": masked_cid},
        )
        return None

    logger.debug(
        "Transcribe voice",
        extra={"event": "transcribe_voice", "chat_id_masked": masked_cid},
    )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        with open(file_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )

        return transcript.strip() if transcript else None

    except ImportError:
        logger.error(
            "openai package not installed",
            extra={
                "event": "voice_transcribe_error",
                "chat_id_masked": masked_cid,
                "error_type": "ImportError",
                "hint": "Run: pip install openai",
            },
        )
        return None
    except Exception as e:
        # Differentiate API errors (HTTP) from other failures.
        # openai package raises APIStatusError for HTTP 4xx/5xx.
        status = getattr(e, "status_code", None)
        if status is not None:
            logger.error(
                "Voice transcription API error",
                extra={
                    "event": "api_error",
                    "provider": "openai",
                    "status": status,
                    "error": str(e),
                    "chat_id_masked": masked_cid,
                },
            )
        else:
            logger.error(
                "Voice transcription failed",
                extra={
                    "event": "voice_transcribe_error",
                    "chat_id_masked": masked_cid,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
        return None


@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe and process as prompt."""
    chat_id = require_chat(update).id
    msg = require_message(update)
    container = _get_container(context)

    voice = msg.voice
    if not voice:
        return

    # PTB types ``Voice.duration`` as ``int | datetime.timedelta``. Comparing a
    # timedelta with an int raises TypeError, so normalize defensively.
    raw_duration = voice.duration
    duration = (
        raw_duration.total_seconds()
        if isinstance(raw_duration, timedelta)
        else raw_duration
    )
    if duration < 1:
        await msg.reply_text("El audio es muy corto, no se detectó voz.")
        return

    progress_msg = await msg.reply_text("\U0001f3a4 Transcribiendo audio...")

    temp_path = None
    try:
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            temp_path = tmp.name
        await file.download_to_drive(temp_path)

        text = await transcribe_voice(temp_path, chat_id=chat_id)

        if not text:
            await progress_msg.edit_text(
                "\u274c No pude transcribir el audio. Intentá de nuevo o escribí el prompt."
            )
            return

        await progress_msg.edit_text(
            "\U0001f3a4 *Transcripción:* {text}".format(
                text=text[:200] + ("..." if len(text) > 200 else "")
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        # Delegate to PromptService via container
        await container.prompt_service.execute(
            conversation_id=chat_id,
            prompt_text=text,
            view=TelegramChatView(container.message_sender, chat_id),
        )

    except PromptAlreadyRunningError:
        await msg.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Usá /cancel para cancelarlo."
        )
    except Exception as e:
        # Why extra={} here? The bare "%s" formatter above meant we lost the
        # machine-readable event index and any correlated fields (chat_id,
        # error_type). The voice path is high-signal for diagnosing audio
        # pipeline issues, so capture the exception type explicitly so we can
        # grep for `event=voice_handler_error error_type=...` later.
        logger.error(
            "Voice handler error",
            extra={
                "event": "voice_handler_error",
                "chat_id_masked": mask_chat_id(chat_id),
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        try:
            await progress_msg.edit_text(
                "\u274c Error al procesar el audio. Intentá de nuevo."
            )
        except Exception:
            log_exception(
                "voice_error_edit_failed",
                module=__name__,
                level=logging.DEBUG,
            )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                log_exception(
                    "voice_temp_cleanup_failed",
                    module=__name__,
                    level=logging.WARNING,
                )


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a text prompt: validate, delegate to PromptService.

    Raises PromptAlreadyRunningError if a prompt is already executing.
    The caller (@authorized decorator) ensures only authorized chats can use this.
    """
    chat_id = require_chat(update).id
    msg = require_message(update)
    container = _get_container(context)

    prompt = require_text(msg).strip()
    if not prompt:
        return

    try:
        await container.prompt_service.execute(
            conversation_id=chat_id,
            prompt_text=prompt,
            view=TelegramChatView(container.message_sender, chat_id),
        )
    except PromptAlreadyRunningError:
        await msg.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Usá /cancel para cancelarlo."
        )
