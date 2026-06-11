"""Command handlers: /start, /help, /status, /model, /cancel, /new, /open.
 
Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module. This eliminates the
  `import bot; bot.X` pattern entirely.
"""
 
from __future__ import annotations

import time

from telegram import Update
from telegram.ext import ContextTypes

from config import (
    DEFAULT_MODEL, DEFAULT_SESSION_NAME,
    MODEL_ALIASES, logger,
)
from utils.logging import mask_chat_id
from utils.time_formatting import relative_time
from handlers import authorized
from services.prompt_service import PromptAlreadyRunningError
from services.container import AppContainer


def _get_container(context) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return context.application.bot_data["container"]


@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "OpenCode Bot listo.\n\n"
        "Envía cualquier mensaje y se ejecutará con OpenCode CLI.\n"
        "Soporta múltiples sesiones con /session new|list|switch|delete|info\n"
        "/help — ver todos los comandos\n"
        "/status — estado de la sesión\n"
        "/new — reiniciar sesión activa"
    )


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n\n"
        "/open <prompt> - Enviar prompt al orquestador\n"
        "/model pro|flash - Cambiar modelo\n"
        "/cancel - Cancelar prompt en ejecución\n"
        "/status - Ver estado de la sesión\n"
        "/new - Reiniciar sesión activa\n"
        "/session new <nombre> - Crear sesión nombrada\n"
        "/session list - Listar todas las sesiones\n"
        "/session switch <nombre> - Cambiar sesión activa\n"
        "/session delete <nombre> - Eliminar sesión\n"
        "/session info [nombre] - Detalles de una sesión\n"
        "/session discover - Descubrir sesiones OpenCode existentes\n"
        "/session adopt <id> <nombre> - Adoptar una sesión OpenCode\n"
        "/test_md - Probar envío de MarkdownV2\n"
        "/help - Este mensaje\n\n"
        "Envá una nota de voz para transcribir y procesar como prompt.\n"
        "También podés enviar cualquier mensaje directamente sin usar /open."
    )


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session status using SessionStore."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    session = await container.session_store.get_active_session(chat_id)
    model = await container.session_store.get_model(chat_id)

    # Build session info
    if session is not None:
        session_name = session.name
        session_id_display = (
            session.real_id[:24] + "..."
            if session.real_id and len(session.real_id) > 24
            else (session.real_id or "pendiente")
        )
        first_msg = relative_time(
            type("dt", (), {"replace": lambda **kw: None})()
        ) if not session.created else "pendiente"
        last_used = "N/A"
        prompt_count = session.prompt_count

        # Try to compute relative time for last_used
        if session.last_used:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(session.last_used)
                last_used = relative_time(last_dt)
            except Exception:
                last_used = str(session.last_used)

        # For created time
        if session.created:
            try:
                from datetime import datetime, timezone
                created_dt = datetime.fromisoformat(session.created)
                first_msg = relative_time(created_dt)
            except Exception:
                first_msg = session.created[:19] if session.created else "N/A"
    else:
        session_name = "-"
        session_id_display = "-"
        first_msg = "N/A"
        last_used = "N/A"
        prompt_count = 0

    # Uptime from container
    if container.start_time is not None:
        uptime_seconds = int(time.time() - container.start_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = "{}h {}m {}s".format(hours, minutes, seconds)
    else:
        uptime_str = "desconocido"

    await update.message.reply_text(
        "\U0001f4ca Estado de la sesión\n"
        "\u251c\u2500 Nombre: {session_name}\n"
        "\u251c\u2500 ID OpenCode: {session_id_display}\n"
        "\u251c\u2500 Modelo: {model}\n"
        "\u251c\u2500 Primera interacción: {first_msg}\n"
        "\u251c\u2500 Última interacción: {last_used}\n"
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


@authorized
async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the active session's OpenCode ID — next prompt starts fresh."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    await container.session_store.reset_session(chat_id)

    active = await container.session_store.get_active_session(chat_id)
    active_name = active.name if active else DEFAULT_SESSION_NAME

    logger.info(
        "Session '%s' reset by /new for %s",
        active_name, mask_chat_id(chat_id),
    )
    await update.message.reply_text(
        "\U0001f504 Sesión '{name}' reiniciada. "
        "El próximo mensaje comenzará desde cero.".format(name=active_name)
    )


@authorized
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or change the AI model using SessionStore."""
    chat_id = update.effective_chat.id
    args = context.args
    container = _get_container(context)

    if not args:
        model = await container.session_store.get_model(chat_id)
        await update.message.reply_text("Modelo actual: {}".format(model))
        return

    choice = args[0].lower()
    model_value = MODEL_ALIASES.get(choice)
    if model_value:
        await container.session_store.set_model(chat_id, model_value)
        await update.message.reply_text("Modelo cambiado a {}".format(model_value))
    else:
        model = await container.session_store.get_model(chat_id)
        await update.message.reply_text(
            "Uso: /model pro | /model flash\n"
            "Modelo actual: {}".format(model)
        )


@authorized
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a running prompt using PromptService."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    if not container.prompt_service.is_running(chat_id):
        await update.message.reply_text("No hay ningún prompt en ejecución.")
        return

    cancelled = container.prompt_service.cancel(chat_id)
    if cancelled:
        logger.info(
            "Prompt cancelled for %s", mask_chat_id(chat_id),
        )
        await update.message.reply_text("\u274c Prompt cancelado")
    else:
        await update.message.reply_text("No hay ningún prompt en ejecución.")


@authorized
async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute a prompt via /open <text> using PromptService."""
    chat_id = update.effective_chat.id
    container = _get_container(context)

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Uso: /open <prompt>")
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
