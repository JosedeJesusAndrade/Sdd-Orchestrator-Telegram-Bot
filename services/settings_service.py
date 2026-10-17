"""SettingsService: per-chat configuration, frontend-agnostic (Fase B).

Moves the ``/config`` business logic out of ``handlers/commands.py`` (B8):
defaults, key validation, ``timeout`` integer validation and model-alias
resolution. The handler keeps only argument parsing and message formatting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from config import DEFAULT_MODEL, OPENCODE_TIMEOUT, OPENCODE_WORKDIR, resolve_model
from services.session_store import SessionStore

if TYPE_CHECKING:
    from services.chat_view import ConversationId

SETTINGS_KEYS: tuple[str, ...] = ("model", "timeout", "workdir", "provider")

# 24 hours. ``asyncio.wait_for`` treats a timeout <= 0 as already expired
# (every prompt fails instantly), so the lower bound must be strict. The upper
# bound is a sanity cap: an interactive chat prompt should never legitimately
# need to block for more than a day, and it rejects typos like ``999999``.
_MAX_TIMEOUT_SECONDS = 86400

_DEFAULTS: dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "timeout": OPENCODE_TIMEOUT,
    "workdir": OPENCODE_WORKDIR,
    "provider": "opencode",
}


class UnknownSettingError(KeyError):
    """Raised when a config key is not part of the supported set."""


class InvalidSettingValueError(ValueError):
    """Raised when a setting value fails validation (e.g. non-numeric timeout)."""


@dataclass(frozen=True)
class ChatSettings:
    """Fully resolved per-chat configuration (defaults applied)."""

    model: str
    timeout: int | str
    workdir: str
    provider: str


class SettingsService:
    """Read, validate and persist per-chat settings."""

    def __init__(self, session_store: SessionStore) -> None:
        self._store = session_store

    async def get(self, conversation_id: ConversationId) -> ChatSettings:
        """Return the effective settings with defaults applied."""
        raw = await self._store.get_all_chat_settings(conversation_id)
        return ChatSettings(
            model=raw.get("model", DEFAULT_MODEL),
            timeout=raw.get("timeout", OPENCODE_TIMEOUT),
            workdir=raw.get("workdir", OPENCODE_WORKDIR),
            provider=raw.get("provider", "opencode"),
        )

    async def get_value(self, conversation_id: ConversationId, key: str) -> Any:
        """Return one setting with its default applied.

        Raises:
            UnknownSettingError: if ``key`` is not supported.
        """
        self._ensure_known(key)
        return await self._store.get_chat_setting(conversation_id, key, _DEFAULTS[key])

    async def set(self, conversation_id: ConversationId, key: str, raw_value: str) -> str | int:
        """Validate and persist one setting, returning the stored value.

        ``model`` values go through alias resolution; ``timeout`` must be an
        integer number of seconds in ``(0, 86400]``.

        Raises:
            UnknownSettingError: if ``key`` is not supported.
            InvalidSettingValueError: if the value is not valid for ``key``.
        """
        self._ensure_known(key)

        value: str | int
        if key == "model":
            value = resolve_model(raw_value)
        elif key == "timeout":
            try:
                value = int(raw_value)
            except ValueError as e:
                raise InvalidSettingValueError(raw_value) from e
            if value <= 0:
                raise InvalidSettingValueError("El timeout debe ser mayor que 0 segundos.")
            if value > _MAX_TIMEOUT_SECONDS:
                raise InvalidSettingValueError(
                    f"El timeout no puede superar los {_MAX_TIMEOUT_SECONDS} segundos (24 horas)."
                )
        else:
            value = raw_value

        await self._store.set_chat_setting(conversation_id, key, value)
        return value

    @staticmethod
    def _ensure_known(key: str) -> None:
        if key not in SETTINGS_KEYS:
            raise UnknownSettingError(key)
