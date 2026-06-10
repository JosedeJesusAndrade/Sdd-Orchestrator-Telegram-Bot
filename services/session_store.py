"""SessionStore: clean, typed API wrapping persistence/sessions.py.

This is the SINGLE SOURCE OF TRUTH for session state in the bot.
It replaces:
  - 5 global dicts in handlers/__init__.py (active_sessions, current_model, ...)
  - Direct JSON read/write in 3+ handler modules
  - Ad-hoc sync logic scattered across the codebase

Architecture rationale:
  The persistence/sessions.py module is a low-level data access layer
  (load/save JSON, TTL cache, atomic writes). SessionStore is the
  SERVICE layer — it adds domain logic (validation, error types, typed
  return values via SessionInfo) while leaning on the DAL for storage.

  Why a class instead of more module-level functions?
    - Stateful: TTL cache + asyncio.Lock need an owning context
    - Testable: can be instantiated with a temp file for tests
    - Injectable: bot.py creates one instance, handlers use it
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import DEFAULT_MODEL, DEFAULT_SESSION_NAME, logger


# ── Domain objects ─────────────────────────────────────────────────────

@dataclass
class SessionInfo:
    """Immutable snapshot of a named session's state.

    Returned by all read operations — callers never touch raw dicts.
    """
    name: str
    real_id: str | None
    title: str
    created: str          # ISO 8601
    last_used: str | None # ISO 8601 or None
    prompt_count: int
    is_active: bool = False


# ── Domain errors ──────────────────────────────────────────────────────

class SessionExistsError(Exception):
    """Raised when trying to create a session that already exists."""

class SessionNotFoundError(Exception):
    """Raised when referencing a session that does not exist."""


# ── Service ────────────────────────────────────────────────────────────

class SessionStore:
    """Typed, async-safe session state management.

    Wraps a JSON file on disk with:
      - asyncio.Lock for concurrent access safety
      - TTL cache (2 s) to avoid re-reading the file on every handler call
      - Atomic writes (write to .tmp → replace) for crash safety
    """

    def __init__(self, persistence_path: Path) -> None:
        """Initialize with path to the sessions JSON file.

        Args:
            persistence_path: Path to sessions.json on disk.
        """
        self._path = persistence_path
        self._lock = asyncio.Lock()
        self._cache: dict | None = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 2.0

    # ── Internal: file I/O ─────────────────────────────────────────

    async def _load(self) -> dict:
        """Load sessions.json with TTL cache and lock protection.

        Returns a FRESH COPY — callers can mutate safely.
        """
        now = asyncio.get_event_loop().time()
        if self._cache is not None and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        async with self._lock:
            # Double-check: another task may have refreshed while waiting
            now = asyncio.get_event_loop().time()
            if self._cache is not None and (now - self._cache_time) < self._cache_ttl:
                return self._cache

            try:
                content = self._path.read_text(encoding="utf-8")
                self._cache = json.loads(content) if content.strip() else {}
            except (FileNotFoundError, json.JSONDecodeError):
                logger.debug("SessionStore: no valid sessions.json at %s, starting fresh", self._path)
                self._cache = {}

            self._cache_time = asyncio.get_event_loop().time()
            return self._cache

    async def _save(self, data: dict) -> None:
        """Atomically persist session data to disk.

        Writes to a .tmp file first, then replaces the original.
        This prevents corruption on partial writes (crash-safe).
        """
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(self._path)  # atomic on Windows & Unix
            self._cache = data
            self._cache_time = asyncio.get_event_loop().time()

    # ── Public API: session CRUD ───────────────────────────────────

    async def get_active_session(self, chat_id: int) -> SessionInfo | None:
        """Return the currently active session for a chat, or None."""
        data = await self._load()
        chat_data = data.get(str(chat_id), {})
        active_name = chat_data.get("active")
        if not active_name:
            return None
        sessions = chat_data.get("sessions", {})
        session = sessions.get(active_name)
        if not session:
            return None
        return SessionInfo(
            name=active_name,
            real_id=session.get("id"),
            title=session.get("title", active_name),
            created=session.get("created", ""),
            last_used=session.get("last_used"),
            prompt_count=session.get("prompt_count", 0),
            is_active=True,
        )

    async def create_session(self, chat_id: int, name: str) -> SessionInfo:
        """Create a new named session and set it as active.

        Raises:
            SessionExistsError: if a session with this name already exists.
        """
        data = await self._load()
        cid = str(chat_id)
        chat_data = data.setdefault(cid, {})
        sessions = chat_data.setdefault("sessions", {})

        if name in sessions:
            raise SessionExistsError(f"Session '{name}' already exists")

        now = datetime.now(timezone.utc).isoformat()
        sessions[name] = {
            "id": None,
            "title": name,
            "created": now,
            "last_used": None,
            "prompt_count": 0,
        }
        chat_data["active"] = name
        await self._save(data)

        logger.info("SessionStore: created session '%s' for chat %d", name, chat_id)
        return SessionInfo(
            name=name, real_id=None, title=name,
            created=now, last_used=None, prompt_count=0, is_active=True,
        )

    async def switch_session(self, chat_id: int, name: str) -> SessionInfo:
        """Switch the active session to another existing one.

        Raises:
            SessionNotFoundError: if the named session does not exist.
        """
        data = await self._load()
        cid = str(chat_id)
        sessions = data.get(cid, {}).get("sessions", {})

        if name not in sessions:
            raise SessionNotFoundError(f"Session '{name}' not found")

        data.setdefault(cid, {})["active"] = name
        await self._save(data)

        s = sessions[name]
        logger.info("SessionStore: switched to '%s' for chat %d", name, chat_id)
        return SessionInfo(
            name=name, real_id=s.get("id"), title=s.get("title", name),
            created=s.get("created", ""), last_used=s.get("last_used"),
            prompt_count=s.get("prompt_count", 0), is_active=True,
        )

    async def delete_session(self, chat_id: int, name: str) -> str | None:
        """Delete a session. Returns the real OpenCode session ID if it had one.

        If the deleted session was active, auto-switches to the next available.
        If no sessions remain, creates a fresh 'default'.

        Raises:
            SessionNotFoundError: if the session does not exist.
        """
        data = await self._load()
        cid = str(chat_id)
        sessions = data.get(cid, {}).get("sessions", {})

        if name not in sessions:
            raise SessionNotFoundError(f"Session '{name}' not found")

        real_id = sessions[name].get("id")
        del sessions[name]

        # Auto-switch if the deleted session was active
        if data.get(cid, {}).get("active") == name:
            remaining = list(sessions.keys())
            if remaining:
                data[cid]["active"] = remaining[0]
            else:
                data[cid]["active"] = DEFAULT_SESSION_NAME
                now = datetime.now(timezone.utc).isoformat()
                data.setdefault(cid, {}).setdefault("sessions", {})[DEFAULT_SESSION_NAME] = {
                    "id": None,
                    "title": DEFAULT_SESSION_NAME,
                    "created": now,
                    "last_used": None,
                    "prompt_count": 0,
                }

        await self._save(data)
        logger.info("SessionStore: deleted session '%s' for chat %d", name, chat_id)
        return real_id

    async def list_sessions(self, chat_id: int) -> list[SessionInfo]:
        """Return all sessions for a chat, with the active one flagged."""
        data = await self._load()
        cid = str(chat_id)
        chat_data = data.get(cid, {})
        active_name = chat_data.get("active")
        sessions = chat_data.get("sessions", {})

        result: list[SessionInfo] = []
        for name, s in sessions.items():
            result.append(SessionInfo(
                name=name,
                real_id=s.get("id"),
                title=s.get("title", name),
                created=s.get("created", ""),
                last_used=s.get("last_used"),
                prompt_count=s.get("prompt_count", 0),
                is_active=(name == active_name),
            ))
        return result

    # ── Public API: model preference ───────────────────────────────

    async def get_model(self, chat_id: int) -> str:
        """Return the model preference for a chat, falling back to DEFAULT_MODEL."""
        data = await self._load()
        return data.get(str(chat_id), {}).get("model", DEFAULT_MODEL)

    async def set_model(self, chat_id: int, model: str) -> None:
        """Persist a model preference for a chat."""
        data = await self._load()
        data.setdefault(str(chat_id), {})["model"] = model
        await self._save(data)
        logger.info("SessionStore: model set to '%s' for chat %d", model, chat_id)

    # ── Public API: prompt tracking ────────────────────────────────

    async def increment_prompt_count(self, chat_id: int) -> int:
        """Increment prompt count for the active session. Returns new count."""
        data = await self._load()
        cid = str(chat_id)
        active_name = data.get(cid, {}).get("active", DEFAULT_SESSION_NAME)
        session = (
            data.setdefault(cid, {})
            .setdefault("sessions", {})
            .setdefault(active_name, {})
        )
        session["prompt_count"] = session.get("prompt_count", 0) + 1
        session["last_used"] = datetime.now(timezone.utc).isoformat()
        await self._save(data)
        return session["prompt_count"]

    async def update_session_id(self, chat_id: int, real_id: str) -> None:
        """Update the real OpenCode session ID for the active session."""
        data = await self._load()
        cid = str(chat_id)
        active_name = data.get(cid, {}).get("active", DEFAULT_SESSION_NAME)
        sessions = data.setdefault(cid, {}).setdefault("sessions", {})
        if active_name in sessions:
            sessions[active_name]["id"] = real_id
        await self._save(data)

    async def update_session_title(self, chat_id: int, name: str, title: str) -> None:
        """Update the title of a named session."""
        data = await self._load()
        cid = str(chat_id)
        sessions = data.get(cid, {}).get("sessions", {})
        if name in sessions:
            sessions[name]["title"] = title
        await self._save(data)

    async def reset_session(self, chat_id: int) -> None:
        """Reset the active session's OpenCode ID and prompt count.

        Used by /new — clears the real session ID so the next prompt
        starts fresh, but preserves the session name and model.
        """
        data = await self._load()
        cid = str(chat_id)
        chat_data = data.get(cid, {})
        active_name = chat_data.get("active", DEFAULT_SESSION_NAME)
        sessions = chat_data.get("sessions", {})
        if active_name in sessions:
            sessions[active_name]["id"] = None
            sessions[active_name]["prompt_count"] = 0
            await self._save(data)
            logger.info("SessionStore: reset session '%s' for chat %d", active_name, chat_id)
