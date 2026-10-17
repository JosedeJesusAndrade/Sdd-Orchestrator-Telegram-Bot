"""Fase C: tests for the FrontendGate seam (``services/frontend_gate.py``).

Covers:
  - ``AllowAllGate`` allows every conversation id shape (int, str, empty str).
  - ``AllowAllGate`` structurally satisfies the ``FrontendGate`` Protocol —
    exercised through a function annotated with the Protocol, so mypy proves
    the conformance and the test runs the same call path.
  - The module is PTB-free (subprocess check).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from services.chat_view import ConversationId
from services.frontend_gate import AllowAllGate, FrontendGate

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── v1 policy ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("conversation_id", [123, "flet", ""])
def test_allow_all_gate_allows_any_conversation_id(conversation_id: ConversationId) -> None:
    assert AllowAllGate().is_allowed(conversation_id) is True


# ── Protocol conformance ──────────────────────────────────────────────


def _accepts_gate(gate: FrontendGate, conversation_id: ConversationId) -> bool:
    """Typing-level conformance: mypy requires the Protocol shape here."""
    return gate.is_allowed(conversation_id)


def test_allow_all_gate_satisfies_frontend_gate_protocol() -> None:
    gate = AllowAllGate()

    assert _accepts_gate(gate, "flet") is True
    assert isinstance(gate, FrontendGate)


# ── PTB-free guarantee ────────────────────────────────────────────────


def test_frontend_gate_module_is_ptb_free() -> None:
    """Importing the gate in a clean interpreter must not pull in ``telegram``.

    Uses a subprocess because this test session already imports PTB (other
    test modules), which would otherwise pollute ``sys.modules``.
    """
    script = (
        "import services.frontend_gate, sys; "
        "print('PTB_FREE=' + str('telegram' not in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "PTB_FREE=True" in result.stdout
