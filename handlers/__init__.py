"""Handler package: shared authorization and per-chat state."""
import subprocess

from config import ALLOWED_CHAT_IDS


def authorize(chat_id: int) -> bool:
    """Return True if chat_id is in the allowed list."""
    return chat_id in ALLOWED_CHAT_IDS


# ── Shared per-chat state ──

# Track opencode sessions per chat_id
# {chat_id: {"session_name": str, "session_id": str|None, "first_message": datetime, "last_used": datetime, "prompt_count": int}}
active_sessions: dict[int, dict] = {}

# Per-chat model preference (persists across session resets via /new)
current_model: dict[int, str] = {}

# Track currently running subprocess per chat_id (for /cancel support)
current_process: dict[int, subprocess.Popen] = {}

# Track cancel requests to suppress output after kill
cancel_requests: set[int] = set()

# Track process status per chat for accurate /cancel messages
# "idle" | "running" | "cancelling"
process_status: dict[int, str] = {}


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
