"""FrontendGate — frontend-agnostic "who is allowed" seam (Fase C).

WHY THIS EXISTS (do not delete it as "unused"):
    Telegram gates every handler with ``@authorized`` in ``handlers/__init__.py``.
    That decorator does three things a second frontend (Flet) also needs:
    an allow-list check, a fresh ``new_request_id()``, and entry/exit logging.
    But ``@authorized`` is coupled to PTB's ``Update`` and stays untouched as
    the TELEGRAM implementation.

    This module is the seam where a Flet-specific gate plugs in. It is
    deliberately tiny and PTB-free so the Flet entry point can depend on it
    without importing Telegram. ``new_request_id()`` is already
    frontend-agnostic (contextvars) and is reused as-is; only the policy
    ("who is allowed") lives here. Real auth (PIN/token) is deferred; a real
    gate can be swapped in later without touching the call sites.

    ``AllowAllGate`` is the v1 policy: a local single-user app where access is
    physical access to the device.

This module imports only the standard library and ``services.chat_view``
(itself PTB-free). Importing it must NOT put ``telegram`` into ``sys.modules``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.chat_view import ConversationId


@runtime_checkable
class FrontendGate(Protocol):
    """The 'who is allowed' policy, independent of the frontend."""

    def is_allowed(self, conversation_id: ConversationId) -> bool:
        """Return True when this conversation may use the core."""
        ...


class AllowAllGate:
    """v1: access == physical access to the device (single-user local app)."""

    def is_allowed(self, conversation_id: ConversationId) -> bool:
        return True
