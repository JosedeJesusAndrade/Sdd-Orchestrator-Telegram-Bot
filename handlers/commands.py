"""Command handlers: /start, /help, /status, /model, /cancel, /new, /open.

Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module. This eliminates the
  `import bot; bot.X` pattern entirely.

Phase 2 logging:
  - Per-module logger via `__name__`
  - `event=cmd_new` for /new, `event=prompt_cancelled` for /cancel
"""

from __future__ import annotations

from typing import cast

from telegram import Update
from telegram.ext import ContextTypes

from config import CONTAINER_KEY, DEFAULT_SESSION_NAME, resolve_model
from handlers import authorized
from handlers._guards import require_chat, require_message
from locales import get_strings
from services.container import AppContainer
from services.prompt_service import PromptAlreadyRunningError
from services.settings_service import InvalidSettingValueError, UnknownSettingError
from services.telegram_chat_view import TelegramChatView
from utils.logging import get_module_logger, mask_chat_id
from utils.time_formatting import relative_time

logger = get_module_logger(__name__)


def _get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return cast(AppContainer, context.application.bot_data[CONTAINER_KEY])


@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = require_message(update)
    await msg.reply_text(
        "OpenCode Bot listo.\n\n"
        "Envía cualquier mensaje y se ejecutará con OpenCode CLI.\n"
        "Soporta múltiples sesiones con /session new|list|switch|delete|info\n"
        "/help — ver todos los comandos\n"
        "/status — estado de la sesión\n"
        "/new — reiniciar sesión activa"
    )


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = require_message(update)
    await msg.reply_text(
        "*Comandos disponibles:*\n\n"
        "/open `<prompt>` — Enviar prompt al orquestador\n"
        "/model `<alias>` — Cambiar modelo (deepseek: pro, flash | minimax: m3, m27, m27-fast)\n"
        "/config `[key] [value]` — Configuración personal (modelo, timeout, etc.)\n"
        "/health — Estado del bot (versión, uptime, sesiones)\n"
        "/cancel — Cancelar prompt en ejecución\n"
        "/status — Ver estado de la sesión\n"
        "/new — Reiniciar sesión activa\n"
        "/session new `<nombre>` — Crear sesión nombrada\n"
        "/session list — Listar todas las sesiones\n"
        "/session switch `<nombre>` — Cambiar sesión activa\n"
        "/session delete `<nombre>` — Eliminar sesión\n"
        "/session info `[nombre]` — Detalles de una sesión\n"
        "/session discover — Descubrir sesiones OpenCode existentes\n"
        "/session adopt `<id>` `<nombre>` — Adoptar una sesión OpenCode\n"
        "/wdir `[proyecto]` — Listar o cambiar proyecto OpenCode\n"
        "/pr `<título>` — Crear Pull Request en GitHub\n"
        "/update — Reiniciar bot con la última versión\n"
        "/help — Este mensaje\n\n"
        "También podés enviar mensajes directamente o notas de voz."
    )


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session status via StatusService (presentation only)."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    container = _get_container(context)

    snapshot = await container.status_service.snapshot(chat_id)

    # Build session info
    if snapshot.session_name is not None:
        session_name = snapshot.session_name
        session_id_display = (
            snapshot.session_real_id[:24] + "..."
            if snapshot.session_real_id and len(snapshot.session_real_id) > 24
            else (snapshot.session_real_id or "pendiente")
        )
        first_msg = "pendiente"
        if snapshot.created is not None:
            first_msg = relative_time(snapshot.created)
        elif snapshot.created_fallback is not None:
            first_msg = snapshot.created_fallback[:19]

        last_used = "N/A"
        if snapshot.last_used is not None:
            last_used = relative_time(snapshot.last_used)
        elif snapshot.last_used_fallback is not None:
            last_used = snapshot.last_used_fallback

        prompt_count = snapshot.prompt_count
    else:
        session_name = "-"
        session_id_display = "-"
        first_msg = "N/A"
        last_used = "N/A"
        prompt_count = 0

    # Uptime from the snapshot
    if snapshot.uptime_seconds is not None:
        uptime_seconds = snapshot.uptime_seconds
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"
    else:
        uptime_str = "desconocido"

    await msg.reply_text(
        "\U0001f4ca Estado de la sesión\n"
        f"\u251c\u2500 Nombre: {session_name}\n"
        f"\u251c\u2500 ID OpenCode: {session_id_display}\n"
        f"\u251c\u2500 Modelo: {snapshot.model}\n"
        f"\u251c\u2500 Rama: {snapshot.branch}\n"
        f"\u251c\u2500 Primera interacción: {first_msg}\n"
        f"\u251c\u2500 Última interacción: {last_used}\n"
        f"\u251c\u2500 Total prompts: {prompt_count}\n"
        f"\u2514\u2500 Uptime del bot: {uptime_str}"
    )


