"""``.env.example`` must be complete, current, and secret-free (finding #4/#13).

The example is part of the onboarding contract: every variable the app reads
must appear (commented or active) with a safe placeholder, and no real secret
may ever be committed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

#: Every variable read by the app (config.py + bot.py). Keep this in sync.
EXPECTED_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_CHAT_IDS",
    "OPENCODE_BACKEND",
    "OPENCODE_WORKDIR",
    "OPENCODE_TIMEOUT",
    "OPENCODE_CMD",
    "OPENCODE_PERMISSION_TIMEOUT",
    "OPENCODE_SERVER_HOST",
    "OPENCODE_SERVER_PORT",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_SERVER_BINARY",
    "OPENCODE_SERVER_ALLOW_REMOTE",
    "OPENAI_API_KEY",
    "BOT_LOG_FORMAT",
    "BOT_LOG_FILE",
    "EXPLORE_DB",
}

_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


@pytest.fixture(scope="module")
def content() -> str:
    assert ENV_EXAMPLE.is_file(), ".env.example missing at the repository root"
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def test_lists_every_variable_the_app_reads(content: str) -> None:
    keys = set(_KEY_RE.findall(content))

    assert keys == EXPECTED_KEYS


def test_has_no_real_looking_secret(content: str) -> None:
    # The three secret-bearing variables must use empty placeholders.
    for key in ("TELEGRAM_BOT_TOKEN", "OPENCODE_SERVER_PASSWORD", "OPENAI_API_KEY"):
        assert re.search(rf"^{key}=$", content, re.MULTILINE), (
            f"{key} must have an empty placeholder"
        )
    # Cheap guards against pasting a real token by accident.
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", content)
    assert not re.search(r"\bsk-[A-Za-z0-9]{16,}", content)
    assert not re.search(r"\bntn_[A-Za-z0-9]{20,}", content)


def test_permission_timeout_defaults_to_infinite(content: str) -> None:
    assert "OPENCODE_PERMISSION_TIMEOUT=0" in content


def test_required_defaults_are_documented(content: str) -> None:
    assert "OPENCODE_BACKEND=server" in content
    assert "OPENCODE_SERVER_HOST=127.0.0.1" in content
    assert "OPENCODE_SERVER_PORT=4096" in content
    assert "OPENCODE_TIMEOUT=300" in content
    assert "BOT_LOG_FORMAT=text" in content
