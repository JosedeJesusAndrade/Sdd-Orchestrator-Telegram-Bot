"""Tests for the ScreenHost content-swap navigation seam (no real client)."""

from __future__ import annotations

from typing import cast

import flet as ft

from flet_app.core.navigation import ScreenHost


class FakePage:
    """Minimal stand-in for ``ft.Page``: records update() calls only."""

    def __init__(self) -> None:
        self.update_calls = 0

    def update(self, *controls: ft.Control) -> None:
        self.update_calls += 1


async def test_screen_host_swaps_content_and_updates_the_page() -> None:
    page = FakePage()
    first = ft.Text("chat")
    second = ft.Text("conversaciones")

    host = ScreenHost(cast(ft.Page, page), first)

    assert host.control.content is first
    assert host.control.expand is True

    await host.show(second)

    assert host.control.content is second
    assert page.update_calls == 1