@authorized
async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the active session's OpenCode ID — next prompt starts fresh."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    container = _get_container(context)

    await container.session_store.reset_session(chat_id)

    active = await container.session_store.get_active_session(chat_id)
    active_name = active.name if active else DEFAULT_SESSION_NAME

    # Note: session_store.reset_session() already emits event=session_reset.
    # This command-level event records the user-initiated action.
    logger.info(
        "Command /new invoked",
        extra={
            "event": "cmd_new",
            "chat_id_masked": mask_chat_id(chat_id),
            "session_name": active_name,
        },
    )
    await msg.reply_text(
        f"\U0001f504 Sesión '{active_name}' reiniciada. El próximo mensaje comenzará desde cero."
    )


@authorized
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or change the AI model using SessionStore."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    args = context.args
    container = _get_container(context)
    strings = get_strings()

    if not args:
        model = await container.session_store.get_model(chat_id)
        await msg.reply_text(strings.MODEL_CURRENT.format(model=model))
        return

    model_value = resolve_model(args[0].lower())
    await container.session_store.set_model(chat_id, model_value)
    await msg.reply_text(strings.MODEL_CHANGED.format(model=model_value))


@authorized
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a running prompt using PromptService."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    container = _get_container(context)
    strings = get_strings()

    if not container.prompt_service.is_running(chat_id):
        await msg.reply_text(strings.CANCEL_NO_PROMPT)
        return

    cancelled = container.prompt_service.cancel(chat_id)
    if cancelled:
        logger.info(
            "Prompt cancelled by user",
            extra={
                "event": "prompt_cancelled",
                "chat_id_masked": mask_chat_id(chat_id),
            },
        )
        await msg.reply_text(strings.CANCEL_DONE)
    else:
        await msg.reply_text(strings.CANCEL_NO_PROMPT)


@authorized
async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute a prompt via /open <text> using PromptService."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    container = _get_container(context)
    strings = get_strings()

    prompt = " ".join(cast(list[str], context.args))
    if not prompt:
        await msg.reply_text(strings.OPEN_USAGE)
        return

    try:
        await container.prompt_service.execute(
            conversation_id=chat_id,
            prompt_text=prompt,
            view=TelegramChatView(container.message_sender, chat_id),
        )
    except PromptAlreadyRunningError:
        await msg.reply_text(strings.OPEN_BUSY)


@authorized
async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = require_message(update)
    chat_id = require_chat(update).id
    args = context.args
    container = _get_container(context)

    strings = get_strings()

    if not args:
        settings = await container.settings_service.get(chat_id)
        lines = [strings.CONFIG_HEADER + "\n"]
        lines.append(f"  modelo: `{settings.model}`")
        lines.append(f"  timeout: `{settings.timeout}s`")
        lines.append(f"  workdir: `{settings.workdir}`")
        lines.append(f"  provider: `{settings.provider}`")
        await msg.reply_text("\n".join(lines))
        return

    key = args[0].lower()
    try:
        if len(args) < 2:
            value = await container.settings_service.get_value(chat_id, key)
            await msg.reply_text(f"{key} = `{value}`")
            return
        value = await container.settings_service.set(chat_id, key, args[1])
    except UnknownSettingError:
        await msg.reply_text(strings.CONFIG_UNKNOWN_KEY.format(key=key))
        return
    except InvalidSettingValueError:
        await msg.reply_text(strings.CONFIG_INVALID_TIMEOUT)
        return

    if key == "timeout":
        await msg.reply_text(strings.CONFIG_TIMEOUT_SET.format(value=value))
    else:
        await msg.reply_text(strings.CONFIG_SET.format(key=key, value=value))


@authorized
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health check: /health — versión, uptime, conectividad, sesiones."""
    container = _get_container(context)
    chat_id = require_chat(update).id
    sender = container.message_sender

    snapshot = await container.health_service.snapshot(
        chat_id, connectivity_probe=container.bot_port
    )

    h, m = divmod(snapshot.uptime_seconds, 3600)
    m, s = divmod(m, 60)

    if snapshot.connected is None:
        connected = "n/a"
    elif snapshot.connected:
        connected = "✅ Conectado"
    else:
        connected = "❌ Sin conexión"

    oc_count: int | str = "?" if snapshot.oc_session_count is None else snapshot.oc_session_count

    lines = [
        "🩺 *Health Check*\n",
        f"📦 v{snapshot.version}",
        f"🤖 @{snapshot.bot_user} | {connected}",
        f"⏱️ Uptime: {h}h {m}m {s}s",
        f"🐍 Python {snapshot.python}",
        f"💾 Sesiones OpenCode: {oc_count}",
        f"📁 Workdir: `{snapshot.workdir}`",
        f"🧠 Modelo: `{snapshot.model}`",
    ]
    await sender.send_plain(chat_id, "\n".join(lines))
