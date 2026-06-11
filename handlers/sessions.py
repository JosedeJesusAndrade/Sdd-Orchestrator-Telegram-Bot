"""Session handlers: /session new|list|switch|delete|info|discover|adopt.
 
Architecture change (Week 2→3):
  All handlers now access services via AppContainer from PTB context
  instead of lazy-importing the bot module.
"""

from __future__ import annotations

import re
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import OPENCODE_CMD, DEFAULT_SESSION_NAME, INTERNAL_SUBPROCESS_TIMEOUT, logger
from persistence.sessions import (
    fetch_opencode_sessions, invalidate_opencode_sessions_cache,
)
from opencode.client import query_opencode_db
from utils.logging import mask_chat_id
from utils.time_formatting import relative_time
from handlers import authorized
from services.session_store import SessionExistsError, SessionNotFoundError
from services.container import AppContainer
from locales import get_strings


def _get_container(context) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return context.application.bot_data["container"]


async def _session_new(update: Update, chat_id: int, name: str | None, container: AppContainer) -> None:
    """Create a named session (lazy: no OpenCode call yet)."""
    S = get_strings()
    if not name:
        await update.message.reply_text(S.SESSION_NEW_USAGE)
        return

    try:
        await container.session_store.create_session(chat_id, name)
    except SessionExistsError:
        await update.message.reply_text(
            S.SESSION_EXISTS.format(name=name) + " "
            "Usá /session switch {name} para cambiarte.".format(name=name)
        )
        return

    invalidate_opencode_sessions_cache()

    logger.info("Session '%s' created for %s", name, mask_chat_id(chat_id))
    await update.message.reply_text(
        S.SESSION_CREATED.format(name=name) + "\n"
        "El próximo prompt se ejecutará en esta sesión."
    )


async def _session_list(update: Update, chat_id: int, container: AppContainer) -> None:
    """Show all sessions for this chat."""
    S = get_strings()
    sessions = await container.session_store.list_sessions(chat_id)

    if not sessions:
        await update.message.reply_text(
            S.SESSION_LIST_EMPTY + " Usá /session new <nombre> para crear una."
        )
        return

    lines = [S.SESSION_LIST_HEADER + "\n"]
    for s in sessions:
        marker = "\U0001f7e2" if s.is_active else "\u26aa"

        if s.real_id:
            id_display = s.real_id[:16] + "..."
            last_used_str = ""
            if s.last_used:
                try:
                    last_dt = datetime.fromisoformat(s.last_used)
                    last_used_str = ", {}".format(relative_time(last_dt))
                except Exception:
                    pass
            lines.append(
                "{marker} {name} → `{id}` ({count} prompts{lu})".format(
                    marker=marker, name=s.name, id=id_display,
                    count=s.prompt_count, lu=last_used_str,
                )
            )
        else:
            lines.append(
                "{marker} {name} → (nueva, sin usar aún)".format(
                    marker=marker, name=s.name,
                )
            )

    await update.message.reply_text("\n".join(lines))


async def _session_switch(update: Update, chat_id: int, name: str | None, container: AppContainer) -> None:
    """Switch active session."""
    S = get_strings()
    if not name:
        await update.message.reply_text(S.SESSION_SWITCH_USAGE)
        return

    try:
        await container.session_store.switch_session(chat_id, name)
    except SessionNotFoundError:
        await update.message.reply_text(
            S.SESSION_NOT_FOUND.format(name=name) + "\n"
            "Usá /session list para ver tus sesiones."
        )
        return

    logger.info(
        "Session switched to '%s' for %s", name, mask_chat_id(chat_id),
    )
    await update.message.reply_text(
        S.SESSION_SWITCHED.format(name=name) + "\n"
        "Próximo prompt usará esta sesión."
    )


