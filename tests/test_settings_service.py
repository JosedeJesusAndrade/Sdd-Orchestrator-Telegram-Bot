"""Unit tests for SettingsService (Fase B).

Covers defaults, persistence, key/value validation (including timeout),
``get_value`` and model-alias resolution. Uses a recording fake for the
unit contract and a real ``SessionStore`` on ``tmp_path`` for the
round-trip persistence check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from config import DEFAULT_MODEL, OPENCODE_TIMEOUT, OPENCODE_WORKDIR
from services.session_store import SessionStore
from services.settings_service import (
    ChatSettings,
    InvalidSettingValueError,
    SettingsService,
    UnknownSettingError,
)


class FakeStore:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = dict(settings or {})
        self.set_calls: list[tuple[object, str, object]] = []

    async def get_all_chat_settings(self, conversation_id: object) -> dict[str, Any]:
        return dict(self.settings)

    async def get_chat_setting(self, conversation_id: object, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    async def set_chat_setting(self, conversation_id: object, key: str, value: object) -> None:
        self.settings[key] = value
        self.set_calls.append((conversation_id, key, value))


def _service(store: FakeStore | SessionStore) -> SettingsService:
    return SettingsService(cast(SessionStore, store))


# ── Defaults ───────────────────────────────────────────────────────────


async def test_get_returns_defaults_when_store_is_empty() -> None:
    service = _service(FakeStore())

    settings = await service.get(42)

    assert settings == ChatSettings(
        model=DEFAULT_MODEL,
        timeout=OPENCODE_TIMEOUT,
        workdir=OPENCODE_WORKDIR,
        provider="opencode",
    )


async def test_get_applies_defaults_only_where_values_are_missing() -> None:
    service = _service(FakeStore({"model": "custom/model", "timeout": 45}))

    settings = await service.get(42)

    assert settings.model == "custom/model"
    assert settings.timeout == 45
    assert settings.workdir == OPENCODE_WORKDIR
    assert settings.provider == "opencode"


# ── set(): persistence and validation ──────────────────────────────────


async def test_set_persists_and_get_reflects_it() -> None:
    store = FakeStore()
    service = _service(store)

    value = await service.set(42, "workdir", "C:/project")

    assert value == "C:/project"
    assert store.set_calls == [(42, "workdir", "C:/project")]
    assert (await service.get(42)).workdir == "C:/project"


async def test_set_timeout_coerces_to_int_and_persists() -> None:
    store = FakeStore()
    service = _service(store)

    value = await service.set(42, "timeout", "120")

    assert value == 120
    assert isinstance(value, int)
    assert store.settings["timeout"] == 120


@pytest.mark.parametrize(
    ("raw", "resolved"),
    [
        ("pro", "deepseek/deepseek-v4-pro"),
        ("flash", "deepseek/deepseek-flash"),
        ("custom/model", "custom/model"),
    ],
)
async def test_set_model_resolves_aliases(raw: str, resolved: str) -> None:
    store = FakeStore()
    service = _service(store)

    value = await service.set(42, "model", raw)

    assert value == resolved
    assert store.settings["model"] == resolved
    assert (await service.get(42)).model == resolved


@pytest.mark.parametrize("raw", ["abc", "12.5", "", "10s"])
async def test_set_timeout_non_numeric_raises_and_does_not_persist(raw: str) -> None:
    store = FakeStore()
    service = _service(store)

    with pytest.raises(InvalidSettingValueError) as exc:
        await service.set(42, "timeout", raw)

    assert exc.value.args == (raw,)
    assert store.set_calls == []
    assert store.settings == {}


@pytest.mark.parametrize("raw", ["-5", "0"])
async def test_set_timeout_non_positive_raises_and_does_not_persist(raw: str) -> None:
    store = FakeStore()
    service = _service(store)

    with pytest.raises(InvalidSettingValueError) as exc:
        await service.set(42, "timeout", raw)

    assert exc.value.args == ("El timeout debe ser mayor que 0 segundos.",)
    assert store.set_calls == []
    assert store.settings == {}


async def test_set_timeout_above_upper_bound_raises_and_does_not_persist() -> None:
    store = FakeStore()
    service = _service(store)

    with pytest.raises(InvalidSettingValueError) as exc:
        await service.set(42, "timeout", "999999")

    assert exc.value.args == ("El timeout no puede superar los 86400 segundos (24 horas).",)
    assert store.set_calls == []
    assert store.settings == {}


async def test_set_timeout_accepts_valid_values_at_bounds() -> None:
    store = FakeStore()
    service = _service(store)

    assert await service.set(42, "timeout", "300") == 300
    assert await service.set(42, "timeout", "86400") == 86400
    assert store.settings["timeout"] == 86400


async def test_set_unknown_key_raises_and_does_not_write() -> None:
    store = FakeStore()
    service = _service(store)

    with pytest.raises(UnknownSettingError) as exc:
        await service.set(42, "theme", "dark")

    assert exc.value.args == ("theme",)
    assert store.set_calls == []


# ── get_value() ────────────────────────────────────────────────────────


async def test_get_value_returns_default_for_unset_key() -> None:
    service = _service(FakeStore())

    assert await service.get_value(42, "timeout") == OPENCODE_TIMEOUT
    assert await service.get_value(42, "model") == DEFAULT_MODEL


async def test_get_value_returns_stored_value() -> None:
    service = _service(FakeStore({"timeout": 45}))

    assert await service.get_value(42, "timeout") == 45


async def test_get_value_unknown_key_raises() -> None:
    service = _service(FakeStore())

    with pytest.raises(UnknownSettingError) as exc:
        await service.get_value(42, "theme")

    assert exc.value.args == ("theme",)


# ── Persistence round trip through the real store ──────────────────────


async def test_settings_round_trip_through_real_session_store(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    service = _service(store)

    await service.set("flet-user", "workdir", "C:/project")

    settings = await service.get("flet-user")
    assert settings.workdir == "C:/project"

    persisted = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert persisted["flet-user"]["settings"]["workdir"] == "C:/project"
