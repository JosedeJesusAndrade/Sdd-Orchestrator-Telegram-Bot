"""F10 regression: ``OpenCodeCLIBackend.execute`` honors the passed timeout.

Before the fix the per-chat ``timeout`` setting was computed by
``PromptService`` and discarded; the backend always used its constructor
timeout. ``execute`` now accepts a per-call ``timeout`` and uses it for the
``asyncio.wait_for`` deadline AND for the timeout event/error text, so the
effective timeout is truthful.
"""
from __future__ import annotations

import asyncio

from services.opencode_cli_backend import OpenCodeCLIBackend


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.pid = 999999

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"ok", b"")


async def _run(monkeypatch, *, ctor_timeout: int, call_timeout: int | None):
    seen: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    async def spy_wait_for(awaitable, timeout):
        seen["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

    backend = OpenCodeCLIBackend("opencode", workdir=".", timeout=ctor_timeout)
    await backend.execute(
        prompt="x", model="m", session_id=None, timeout=call_timeout,
    )
    return seen["timeout"]


async def test_execute_uses_passed_timeout(monkeypatch) -> None:
    assert await _run(monkeypatch, ctor_timeout=300, call_timeout=7) == 7


async def test_execute_falls_back_to_ctor_timeout(monkeypatch) -> None:
    assert await _run(monkeypatch, ctor_timeout=300, call_timeout=None) == 300


async def test_timeout_result_reports_effective_timeout(monkeypatch) -> None:
    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    async def timeout_wait_for(awaitable, timeout):
        awaitable.close()  # avoid "coroutine never awaited" on the error path
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", timeout_wait_for)

    backend = OpenCodeCLIBackend("opencode", workdir=".", timeout=300)
    backend.cancel = lambda: None  # type: ignore[method-assign]

    result = await backend.execute(
        prompt="x", model="m", session_id=None, timeout=5,
    )

    assert result.timed_out is True
    assert "5s" in result.stderr
