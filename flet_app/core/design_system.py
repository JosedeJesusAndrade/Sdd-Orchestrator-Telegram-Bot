"""Single source of truth for the Flet GUI design tokens (Slice 1).

Concept: *Gemini in form, DeepSeek in soul*. A near-black canvas slides into
deep blue and a restrained purple, with electric blue as the primary action
and lavender as the complement. All UI code imports :data:`DS` and never
hardcodes a color, size, spacing, radius or gradient.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import flet as ft


@dataclass(frozen=True, slots=True)
class Colors:
    """DeepSeek-inspired palette with a subtle lavender complement."""

    background: str = "#0A0A0A"
    # Mid/end stops sit ~20% closer to ``background`` than the Slice 1 values
    # (#0B1226 / #1A1030): the gradient stays perceptible but quieter.
    gradient_mid: str = "#0B1020"
    gradient_end: str = "#170F28"
    accent: str = "#4D6BFE"
    accent_soft: str = "#A78BFA"
    surface: str = "#16161A"
    outline: str = "#24242C"
    text_primary: str = "#F2F2F5"
    text_secondary: str = "#9A9AA5"
    text_muted: str = "#4A4A52"
    white: str = "#FFFFFF"

    @staticmethod
    def alpha(color: str, opacity: float) -> str:
        """Return ``color`` with the given opacity (0.0 - 1.0)."""
        return ft.Colors.with_opacity(opacity, color)


@dataclass(frozen=True, slots=True)
class Typography:
    """Type scale: a large light greeting, body copy and muted captions."""

    greeting_size: int = 36
    greeting_weight: ft.FontWeight = ft.FontWeight.W_300
    title_size: int = 16
    title_weight: ft.FontWeight = ft.FontWeight.W_500
    body_size: int = 14
    body_weight: ft.FontWeight = ft.FontWeight.W_400
    small_size: int = 12
    muted_size: int = 11


@dataclass(frozen=True, slots=True)
class Spacing:
    """4/8/12/16/24/32 spacing scale."""

    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32


@dataclass(frozen=True, slots=True)
class Radii:
    """Corner radii: pill composer, cards and small chips."""

    small: int = 8
    card: int = 18
    pill: int = 999


@dataclass(frozen=True, slots=True)
class Gradients:
    """Background gradient and the restrained lavender/blue glows."""

    @staticmethod
    def background() -> ft.LinearGradient:
        """Near-black at the top -> deep blue -> purple-blue at the bottom."""
        return ft.LinearGradient(
            begin=ft.Alignment.TOP_CENTER,
            end=ft.Alignment.BOTTOM_CENTER,
            colors=[
                _PALETTE.background,
                _PALETTE.gradient_mid,
                _PALETTE.gradient_end,
            ],
            stops=[0.0, 0.55, 1.0],
        )

    @staticmethod
    def lavender_glow(opacity: float = 0.22) -> ft.RadialGradient:
        """Soft light-purple halo that complements (never fights) the blue."""
        return ft.RadialGradient(
            center=ft.Alignment.CENTER,
            radius=1.0,
            colors=[
                Colors.alpha(_PALETTE.accent_soft, opacity),
                Colors.alpha(_PALETTE.accent_soft, 0.0),
            ],
        )

    @staticmethod
    def blue_glow(opacity: float = 0.16) -> ft.RadialGradient:
        """Faint electric-blue glow used near the composer."""
        return ft.RadialGradient(
            center=ft.Alignment.CENTER,
            radius=1.0,
            colors=[
                Colors.alpha(_PALETTE.accent, opacity),
                Colors.alpha(_PALETTE.accent, 0.0),
            ],
        )


_PALETTE = Colors()


@dataclass(frozen=True, slots=True)
class DesignSystem:
    """The complete token set exposed as ``DS``."""

    colors: Colors = field(default_factory=Colors)
    typography: Typography = field(default_factory=Typography)
    spacing: Spacing = field(default_factory=Spacing)
    radii: Radii = field(default_factory=Radii)
    gradients: Gradients = field(default_factory=Gradients)


DS = DesignSystem()
