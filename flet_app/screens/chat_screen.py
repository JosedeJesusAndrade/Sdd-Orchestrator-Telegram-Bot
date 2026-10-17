"""Chat screen: assembles the top bar, greeting, conversation and composer.

Composition follows Gemini: chrome at the top, a centered greeting in the
empty state, a scrolling conversation once messages exist, and a floating
pill composer pinned to the bottom with the workdir muted underneath.
"""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

from flet_app.components.composer import Composer, build_composer
from flet_app.components.greeting import build_greeting
from flet_app.components.top_bar import TopBar, build_top_bar
from flet_app.core.design_system import DS


@dataclass
class ChatScreen:
    """The assembled screen plus handles the entry point needs to drive it."""

    control: ft.Container
    top_bar: TopBar
    composer: Composer
    conversation: ft.Column
    greeting: ft.Container
    conversation_holder: ft.Container

    def set_model(self, model: str) -> None:
        self.top_bar.set_model(model)

    def reveal_conversation(self) -> None:
        """Hide the greeting overlay on the first message.

        The conversation column is mounted VISIBLE from the very first frame
        (see ``build_chat_screen``); only the greeting overlay is hidden here.
        Toggling the conversation from ``visible=False`` to ``True`` in the
        same patch that appended the first message left some clients with a
        subtree that never rendered. Keeping it always visible removes that
        failure mode entirely.
        """
        self.greeting.visible = False


def build_chat_screen(*, model: str, workdir: str) -> ChatScreen:
    """Build the Slice 1 chat screen."""
    top_bar = build_top_bar(model)
    greeting = build_greeting()
    composer = build_composer(workdir=workdir)

    conversation = ft.Column(
        controls=[],
        spacing=DS.spacing.md,
        scroll=ft.ScrollMode.AUTO,
        auto_scroll=True,
        expand=True,
    )
    conversation_holder = ft.Container(
        content=conversation,
        expand=True,
        padding=ft.Padding.only(
            left=DS.spacing.xl,
            right=DS.spacing.xl,
            top=DS.spacing.sm,
        ),
    )

    # The conversation is NOT hidden at mount: a control tree that is created
    # with ``visible=False`` and revealed later (in the same patch that adds
    # its first child) can fail to build that child on the client. Mount the
    # scrollable column visible from frame one and let the greeting sit on top
    # of it; the first message only hides the greeting.
    conversation_area = ft.Stack(
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[conversation_holder, greeting],
    )

    composer_holder = ft.Container(
        content=ft.ResponsiveRow(
            controls=[
                ft.Container(
                    content=composer.control,
                    col={"sm": 12, "md": 10, "lg": 8},
                )
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        padding=ft.Padding.only(
            left=DS.spacing.xl,
            right=DS.spacing.xl,
            top=DS.spacing.sm,
            bottom=DS.spacing.md,
        ),
    )

    body = ft.Column(
        controls=[top_bar.control, conversation_area, composer_holder],
        expand=True,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    control = ft.Container(
        expand=True,
        gradient=DS.gradients.background(),
        content=ft.Stack(
            expand=True,
            fit=ft.StackFit.EXPAND,
            controls=[
                ft.Container(
                    width=520,
                    height=520,
                    top=-200,
                    right=-180,
                    border_radius=260,
                    gradient=DS.gradients.lavender_glow(),
                    ignore_interactions=True,
                ),
                ft.Container(
                    width=420,
                    height=420,
                    bottom=-220,
                    left=-140,
                    border_radius=210,
                    gradient=DS.gradients.blue_glow(),
                    ignore_interactions=True,
                ),
                # ``SafeArea`` keeps the chrome below the notch/status bar on a
                # real APK. The Flet viewer reports no intrusions, so the top
                # bar also carries a small explicit top padding (top_bar.py).
                ft.SafeArea(
                    content=body,
                    expand=True,
                    avoid_intrusions_top=True,
                    avoid_intrusions_bottom=True,
                    avoid_intrusions_left=False,
                    avoid_intrusions_right=False,
                ),
            ],
        ),
    )

    return ChatScreen(
        control=control,
        top_bar=top_bar,
        composer=composer,
        conversation=conversation,
        greeting=greeting,
        conversation_holder=conversation_holder,
    )
