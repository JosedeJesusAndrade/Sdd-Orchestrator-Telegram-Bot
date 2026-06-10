"""Handler package: shared authorization decorator.

Session state has been moved to services/session_store.py (SessionStore).
Process tracking has been moved to services/prompt_service.py (PromptService).
This module now ONLY contains the @authorized decorator.
"""

from config import ALLOWED_CHAT_IDS


def authorize(chat_id: int) -> bool:
    """Return True if chat_id is in the allowed list."""
    return chat_id in ALLOWED_CHAT_IDS


import functools
from typing import Callable, Awaitable

from config import logger
from utils.logging import mask_chat_id


def authorized(
    handler: Callable[..., Awaitable[None]]
) -> Callable[..., Awaitable[None]]:
    """Decorator: only allow authorized chat_ids to execute the handler.

    Extracts chat_id from the ``update`` parameter automatically.
    """
    @functools.wraps(handler)
    async def wrapper(*args, **kwargs) -> None:
        update = args[0] if args else kwargs.get("update")
        if update is None:
            logger.error("@authorized could not find Update in %s", handler.__name__)
            return
        chat_id = update.effective_chat.id
        if not authorize(chat_id):
            logger.warning("Unauthorized %s from %s", handler.__name__, mask_chat_id(chat_id))
            return
        return await handler(*args, **kwargs)
    return wrapper
