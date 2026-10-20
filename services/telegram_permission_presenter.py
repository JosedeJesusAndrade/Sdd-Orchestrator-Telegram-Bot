"""TelegramPermissionPresenter — the Telegram frontend for permission requests.

Why this is a frontend adapter (and imports ``telegram``): the shared
``BotPort`` only models plain message transport, while a permission prompt
needs an inline keyboard and later an edit that removes it. Keeping PTB out of
the core would force a second transport abstraction for one feature, so this
module is allowed to talk to ``telegram.Bot`` directly (see the design).

Design notes:
  - The message is PLAIN TEXT (no ``parse_mode``). Permission patterns are
    arbitrary shell text (``git commit *``, quotes, backslashes...), which
    MarkdownV2 would reject; plain text is correct by construction.
  - ``resolve`` appends the outcome and removes the keyboard. Editing with an
    empty ``InlineKeyboardMarkup`` is how the Bot API removes an inline
    keyboard (omitting ``reply_markup`` would leave it in place).
  - The presenter stores which messages it sent so ``resolve`` can edit them.
    Edit failures are expected (the user may have deleted the message) and are
    logged at DEBUG only — a resolution must never crash the router/backend.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from services.permissions import PermissionDecision, PermissionRequest
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

CALLBACK_PREFIX = "perm:"
CALLBACK_PATTERN = r"^perm:"

ONCE_LABEL = "✅ Permitir"
ALWAYS_LABEL = "🔁 Siempre"
REJECT_LABEL = "❌ Rechazar"

_ONCE_REPLY = "once"
_ALWAYS_REPLY = "always"
_REJECT_REPLY = "reject"

RESULT_PERMITTED = "✅ Permitido"
RESULT_PERMITTED_ALWAYS = "✅ Permitido (siempre)"
RESULT_REJECTED = "❌ Rechazado"
RESULT_TIMED_OUT = "⏱️ Permiso rechazado automáticamente (timeout)"

#: ``(chat_id, message_id, text)`` — the text is kept so ``resolve`` can append
#: the outcome without re-rendering the request (the metadata may be gone).
_SentMessage = tuple[int, int, str]


def format_permission_request(request: PermissionRequest) -> str:
    """Render the request as a short, plain-text, mobile-friendly message."""
    lines = ["🔐 Permiso requerido", "", f"Acción: {request.permission}"]

    if len(request.patterns) == 1:
        lines.append(f"Patrón: {request.patterns[0]}")
    elif request.patterns:
        lines.append("Patrones:")
        lines.extend(f"  • {pattern}" for pattern in request.patterns)

    command = request.metadata.get("command")
    if isinstance(command, str) and command:
        lines.append(f"Comando: {command}")

    if request.always:
        lines.append(f"Siempre: {', '.join(request.always)}")

    return "\n".join(lines)


def result_text(decision: PermissionDecision) -> str:
    """Return the one-line outcome appended by ``resolve``."""
    if decision.reply == _REJECT_REPLY:
        return RESULT_TIMED_OUT if decision.timed_out else RESULT_REJECTED
    if decision.reply == _ALWAYS_REPLY:
        return RESULT_PERMITTED_ALWAYS
    return RESULT_PERMITTED


def build_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """Build the three-button inline keyboard for a request id."""
    buttons = [
        InlineKeyboardButton(
            label,
            callback_data=f"{CALLBACK_PREFIX}{request_id}:{reply}",
        )
        for label, reply in (
            (ONCE_LABEL, _ONCE_REPLY),
            (ALWAYS_LABEL, _ALWAYS_REPLY),
            (REJECT_LABEL, _REJECT_REPLY),
        )
    ]
    return InlineKeyboardMarkup([buttons])


class TelegramPermissionPresenter:
    """Implements :class:`~services.permissions.PermissionPresenter`."""

    def __init__(self, bot: Bot, chat_ids: Sequence[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(chat_ids)
        self._sent: dict[str, list[_SentMessage]] = {}

    async def show(self, request: PermissionRequest) -> None:
        """Send the prompt + keyboard to every configured chat."""
        text = format_permission_request(request)
        keyboard = build_keyboard(request.id)
        sent: list[_SentMessage] = []

        for chat_id in self._chat_ids:
            try:
                message = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except Exception as e:
                # One unreachable chat must not prevent the others from seeing
                # the request (the router also isolates presenter failures).
                logger.warning(
                    "Permission prompt could not be sent",
                    extra={
                        "event": "permission_prompt_send_failed",
                        "request_id": request.id,
                        "chat_id": chat_id,
                        "error_type": type(e).__name__,
                    },
                )
                continue
            sent.append((chat_id, message.message_id, text))

        if sent:
            self._sent[request.id] = sent
            logger.info(
                "Permission prompt sent",
                extra={
                    "event": "permission_prompt_sent",
                    "request_id": request.id,
                    "session_id": request.session_id,
                    "chats": len(sent),
                },
            )
        logger.info(
            "Permission presenter show",
            extra={
                "event": "permission_presenter_show",
                "presenter": "telegram",
                "request_id": request.id,
                "chats": len(sent),
                "configured_chats": len(self._chat_ids),
            },
        )

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
        """Append the outcome and remove the keyboard from every message."""
        sent = self._sent.pop(request_id, None)
        if not sent:
            logger.info(
                "Permission presenter resolve",
                extra={
                    "event": "permission_presenter_resolve",
                    "presenter": "telegram",
                    "request_id": request_id,
                    "reply": decision.reply,
                    "sent_messages": 0,
                },
            )
            return

        outcome = result_text(decision)
        # Empty markup = remove the inline keyboard (omitting reply_markup would
        # leave the buttons clickable after the request is already resolved).
        empty_keyboard = InlineKeyboardMarkup([])

        for chat_id, message_id, original_text in sent:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"{original_text}\n\n{outcome}",
                    reply_markup=empty_keyboard,
                )
            except Exception as e:
                log_exception(
                    "permission_prompt_edit_failed",
                    module=__name__,
                    level=logging.DEBUG,
                    request_id=request_id,
                    chat_id=chat_id,
                    error_type=type(e).__name__,
                )

        logger.info(
            "Permission presenter resolve",
            extra={
                "event": "permission_presenter_resolve",
                "presenter": "telegram",
                "request_id": request_id,
                "reply": decision.reply,
                "sent_messages": len(sent),
            },
        )
