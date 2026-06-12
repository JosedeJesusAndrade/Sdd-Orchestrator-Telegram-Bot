"""Command handlers: /start, /help, /status, /model, /cancel, /new, /open."""
import sys
import subprocess
import time

from telegram import Update
from telegram.ext import ContextTypes

from config import (
    DEFAULT_MODEL, DEFAULT_SESSION_NAME,
    MODEL_ALIASES, logger,
)
import config as _config
from persistence.sessions import (
    load_session_map_safe, save_session_map_atomic,
)
from utils.logging import mask_chat_id
from handlers import (
    authorized, active_sessions, current_model,
    current_process, cancel_requests, process_status,
)
from handlers.messages import _process_prompt, _relative_time


@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    await update.message.reply_text(
        "OpenCode Bot listo.\n\n"
        "Env\u00eda cualquier mensaje y se ejecutar\u00e1 con OpenCode CLI.\n"
        "Soporta m\u00faltiples sesiones con /session new|list|switch|delete|info\n"
        "/help \u2014 ver todos los comandos\n"
        "/status \u2014 estado de la sesi\u00f3n\n"
        "/new \u2014 reiniciar sesi\u00f3n activa"
    )


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

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
        "/test_md - Probar env\u00edo de MarkdownV2\n"
        "/help - Este mensaje\n\n"
        "Env\u00e1 una nota de voz para transcribir y procesar como prompt.\n"
        "Tambi\u00e9n pod\u00e9s enviar cualquier mensaje directamente sin usar /open."
    )


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    session = active_sessions.get(chat_id)
    smap = await load_session_map_safe()
    model = smap.get(str(chat_id), {}).get("model") or current_model.get(chat_id) or DEFAULT_MODEL

    if session:
        session_name = session.get("session_name", DEFAULT_SESSION_NAME)
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

    if _config.START_TIME is not None:
        uptime_seconds = int(time.time() - _config.START_TIME)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = "{}h {}m {}s".format(hours, minutes, seconds)
    else:
        uptime_str = "desconocido"

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


@authorized
async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    smap = await load_session_map_safe()
    chat_data = smap.get(str(chat_id), {})
    active_name = chat_data.get("active", DEFAULT_SESSION_NAME)
    if active_name in chat_data.get("sessions", {}):
        chat_data["sessions"][active_name]["id"] = None
        chat_data["sessions"][active_name]["prompt_count"] = 0
        await save_session_map_atomic(smap)

    active_sessions.pop(chat_id, None)
    logger.info("Session '%s' reset by /new for %s", active_name, mask_chat_id(chat_id))
    await update.message.reply_text(
        "\U0001f504 Sesi\u00f3n '{name}' reiniciada. El pr\u00f3ximo mensaje comenzar\u00e1 desde cero.".format(name=active_name)
    )


@authorized
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    args = context.args
    if not args:
        model = current_model.get(chat_id, DEFAULT_MODEL)
        await update.message.reply_text("Modelo actual: {}".format(model))
        return

    choice = args[0].lower()
    model_value = MODEL_ALIASES.get(choice)
    if model_value:
        current_model[chat_id] = model_value
        smap = await load_session_map_safe()
        chat_data = smap.setdefault(str(chat_id), {})
        chat_data["model"] = model_value
        await save_session_map_atomic(smap)
        await update.message.reply_text("Modelo cambiado a {}".format(model_value))
    else:
        model = current_model.get(chat_id, DEFAULT_MODEL)
        await update.message.reply_text(
            "Uso: /model pro | /model flash\n"
            "Modelo actual: {}".format(model)
        )


@authorized
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    status = process_status.get(chat_id, "idle")
    if status == "cancelling":
        await update.message.reply_text(
            "El prompt ya est\u00e1 siendo cancelado..."
        )
        return
    if status != "running":
        if status == "idle":
            await update.message.reply_text(
                "No hay ning\u00fan prompt en ejecuci\u00f3n."
            )
        else:
            await update.message.reply_text(
                "El prompt ya termin\u00f3."
            )
        return

    process_status[chat_id] = "cancelling"

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


@authorized
async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Uso: /open &lt;prompt&gt;")
        return

    if chat_id in current_process:
        await update.message.reply_text(
            "\u23f3 Ya hay un prompt en proceso. Us\u00e1 /cancel para cancelarlo."
        )
        return

    await _process_prompt(update, chat_id, prompt, context)
