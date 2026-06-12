"""Shared fixtures for bot tests."""
import pytest
import json
import tempfile
from pathlib import Path

from config import DEFAULT_SESSION_NAME

@pytest.fixture
def tmp_sessions_json():
    """Create a temporary sessions.json for testing."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
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
                    "prompt_count": 5
                }
            }
        }
    }
