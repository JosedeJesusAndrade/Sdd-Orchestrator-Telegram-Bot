"""Handler package: shared authorization decorator with structured logging.

This module now contains:
  - `authorize(chat_id)` — pure check, no I/O
  - `@authorized` — decorator that gates handler execution and logs every
    call with structured events (handler_entry, handler_exit, unauthorized)

Session state has been moved to services/session_store.py (SessionStore).
Process tracking has been moved to services/prompt_service.py (PromptService).
"""

import functools
import time
from typing import Any, Awaitable, Callable, Concatenate, Coroutine, ParamSpec, cast

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_IDS
from utils.logging import get_module_logger, mask_chat_id, new_request_id
from handlers._guards import require_chat

logger = get_module_logger(__name__)

P = ParamSpec("P")


def authorize(chat_id: int) -> bool:
    """Return True if chat_id is in the allowed list."""
    return chat_id in ALLOWED_CHAT_IDS


def authorized(
    handler: Callable[Concatenate[Update, ContextTypes.DEFAULT_TYPE, P], Awaitable[None]],
) -> Callable[Concatenate[Update, ContextTypes.DEFAULT_TYPE, P], Coroutine[Any, Any, None]]:
    """Decorator: only allow authorized chat_ids to execute the handler.

    Responsibilities (in order):
      1. Generate a fresh request_id (visible as [req=...] in file logs)
      2. Log handler_entry DEBUG event with handler name + masked chat_id
      3. Extract the Update from positional/keyword args
      4. Authorize the chat_id — log `unauthorized` WARNING if rejected
      5. Time the handler execution
      6. Log handler_exit DEBUG event with duration_ms
      7. Return the handler's result

    Event coverage (Phase 2 logging improvements):
      - DEBUG event=handler_entry   at handler start
      - DEBUG event=handler_exit    at handler end (with duration_ms)
      - WARNING event=unauthorized  if chat_id not in ALLOWED_CHAT_IDS
    """
    @functools.wraps(handler)
    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        # Fresh request_id for this invocation — visible in file logs as [req=...]
        new_request_id()

        if update is None:
            logger.error(
                "Update missing in authorized handler",
                extra={
                    "event": "authorized_missing_update",
                    "handler": handler.__name__,
                },
            )
            return

        chat_id = require_chat(update).id
        masked_cid = mask_chat_id(chat_id)

        logger.debug(
            "Handler entry",
            extra={
                "event": "handler_entry",
                "handler": handler.__name__,
                "chat_id_masked": masked_cid,
            },
        )

        if not authorize(chat_id):
            logger.warning(
                "Unauthorized access",
                extra={
                    "event": "unauthorized",
                    "handler": handler.__name__,
                    "chat_id_masked": masked_cid,
                },
            )
            return

        start = time.monotonic()
        try:
            return await handler(update, context, *args, **kwargs)
        finally:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            logger.debug(
                "Handler exit",
                extra={
                    "event": "handler_exit",
                    "handler": handler.__name__,
                    "chat_id_masked": masked_cid,
                    "duration_ms": elapsed_ms,
                },
            )

    # ``functools.wraps`` wraps ``wrapper`` in ``functools._Wrapped``, which
    # mypy cannot reconcile with the Concatenate/ParamSpec return type; the
    # callable shape is identical, so cast is type-only.
    return cast(
        Callable[Concatenate[Update, ContextTypes.DEFAULT_TYPE, P], Coroutine[Any, Any, None]],
        wrapper,
    )
