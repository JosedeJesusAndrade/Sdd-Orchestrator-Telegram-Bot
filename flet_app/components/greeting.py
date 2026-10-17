"""Centered greeting molecule with the reserved personalization field.

``USER_NAME`` is deliberately inert in Slice 1: it only changes the greeting
copy. The personalization flow (setup, persistence, UI) lands in a later
slice; until then the generic fallback is always shown.
"""

from __future__ import annotations

import flet as ft

from flet_app.core.design_system import DS

USER_NAME: str | None = None

_GREETING_FALLBACK = "¿Qué vamos a construir hoy?"


def greeting_text() -> str:
    """Return the personalized or generic greeting."""
    if USER_NAME:
        return f"¡Es tu turno, {USER_NAME}!"
    return _GREETING_FALLBACK


def build_greeting() -> ft.Container:
    """Large light greeting, centered cleanly in the free space.

    The logo mark that stood above the copy was removed per design feedback:
    the greeting now carries the empty state on its own, with the page-level
    lavender glow providing the only purple accent.
    """
    text = ft.Text(
        value=greeting_text(),
        size=DS.typography.greeting_size,
        weight=DS.typography.greeting_weight,
        color=DS.colors.text_primary,
        text_align=ft.TextAlign.CENTER,
    )

    return ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Container(
            content=text,
            padding=ft.Padding.symmetric(horizontal=DS.spacing.xl),
        ),
    )
