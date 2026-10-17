"""Unit tests for StatusService (Fase B).

Covers the observable snapshot contract: an active vs. no session, uptime,
the timestamp fallback paths preserved byte-for-byte from the old handler,
and the git-branch lookup (success and both failure paths).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from services.session_store import SessionInfo, SessionStore
from services.status_service import StatusService, StatusSnapshot

_CREATED = "2026-05-19T00:00:00+00:00"
_LAST_USED = "2026-05-19T01:00:00+00:00"


class FakeStore:
    def __init__(
        self,
        session: SessionInfo | None = None,
        *,
        model: str = "test/model",
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._model = model
        self._settings = settings or {}

    async def get_active_session(self, conversation_id: object) -> SessionInfo | None:
        return self._session

    async def get_model(self, conversation_id: object) -> str:
        return self._model

    async def get_all_chat_settings(self, conversation_id: object) -> dict[str, Any]:
        return dict(self._settings)


def _session(
    *,
    name: str = "work",
    real_id: str | None = "ses_123",
    created: str = _CREATED,
    last_used: str | None = _LAST_USED,
    prompt_count: int = 7,
) -> SessionInfo:
    return SessionInfo(
        name=name,
        real_id=real_id,
        title=name,
        created=created,
        last_used=last_used,
        prompt_count=prompt_count,
        is_active=True,
    )


async def _snapshot(
    session: SessionInfo | None,
    *,
    settings: dict[str, Any] | None = None,
    start_time: float | None = None,
) -> StatusSnapshot:
    service = StatusService(
        cast(SessionStore, FakeStore(session, model="test/model", settings=settings or {})),
        start_time=start_time,
    )
    return await service.snapshot(42)


# ── Active session / no session ────────────────────────────────────────


async def test_snapshot_with_active_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.status_service.time.time", lambda: 1000.0)
    invocations: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        invocations.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="main\n")

    monkeypatch.setattr("services.status_service.subprocess.run", fake_run)

    service = StatusService(
        cast(
            SessionStore,
            FakeStore(
                _session(),
                model="deepseek/deepseek-v4-pro",
                settings={"workdir": "C:/repo"},
            ),
        ),
        start_time=940.0,
    )

    snapshot = await service.snapshot(42)

    assert isinstance(snapshot, StatusSnapshot)
    assert snapshot.session_name == "work"
    assert snapshot.session_real_id == "ses_123"
    assert snapshot.model == "deepseek/deepseek-v4-pro"
    assert snapshot.branch == "main"
    assert snapshot.created == datetime.fromisoformat(_CREATED)
    assert snapshot.last_used == datetime.fromisoformat(_LAST_USED)
    assert snapshot.created_fallback is None
    assert snapshot.last_used_fallback is None
    assert snapshot.prompt_count == 7
    assert snapshot.uptime_seconds == 60
    assert invocations == [
        (
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            {"capture_output": True, "text": True, "cwd": "C:/repo", "timeout": 5},
        )
    ]


async def test_snapshot_without_active_session_uses_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.status_service.time.time", lambda: 1000.0)

    snapshot = await _snapshot(None, settings={}, start_time=None)

    assert snapshot.session_name is None
    assert snapshot.session_real_id is None
    assert snapshot.model == "test/model"
    assert snapshot.branch == "?"
    assert snapshot.created is None
    assert snapshot.last_used is None
    assert snapshot.created_fallback is None
    assert snapshot.last_used_fallback is None
    assert snapshot.prompt_count == 0
    assert snapshot.uptime_seconds is None


# ── Timestamp fallbacks (byte-for-byte old-handler behavior) ───────────


async def test_malformed_timestamps_fall_back_to_raw_strings() -> None:
    snapshot = await _snapshot(_session(created="not-a-date", last_used="also-bad"))

    assert snapshot.created is None
    assert snapshot.last_used is None
    assert snapshot.created_fallback == "not-a-date"
    assert snapshot.last_used_fallback == "also-bad"


async def test_naive_timestamps_fall_back_to_raw_strings() -> None:
    snapshot = await _snapshot(
        _session(
            created="2026-05-19T00:00:00",
            last_used="2026-05-19T01:00:00",
        )
    )

    assert snapshot.created is None
    assert snapshot.last_used is None
    assert snapshot.created_fallback == "2026-05-19T00:00:00"
    assert snapshot.last_used_fallback == "2026-05-19T01:00:00"


async def test_aware_created_with_malformed_last_used_keeps_paths_separate() -> None:
    snapshot = await _snapshot(_session(created=_CREATED, last_used="garbage"))

    assert snapshot.created == datetime.fromisoformat(_CREATED)
    assert snapshot.created_fallback is None
    assert snapshot.last_used is None
    assert snapshot.last_used_fallback == "garbage"


async def test_empty_and_missing_timestamps_have_no_fallback() -> None:
    snapshot = await _snapshot(_session(created="", last_used=None))

    assert snapshot.created is None
    assert snapshot.last_used is None
    assert snapshot.created_fallback is None
    assert snapshot.last_used_fallback is None


# ── Git branch lookup ──────────────────────────────────────────────────


async def test_branch_falls_back_when_git_returns_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.status_service.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    snapshot = await _snapshot(_session(), settings={"workdir": "C:/repo"})

    assert snapshot.branch == "?"


async def test_branch_falls_back_when_git_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("git not found")

    monkeypatch.setattr("services.status_service.subprocess.run", explode)

    snapshot = await _snapshot(_session(), settings={"workdir": "C:/repo"})

    assert snapshot.branch == "?"


async def test_branch_is_unknown_without_workdir_and_skips_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[bool] = []

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        invocations.append(True)
        return SimpleNamespace(returncode=0, stdout="main\n")

    monkeypatch.setattr("services.status_service.subprocess.run", fake_run)

    snapshot = await _snapshot(_session(), settings={})

    assert snapshot.branch == "?"
    assert invocations == []
