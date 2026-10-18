"""Telegram callback handler for permission buttons.

The inline keyboard built by ``TelegramPermissionPresenter`` sends
``callback_data="perm:<request_id>:<once|always|reject>"``. This handler is the
only place that turns that click into a ``PermissionDecision``:

  1. Parse + validate the callback payload (malformed clicks are answered, not
     crashed).
  2. ``permission_router.resolve_from_ui(...)`` — the router is first-wins and
     returns ``False`` for unknown/already-resolved requests.
  3. Answer the callback (the little toast Telegram shows on the button).

It deliberately does NOT edit the message: the router notifies every presenter
after a successful resolution, and ``TelegramPermissionPresenter.resolve``
owns the edit. Editing here would race the presenter and double-edit the same
message.

Auth: ``@authorized`` is used on purpose. ``Update.effective_chat`` is present
on callback queries (it is the chat that owns the original message), so the
same allow-list gate that protects every command also protects the buttons;
the keyboard is only ever delivered to allowed chats in the first place.
"""

from __future__ import annotations

import logging
from typing import cast

from telegram import CallbackQuery, Update
from telegram.ext import ContextTypes

from config import CONTAINER_KEY
from handlers import authorized
from services.container import AppContainer
from services.permissions import PermissionDecision, Reply
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

ALREADY_RESOLVED_TOAST = "Ya fue resuelto"
INVALID_CALLBACK_TOAST = "Solicitud inválida"

_TOASTS: dict[str, str] = {
    "once": "✅ Permitido",
    "always": "✅ Permitido (siempre)",
    "reject": "❌ Rechazado",
}
_VALID_REPLIES = frozenset(_TOASTS)


def _get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return cast(AppContainer, context.application.bot_data[CONTAINER_KEY])


async def _answer(query: CallbackQuery, text: str) -> None:
    """Answer the callback, never letting an expired query crash the handler."""
    try:
        await query.answer(text)
    except Exception as e:
        log_exception(
            "permission_callback_answer_failed",
            module=__name__,
            level=logging.DEBUG,
            error_type=type(e).__name__,
        )


def _parse_callback(data: str) -> tuple[str, str] | None:
    """Return ``(request_id, reply)`` for a valid payload, else ``None``."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "perm":
        return None
    _, request_id, reply = parts
    if not request_id or reply not in _VALID_REPLIES:
        return None
    return request_id, reply


@authorized
async def permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve a pending permission request from an inline-button click."""
    query = update.callback_query
    if query is None:
        logger.warning(
            "Permission callback received without a callback query",
            extra={"event": "permission_callback_missing_query"},
        )
        return

    data = query.data or ""
    parsed = _parse_callback(data)
    if parsed is None:
        logger.warning(
            "Invalid permission callback payload",
            extra={"event": "permission_callback_invalid", "callback_data": data[:64]},
        )
        await _answer(query, INVALID_CALLBACK_TOAST)
        return

    request_id, reply = parsed
    router = _get_container(context).permission_router
    if router is None:
        # CLI rollback path: nothing can be pending, so the button is stale.
        logger.debug(
            "Permission callback ignored: no router (CLI backend)",
            extra={"event": "permission_callback_no_router", "request_id": request_id},
        )
        await _answer(query, ALREADY_RESOLVED_TOAST)
        return

    decision = PermissionDecision(reply=cast(Reply, reply))
    resolved = await router.resolve_from_ui(request_id, decision)

    if not resolved:
        logger.info(
            "Permission callback ignored: already resolved or unknown",
            extra={"event": "permission_callback_unresolved", "request_id": request_id},
        )
        await _answer(query, ALREADY_RESOLVED_TOAST)
        return

    logger.info(
        "Permission resolved from Telegram",
        extra={
            "event": "permission_callback_resolved",
            "request_id": request_id,
            "reply": reply,
        },
    )
    await _answer(query, _TOASTS[reply])