async def _session_delete(update: Update, chat_id: int, name: str | None, container: AppContainer) -> None:
    """Delete a named session."""
    S = get_strings()
    if not name:
        await update.message.reply_text(S.SESSION_DELETE_USAGE)
        return

    try:
        real_id = await container.session_store.delete_session(chat_id, name)
    except SessionNotFoundError:
        await update.message.reply_text(
            S.SESSION_NOT_FOUND.format(name=name)
        )
        return

    invalidate_opencode_sessions_cache()

    # Clean up in OpenCode if it had a real ID
    if real_id:
        try:
            proc = await asyncio.create_subprocess_exec(
                OPENCODE_CMD, "session", "delete", real_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=INTERNAL_SUBPROCESS_TIMEOUT)
            logger.info(
                "Deleted OpenCode session %s for chat %s",
                real_id, mask_chat_id(chat_id),
            )
        except Exception as e:
            logger.warning(
                "Failed to delete OpenCode session %s: %s", real_id, e,
            )

    await update.message.reply_text(
        S.SESSION_DELETED.format(name=name)
    )


async def _session_info(update: Update, chat_id: int, name: str | None, container: AppContainer) -> None:
    """Show detailed info about a session."""
    S = get_strings()
    sessions = await container.session_store.list_sessions(chat_id)

    # Determine the target session name
    active_session = await container.session_store.get_active_session(chat_id)
    target_name = name if name else (active_session.name if active_session else DEFAULT_SESSION_NAME)

    # Find the matching SessionInfo
    target = next((s for s in sessions if s.name == target_name), None)
    if target is None:
        await update.message.reply_text(
            S.SESSION_NOT_FOUND.format(name=target_name)
        )
        return

    oc_id = target.real_id
    title = target.title
    created = target.created
    last_used = target.last_used
    prompt_count = target.prompt_count

    id_display = S.SESSION_INFO_NO_ID if not oc_id else (
        oc_id[:24] + "..." if len(oc_id) > 24 else oc_id
    )

    last_used_str = "nunca"
    if last_used:
        try:
            last_dt = datetime.fromisoformat(last_used)
            last_used_str = relative_time(last_dt)
        except Exception:
            last_used_str = str(last_used)

    created_display = created[:19] if created and created != "\u2014" else created

    lines = [
        S.SESSION_INFO_HEADER.format(name=target_name),
        "    ID: {id}".format(id=id_display),
        "    Título: {title}".format(title=title),
        "    Creada: {created}".format(created=created_display),
        "    Último uso: {lu}".format(lu=last_used_str),
        "    Prompts: {count}".format(count=prompt_count),
    ]

    if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
        try:
            msg_rows = await query_opencode_db(
                f"SELECT COUNT(*) as count FROM message WHERE session_id = '{oc_id}'",
                allowed_pattern=r'^[a-zA-Z0-9_]+$',
            )
            if msg_rows:
                lines.append(
                    "    Mensajes en BD: {cnt}".format(
                        cnt=msg_rows[0].get('count', '?'),
                    )
                )

            session_rows = await query_opencode_db(
                f"SELECT created_at, model FROM session WHERE id = '{oc_id}'",
                allowed_pattern=r'^[a-zA-Z0-9_]+$',
            )
            if session_rows:
                row = session_rows[0]
                if row.get("created_at"):
                    lines.append(
                        "    Creada (BD): {ca}".format(ca=row['created_at']),
                    )
                if row.get("model"):
                    lines.append(
                        "    Modelo (BD): {m}".format(m=row['model']),
                    )
        except Exception:
            pass

    await update.message.reply_text("\n".join(lines))


