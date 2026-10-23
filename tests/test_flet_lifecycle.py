"""Flet startup lifecycle (finding #6): eager warm-up, never stop.

D5: the OpenCode server lives always. The GUI warms it up eagerly (adopt or
spawn) and, on failure, shows a clear notice without crashing; closing the GUI
never stops a process the bot may be sharing.
"""

from __future__ import annotations

from flet_app.main import WARM_UP_FAILED_MESSAGE, warm_up_gui
from services.opencode_server.errors import OpenCodeUnavailableError


class _FakeManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.directories: list[object] = []
        self.stop_calls = 0

    async def ensure_running(self, *, directory: object = None) -> None:
        self.directories.append(directory)
        if self.fail:
            raise OpenCodeUnavailableError("no se pudo iniciar opencode serve")

    async def stop(self) -> None:
        self.stop_calls += 1


class _FakeSend:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def test_startup_warms_the_server_and_is_silent_on_success() -> None:
    manager = _FakeManager()
    send = _FakeSend()

    ok = await warm_up_gui(send, manager, directory="C:/base")

    assert ok is True
    assert manager.directories == ["C:/base"]
    assert send.sent == []
    assert manager.stop_calls == 0


async def test_failed_warm_up_shows_a_clear_notice_and_does_not_crash() -> None:
    manager = _FakeManager(fail=True)
    send = _FakeSend()

    ok = await warm_up_gui(send, manager, directory="C:/base")

    assert ok is False
    assert send.sent == [WARM_UP_FAILED_MESSAGE]
    assert "No se pudo preparar el servidor OpenCode" in send.sent[0]
    assert manager.stop_calls == 0
