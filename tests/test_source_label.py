"""F7 regression: the engine prompt label must be opt-in, never injected.

The old default was ``"OpenCode"``, so any caller that forgot to pass a
label silently prefixed the prompt with ``[OpenCode] `` — a lie about the
frontend that originated it. The default is now the neutral empty string:
a forgotten label adds NO prefix, while Telegram keeps passing its explicit
``"📱 Telegram"`` marker.
"""
from __future__ import annotations

import asyncio

from services.opencode_cli_backend import OpenCodeCLIBackend


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"ok", b"")


class _CaptureExec:
    """Stand-in for asyncio.create_subprocess_exec that records argv."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *cmd_parts, **kwargs):
        self.calls.append(cmd_parts)
        return _FakeProc()


async def _labeled_prompt(monkeypatch, **kwargs) -> str:
    capture = _CaptureExec()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    backend = OpenCodeCLIBackend("opencode", workdir=".")
    result = await backend.execute(
        prompt="hello world", model="", session_id=None, **kwargs,
    )
    assert result.returncode == 0
    return capture.calls[0][-1]


async def test_default_source_label_adds_no_prefix(monkeypatch) -> None:
    assert await _labeled_prompt(monkeypatch) == "hello world"


async def test_explicit_source_label_adds_prefix(monkeypatch) -> None:
    labeled = await _labeled_prompt(monkeypatch, source_label="\U0001f4f1 Telegram")
    assert labeled == "[\U0001f4f1 Telegram] hello world"


async def test_empty_source_label_adds_no_prefix(monkeypatch) -> None:
    assert await _labeled_prompt(monkeypatch, source_label="") == "hello world"