async def _session_discover(update: Update, chat_id: int, container: AppContainer) -> None:
    """Show all OpenCode sessions with adoption status."""
    S = get_strings()
    oc_sessions = await fetch_opencode_sessions()
    sessions = await container.session_store.list_sessions(chat_id)

    # Build adoption map
    adopted_map: dict[str, str] = {}
    for s in sessions:
        if s.real_id:
            adopted_map[s.real_id] = s.name

    if not oc_sessions:
        await update.message.reply_text(
            S.SESSION_DISCOVER_EMPTY
        )
        return

    lines = [S.SESSION_DISCOVER_HEADER + "\n"]

    for s in oc_sessions[:15]:
        sid = s["id"]
        title = s["title"][:60]

        friendly_name = adopted_map.get(sid)
        if friendly_name:
            matching = next((x for x in sessions if x.name == friendly_name), None)
            prompt_count = matching.prompt_count if matching else 0
            lines.append(
                "\u2705 *{name}* → `{sid}` (ya adoptada, {count} prompts)".format(
                    name=friendly_name, sid=sid, count=prompt_count,
                )
            )
        else:
            lines.append(
                "\u26aa *{title}*\n"
                "   `{sid}`\n"
                "   Para adoptar: `/session adopt {sid} <nombre>`".format(
                    title=title, sid=sid,
                )
            )
        lines.append("")

    if len(oc_sessions) > 15:
        lines.append(
            "Mostrando 15 de {} sesiones.".format(len(oc_sessions))
        )

    lines.append(
        "\nUsa `/session adopt <id> <nombre>` para adoptar una sesión."
    )

    msg = "\n".join(lines)

    # Use MessageSender for delivery
    await container.message_sender.send_formatted(chat_id, msg)


async def _session_adopt(
    update: Update, chat_id: int, real_id: str | None, name: str | None,
    container: AppContainer,
) -> None:
    """Adopt an existing OpenCode session by real ID."""
    if not real_id or not name:
        await update.message.reply_text(
            "\u274c Uso: /session adopt <id> <nombre>"
        )
        return

    if name == "<nombre>":
        await update.message.reply_text(
            "\u274c Reemplazá `<nombre>` con un nombre para la sesión.\n"
            "Ejemplo: `/session adopt {id} telegram-bot`".format(id=real_id[:24])
        )
        return

    if not re.match(r'^[a-zA-Z0-9_-]{1,30}$', name):
        await update.message.reply_text(
            "\u274c El nombre debe tener entre 1 y 30 caracteres y solo usar "
            "letras, números, guiones y guiones bajos.\n"
            "Ejemplo: `mi-sesion`, `balanceate_api`, `docs`"
        )
        return

    oc_sessions = await fetch_opencode_sessions()
    oc_by_id = {s["id"]: s for s in oc_sessions}

    if real_id not in oc_by_id:
        await update.message.reply_text(
            "\u274c La sesión `{id}` no existe en OpenCode.".format(id=real_id)
        )
        return

    # Check if name already exists
    existing = await container.session_store.list_sessions(chat_id)
    if any(s.name == name for s in existing):
        await update.message.reply_text(
            "\u26a0\ufe0f El nombre '{name}' ya existe. "
            "Usá /session switch {name} para usarla.".format(name=name)
        )
        return

    # Adopt: we'll create the session entry manually since adopt has
    # extra logic (fetching title from OpenCode)
    from persistence.sessions import load_session_map_safe, save_session_map_atomic

    smap = await load_session_map_safe()
    chat_data = smap.setdefault(str(chat_id), {})
    chat_sessions = chat_data.setdefault("sessions", {})

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

    await save_session_map_atomic(smap)
    invalidate_opencode_sessions_cache()

    logger.info(
        "Session '%s' adopted (%s) for %s",
        name, real_id, mask_chat_id(chat_id),
    )

    await update.message.reply_text(
        "\u2705 Sesión '{name}' adoptada (ID: `{id}`)".format(
            name=name, id=real_id,
        )
    )


@authorized
async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /session subcommands."""
    chat_id = update.effective_chat.id
    container = _get_container(context)
    S = get_strings()

    args = context.args
    if not args:
        await update.message.reply_text(S.SESSION_USAGE)
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
        await update.message.reply_text(S.SESSION_UNKNOWN_SUB.format(sub=subcommand))
