"""Tests for the fail-fast ALLOWED_CHAT_IDS validation (roadmap item 4).

Primary coverage exercises the pure parser. One subprocess test per failure
mode proves a bad/empty value exits with code 1 WITHOUT killing the runner.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import parse_allowed_chat_ids

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_parse_single_id() -> None:
    assert parse_allowed_chat_ids("123") == [123]


def test_parse_csv_with_whitespace() -> None:
    assert parse_allowed_chat_ids(" 1 , 2 ,3 ") == [1, 2, 3]


def test_parse_empty_returns_empty_list() -> None:
    assert parse_allowed_chat_ids("") == []
    assert parse_allowed_chat_ids("   ,  ") == []


def test_parse_non_numeric_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_allowed_chat_ids("abc")


def _import_config_with(raw: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ALLOWED_CHAT_IDS": raw}
    return subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_non_numeric_env_value_exits_with_code_1() -> None:
    result = _import_config_with("not-a-number")
    assert result.returncode == 1


def test_empty_env_value_exits_with_code_1() -> None:
    result = _import_config_with("")
    assert result.returncode == 1


def test_valid_env_value_imports_cleanly() -> None:
    result = _import_config_with("111,222")
    assert result.returncode == 0
