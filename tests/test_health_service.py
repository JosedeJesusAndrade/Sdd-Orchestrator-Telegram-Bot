"""Unit tests for HealthService (Fase B).

Covers the observable snapshot contract: connectivity probe both ways
(and the Flet "no probe" case), version parsing via ``BASE_DIR``, OC
session count with its error fallback, uptime and python fields.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest

from services.health_service import HealthService
from services.session_store import SessionStore

_VERSION_FILE = '[project]\nname = "x"\nversion = "1.2.3"\n'


class FakeStore:
    def __init__(self, settings: dict[str, Any] | None = None, model: str = "test/model") -> None:
        self._settings = settings or {}
        self._model = model

    async def get_all_chat_settings(self, conversation_id: object) -> dict[str, Any]:
        return dict(self._settings)

    async def get_model(self, conversation_id: object) -> str:
        return self._model


class FakeProbe:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response if response is not None else {"username": "my_bot"}
        self._error = error
        self.calls = 0

    async def get_me(self) -> dict[str, Any]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _service(store: FakeStore | None = None, *, start_time: float = 900.0) -> HealthService:
    return HealthService(cast(SessionStore, store or FakeStore()), start_time)


@pytest.fixture(autouse=True)
def _fixed_clock_and_no_oc_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.health_service.time.time", lambda: 1000.0)

    async def no_sessions() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("services.health_service.fetch_opencode_sessions", no_sessions)


def _write_version(tmp_path: Path, content: str) -> None:
    (tmp_path / "pyproject.toml").write_text(content, encoding="utf-8")


# ── Connectivity probe ─────────────────────────────────────────────────


async def test_snapshot_without_probe_reports_unknown_connectivity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.health_service.BASE_DIR", tmp_path)
    _write_version(tmp_path, _VERSION_FILE)

    snapshot = await _service().snapshot(42)

    assert snapshot.connected is None
    assert snapshot.bot_user == "?"
    assert snapshot.version == "1.2.3"
    assert snapshot.uptime_seconds == 100
    assert snapshot.python == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert snapshot.oc_session_count == 0
    assert snapshot.workdir == "default"
    assert snapshot.model == "test/model"


async def test_probe_success_reports_connected_and_bot_user() -> None:
    probe = FakeProbe(response={"username": "my_bot"})

    snapshot = await _service().snapshot(42, connectivity_probe=probe)

    assert snapshot.connected is True
    assert snapshot.bot_user == "my_bot"
    assert probe.calls == 1


async def test_probe_without_username_leaves_bot_user_unknown() -> None:
    probe = FakeProbe(response={})

    snapshot = await _service().snapshot(42, connectivity_probe=probe)

    assert snapshot.connected is True
    assert snapshot.bot_user == "?"


async def test_probe_failure_reports_disconnected_and_unknown_user() -> None:
    probe = FakeProbe(error=RuntimeError("network down"))

    snapshot = await _service().snapshot(42, connectivity_probe=probe)

    assert snapshot.connected is False
    assert snapshot.bot_user == "?"
    assert probe.calls == 1


# ── Version read (BASE_DIR/pyproject.toml) ─────────────────────────────


async def test_version_is_read_from_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.health_service.BASE_DIR", tmp_path)
    _write_version(tmp_path, _VERSION_FILE)

    snapshot = await _service().snapshot(1)

    assert snapshot.version == "1.2.3"


async def test_version_falls_back_when_pyproject_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.health_service.BASE_DIR", tmp_path)

    snapshot = await _service().snapshot(1)

    assert snapshot.version == "?"


async def test_version_falls_back_when_pyproject_has_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.health_service.BASE_DIR", tmp_path)
    _write_version(tmp_path, '[project]\nname = "x"\n')

    snapshot = await _service().snapshot(1)

    assert snapshot.version == "?"


# ── OpenCode session count ─────────────────────────────────────────────


async def test_oc_session_count_reflects_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> list[dict[str, Any]]:
        return [{"id": "ses_1"}, {"id": "ses_2"}]

    monkeypatch.setattr("services.health_service.fetch_opencode_sessions", fake_fetch)

    snapshot = await _service().snapshot(1)

    assert snapshot.oc_session_count == 2


async def test_oc_session_count_falls_back_to_none_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch() -> list[dict[str, Any]]:
        raise RuntimeError("opencode missing")

    monkeypatch.setattr("services.health_service.fetch_opencode_sessions", fake_fetch)

    snapshot = await _service().snapshot(1)

    assert snapshot.oc_session_count is None


# ── Per-chat settings ──────────────────────────────────────────────────


async def test_workdir_and_model_are_read_from_the_store() -> None:
    store = FakeStore(settings={"workdir": "C:/project"}, model="deepseek/deepseek-flash")

    snapshot = await _service(store).snapshot(1)

    assert snapshot.workdir == "C:/project"
    assert snapshot.model == "deepseek/deepseek-flash"
