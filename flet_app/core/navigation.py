"""ScreenHost: the minimal navigation seam between full-screen controls.

This is deliberately NOT a router: it owns one expanding container and swaps
its content. Screens only know about ``show``; a real router can replace this
class later without touching any screen. Async-friendly because Flet's event
loop is asyncio-based.
"""

from __future__ import annotations

import flet as ft


class ScreenHost:
    """Swap the visible screen control inside one host container."""

    def __init__(self, page: ft.Page, initial_control: ft.Control) -> None:
        self._page = page
        self.control = ft.Container(content=initial_control, expand=True)

    async def show(self, control: ft.Control) -> None:
        """Replace the visible screen and flush the page."""
        self.control.content = control
        self._page.update()
