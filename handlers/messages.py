"""Message handlers: text prompts and voice messages.
 
Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module.
"""

from __future__ import annotations

import os
import tempfile

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config import OPENAI_API_KEY, CONTAINER_KEY, logger
from utils.logging import mask_chat_id
from handlers import authorized
from services.prompt_service import PromptAlreadyRunningError
from services.container import AppContainer


def _get_container(context) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return context.application.bot_data[CONTAINER_KEY]


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
                response_format="text",
            )

        return transcript.strip() if transcript else None

    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        return None


@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe and process as prompt."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    voice = update.message.voice
    if not voice:
        return

    if voice.duration < 1:
        await update.message.reply_text("El audio es muy corto, no se detectó voz.")
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
            chat_id=chat_id,
            prompt_text=text,
            update_for_logging=update,
        )

    except PromptAlreadyRunningError:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Usá /cancel para cancelarlo."
        )
    except Exception as e:
        logger.error("Voice handler error: %s", e)
        try:
            await progress_msg.edit_text(
                "\u274c Error al procesar el audio. Intentá de nuevo."
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
    """Handle a text prompt: validate, delegate to PromptService.

    Raises PromptAlreadyRunningError if a prompt is already executing.
    The caller (@authorized decorator) ensures only authorized chats can use this.
    """
    chat_id = update.effective_chat.id
    container = _get_container(context)

    prompt = update.message.text.strip()
    if not prompt:
        return

    try:
        await container.prompt_service.execute(
            chat_id=chat_id,
            prompt_text=prompt,
            update_for_logging=update,
        )
    except PromptAlreadyRunningError:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Usá /cancel para cancelarlo."
        )
