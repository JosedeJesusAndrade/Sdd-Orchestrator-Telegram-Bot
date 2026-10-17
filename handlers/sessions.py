"""Session handlers: /session new|list|switch|delete|info|discover|adopt.

Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from telegram import Update
from telegram.ext import ContextTypes

from config import CONTAINER_KEY, DEFAULT_SESSION_NAME
from handlers import authorized
from handlers._guards import require_chat, require_message
from locales import get_strings
from services.container import AppContainer
from services.session_service import InvalidSessionNameError, SessionNotDiscoveredError
from services.session_store import SessionExistsError, SessionNotFoundError
from utils.logging import get_module_logger, mask_chat_id
from utils.time_formatting import relative_time

logger = get_module_logger(__name__)


def _get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return cast(AppContainer, context.application.bot_data[CONTAINER_KEY])


async def _session_new(
    update: Update, chat_id: int, name: str | None, container: AppContainer
) -> None:
    """Create a named session (lazy: no OpenCode call yet)."""
    msg = require_message(update)
    strings = get_strings()
    if not name:
        await msg.reply_text(strings.SESSION_NEW_USAGE)
        return

    try:
        await container.session_service.create(chat_id, name)
    except SessionExistsError:
        await msg.reply_text(
            strings.SESSION_EXISTS.format(name=name) + " "
            f"Usá /session switch {name} para cambiarte."
        )
        return

    logger.info("Session '%s' created for %s", name, mask_chat_id(chat_id))
    await msg.reply_text(
        strings.SESSION_CREATED.format(name=name) + "\n"
        "El próximo prompt se ejecutará en esta sesión."
    )


async def _session_list(update: Update, chat_id: int, container: AppContainer) -> None:
    """Show all sessions — bot-managed + OpenCode-discovered, unified."""
    msg = require_message(update)
    strings = get_strings()

    snapshot = await container.session_service.list_with_discovered(chat_id)
    bot_sessions = snapshot.sessions
    unadopted = snapshot.unadopted

    if not bot_sessions and not unadopted:
        await msg.reply_text(
            strings.SESSION_LIST_EMPTY + " Usá /session new <nombre> para crear una."
        )
        return

    lines = [strings.SESSION_LIST_HEADER + "\n"]

    # Bot sessions first (with adoption status from OpenCode)
    for s in bot_sessions:
        marker = "\U0001f7e2" if s.is_active else "\u26aa"
        if s.real_id:
            id_display = s.real_id[:16] + "..."
            last_used_str = ""
            if s.last_used:
                try:
                    last_dt = datetime.fromisoformat(s.last_used)
                    last_used_str = f", {relative_time(last_dt)}"
                except Exception:
                    # Malformed timestamp: omit the ", hace X" suffix.
                    pass
            lines.append(
                f"{marker} {s.name} → `{id_display}` ({s.prompt_count} prompts{last_used_str})"
            )
        else:
            lines.append(f"{marker} {s.name} → (nueva, sin usar aún)")

    # OpenCode unadopted sessions
    if unadopted:
        lines.append("\n\u26aa *Sesiones OpenCode no adoptadas:*")
        lines.append("_Usá /session adopt `<id>` `<nombre>` para adoptarlas._\n")
        for oc in unadopted:
            sid = oc.id
            title = oc.title
            lines.append(f"\u26aa `{title}`\n   `{sid}`")

    await msg.reply_text("\n".join(lines))


async def _session_switch(
    update: Update, chat_id: int, name: str | None, container: AppContainer
) -> None:
    """Switch active session."""
    msg = require_message(update)
    strings = get_strings()
    if not name:
        await msg.reply_text(strings.SESSION_SWITCH_USAGE)
        return

    try:
        await container.session_service.switch(chat_id, name)
    except SessionNotFoundError:
        await msg.reply_text(
            strings.SESSION_NOT_FOUND.format(name=name) + "\n"
            "Usá /session list para ver tus sesiones."
        )
        return

    logger.info(
        "Session switched to '%s' for %s",
        name,
        mask_chat_id(chat_id),
    )
    await msg.reply_text(
        strings.SESSION_SWITCHED.format(name=name) + "\nPróximo prompt usará esta sesión."
    )


async def _session_delete(
    update: Update, chat_id: int, name: str | None, container: AppContainer
) -> None:
    """Delete a named session."""
    msg = require_message(update)
    strings = get_strings()
    if not name:
        await msg.reply_text(strings.SESSION_DELETE_USAGE)
        return

    try:
        await container.session_service.delete(chat_id, name)
    except SessionNotFoundError:
        await msg.reply_text(strings.SESSION_NOT_FOUND.format(name=name))
        return

    await msg.reply_text(strings.SESSION_DELETED.format(name=name))


async def _session_info(
    update: Update, chat_id: int, name: str | None, container: AppContainer
) -> None:
    """Show detailed info about a session."""
    msg = require_message(update)
    strings = get_strings()
    sessions = await container.session_store.list_sessions(chat_id)

    # Determine the target session name
    active_session = await container.session_store.get_active_session(chat_id)
    target_name = (
        name if name else (active_session.name if active_session else DEFAULT_SESSION_NAME)
    )

    # Find the matching SessionInfo
    target = next((s for s in sessions if s.name == target_name), None)
    if target is None:
        await msg.reply_text(strings.SESSION_NOT_FOUND.format(name=target_name))
        return

    oc_id = target.real_id
    title = target.title
    created = target.created
    last_used = target.last_used
    prompt_count = target.prompt_count

    id_display = (
        strings.SESSION_INFO_NO_ID
        if not oc_id
        else (oc_id[:24] + "..." if len(oc_id) > 24 else oc_id)
    )

    last_used_str = "nunca"
    if last_used:
        try:
            last_dt = datetime.fromisoformat(last_used)
            last_used_str = relative_time(last_dt)
        except Exception:
            # Malformed timestamp: fall back to the raw value.
            last_used_str = str(last_used)

    created_display = created[:19] if created and created != "\u2014" else created

    lines = [
        strings.SESSION_INFO_HEADER.format(name=target_name),
        f"    ID: {id_display}",
        f"    Título: {title}",
        f"    Creada: {created_display}",
        f"    Último uso: {last_used_str}",
        f"    Prompts: {prompt_count}",
    ]

    enrichment = await container.session_service.enrich(oc_id)
    if "message_count" in enrichment:
        lines.append(
            "    Mensajes en BD: {cnt}".format(
                cnt=enrichment["message_count"],
            )
        )
    if "created_at" in enrichment:
        lines.append(
            "    Creada (BD): {ca}".format(ca=enrichment["created_at"]),
        )
    if "model" in enrichment:
        lines.append(
            "    Modelo (BD): {m}".format(m=enrichment["model"]),
        )

    await msg.reply_text("\n".join(lines))


async def _session_discover(update: Update, chat_id: int, container: AppContainer) -> None:
    """Show all OpenCode sessions with adoption status."""
    msg = require_message(update)
    strings = get_strings()
    discovered = await container.session_service.discover(chat_id)

    if not discovered:
        await msg.reply_text(strings.SESSION_DISCOVER_EMPTY)
        return

    lines = [strings.SESSION_DISCOVER_HEADER + "\n"]

    for oc in discovered[:15]:
        sid = oc.id
        title = oc.title[:60]

        if oc.adopted_name:
            lines.append(
                f"\u2705 *{oc.adopted_name}* → `{sid}` (ya adoptada, {oc.prompt_count} prompts)"
            )
        else:
            lines.append(
                f"\u26aa *{title}*\n   `{sid}`\n   Para adoptar: `/session adopt {sid} <nombre>`"
            )
        lines.append("")

    if len(discovered) > 15:
        lines.append(f"Mostrando 15 de {len(discovered)} sesiones.")

    lines.append("\nUsa `/session adopt <id> <nombre>` para adoptar una sesión.")

    text = "\n".join(lines)

    # Use MessageSender for delivery
    await container.message_sender.send_formatted(chat_id, text)


async def _session_adopt(
    update: Update,
    chat_id: int,
    real_id: str | None,
    name: str | None,
    container: AppContainer,
) -> None:
    """Adopt an existing OpenCode session by real ID."""
    msg = require_message(update)
    if not real_id or not name:
        await msg.reply_text("\u274c Uso: /session adopt <id> <nombre>")
        return

    if name == "<nombre>":
        await msg.reply_text(
            "\u274c Reemplazá `<nombre>` con un nombre para la sesión.\n"
            f"Ejemplo: `/session adopt {real_id[:24]} telegram-bot`"
        )
        return

    try:
        await container.session_service.adopt(chat_id, real_id, name)
    except InvalidSessionNameError:
        await msg.reply_text(
            "\u274c El nombre debe tener entre 1 y 30 caracteres y solo usar "
            "letras, números, guiones y guiones bajos.\n"
            "Ejemplo: `mi-sesion`, `balanceate_api`, `docs`"
        )
        return
    except SessionNotDiscoveredError:
        await msg.reply_text(f"\u274c La sesión `{real_id}` no existe en OpenCode.")
        return
    except SessionExistsError:
        await msg.reply_text(
            f"\u26a0\ufe0f El nombre '{name}' ya existe. Usá /session switch {name} para usarla."
        )
        return

    await msg.reply_text(f"\u2705 Sesión '{name}' adoptada (ID: `{real_id}`)")


@authorized
async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /session subcommands."""
    msg = require_message(update)
    chat_id = require_chat(update).id
    container = _get_container(context)
    strings = get_strings()

    args = context.args
    if not args:
        await msg.reply_text(strings.SESSION_USAGE)
        return

    subcommand = args[0].lower()
    name = args[1] if len(args) > 1 else None

    if subcommand == "new":
        await _session_new(update, chat_id, name, container)
    elif subcommand == "list":
        await _session_list(update, chat_id, container)
    elif subcommand == "switch":
        await _session_switch(update, chat_id, name, container)
    elif subcommand == "delete":
        await _session_delete(update, chat_id, name, container)
    elif subcommand == "info":
        await _session_info(update, chat_id, name, container)
    elif subcommand == "discover":
        await _session_discover(update, chat_id, container)
    elif subcommand == "adopt":
        real_id = args[1] if len(args) > 1 else None
        adopt_name = args[2] if len(args) > 2 else None
        await _session_adopt(update, chat_id, real_id, adopt_name, container)
    else:
        await msg.reply_text(strings.SESSION_UNKNOWN_SUB.format(sub=subcommand))
