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
