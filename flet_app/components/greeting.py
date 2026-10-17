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
    """Logo mark + large light greeting, centered in the free space."""
    mark = ft.Container(
        width=64,
        height=64,
        border_radius=20,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_LEFT,
            end=ft.Alignment.BOTTOM_RIGHT,
            colors=[DS.colors.accent_soft, DS.colors.accent],
        ),
        alignment=ft.Alignment.CENTER,
        shadow=ft.BoxShadow(
            blur_radius=32,
            color=DS.colors.alpha(DS.colors.accent_soft, 0.35),
        ),
        content=ft.Icon(ft.Icons.AUTO_AWESOME, size=30, color=DS.colors.white),
    )

    halo = ft.Container(
        width=220,
        height=220,
        border_radius=110,
        gradient=DS.gradients.lavender_glow(),
        alignment=ft.Alignment.CENTER,
        content=mark,
    )

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
        content=ft.Column(
            controls=[
                halo,
                ft.Container(
                    content=text,
                    padding=ft.Padding.symmetric(horizontal=DS.spacing.xl),
                ),
            ],
            spacing=DS.spacing.lg,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )
