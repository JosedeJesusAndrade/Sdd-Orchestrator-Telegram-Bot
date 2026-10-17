"""Shared fixtures for bot tests."""

import json
import os
import tempfile
from pathlib import Path

import pytest

# Redirect logging to a temp file so pytest runs never pollute the
# production bot.log. config.py no longer configures logging at import time
# (imports are side-effect free), but we still set BOT_LOG_FILE BEFORE the
# import and call setup_logger() explicitly: belt-and-suspenders, and the
# suite needs the configured handlers for the end-to-end logging tests.
# Unconditional (not setdefault) so a stray BOT_LOG_FILE exported in the
# developer's environment can never point a test run at the production log.
_TEST_LOG_DIR = Path(tempfile.gettempdir()) / "sdd-bot-tests"
_TEST_LOG_DIR.mkdir(exist_ok=True)
os.environ["BOT_LOG_FILE"] = str(_TEST_LOG_DIR / "bot-test.log")

from config import DEFAULT_SESSION_NAME, setup_logger  # noqa: E402

setup_logger()


@pytest.fixture
def tmp_sessions_json():
    """Create a temporary sessions.json for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({}, f)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def sample_session_data():
    """Sample sessions.json data for testing."""
    return {
        "123456789": {
            "active": DEFAULT_SESSION_NAME,
            "model": "deepseek/deepseek-v4-pro",
            "sessions": {
                "default": {
                    "id": "ses_TEST123abc",
                    "title": "Test session",
                    "created": "2026-05-19T00:00:00",
                    "last_used": "2026-05-19T01:00:00",
                    "prompt_count": 5,
                }
            },
        }
    }
