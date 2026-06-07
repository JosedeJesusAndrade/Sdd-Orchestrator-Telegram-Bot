"""Session handlers: /session new|list|switch|delete|info|discover|adopt."""
import re
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from ..config import OPENCODE_CMD, logger
from ..persistence.sessions import (
    load_session_map_safe, save_session_map_atomic,
    fetch_opencode_sessions, invalidate_opencode_sessions_cache,
)
from ..formatting.markdown import (
    minimal_escape_mdv2,
    send_telegram_mdv2,
)
from ..opencode.client import query_opencode_db
from ..utils.logging import mask_chat_id
from . import authorize, active_sessions
from .messages import _relative_time


async def _session_new(update: Update, chat_id: int, name: str | None) -> None:
    """Create a named session (lazy: no OpenCode call yet)."""
    if not name:
        await update.message.reply_text("Uso: /session new <nombre>")
        return

    smap = await load_session_map_safe()
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
    await save_session_map_atomic(smap)
    invalidate_opencode_sessions_cache()

    logger.info("Session '%s' created for %s", name, mask_chat_id(chat_id))
    await update.message.reply_text(
        "\U0001f195 Sesi\u00f3n '{name}' creada y activada.\n"
        "El pr\u00f3ximo prompt se ejecutar\u00e1 en esta sesi\u00f3n.".format(name=name)
    )


async def _session_list(update: Update, chat_id: int) -> None:
    """Show all sessions for this chat."""
    smap = await load_session_map_safe()
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

    smap = await load_session_map_safe()
    chat_data = smap.get(str(chat_id), {})
    chat_sessions = chat_data.get("sessions", {})

    if name not in chat_sessions:
        await update.message.reply_text(
            "\u274c La sesi\u00f3n '{name}' no existe.\n"
            "Us\u00e1 /session list para ver tus sesiones.".format(name=name)
        )
        return

    chat_data["active"] = name
    await save_session_map_atomic(smap)

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

    smap = await load_session_map_safe()
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

    await save_session_map_atomic(smap)
    invalidate_opencode_sessions_cache()
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
    smap = await load_session_map_safe()
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

    if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
        try:
            msg_rows = await query_opencode_db(
                f"SELECT COUNT(*) as count FROM message WHERE session_id = '{oc_id}'",
                allowed_pattern=r'^[a-zA-Z0-9_]+$'
            )
            if msg_rows:
                lines.append("    Mensajes en BD: {cnt}".format(cnt=msg_rows[0].get('count', '?')))

            session_rows = await query_opencode_db(
                f"SELECT created_at, model FROM session WHERE id = '{oc_id}'",
                allowed_pattern=r'^[a-zA-Z0-9_]+$'
            )
            if session_rows:
                row = session_rows[0]
                if row.get("created_at"):
                    lines.append("    Creada (BD): {ca}".format(ca=row['created_at']))
                if row.get("model"):
                    lines.append("    Modelo (BD): {m}".format(m=row['model']))
        except Exception:
            pass

    await update.message.reply_text("\n".join(lines))


async def _session_discover(update: Update, chat_id: int) -> None:
    """Show all OpenCode sessions with adoption status."""
    oc_sessions = await fetch_opencode_sessions()
    smap = await load_session_map_safe()
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
    msg = minimal_escape_mdv2(msg)
    await send_telegram_mdv2(context.bot, chat_id, msg)


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
            "Ejemplo: `/session adopt {id} telegram-bot`".format(id=real_id[:24])
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

    smap = await load_session_map_safe()
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

    await save_session_map_atomic(smap)
    invalidate_opencode_sessions_cache()

    logger.info(
        "Session '%s' adopted (%s) for %s", name, real_id, mask_chat_id(chat_id)
    )

    await update.message.reply_text(
        "\u2705 Sesi\u00f3n '{name}' adoptada (ID: `{id}`)".format(
            name=name, id=real_id
        )
    )


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
