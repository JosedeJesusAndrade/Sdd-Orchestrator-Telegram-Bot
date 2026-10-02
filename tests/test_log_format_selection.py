"""Tests for formatter selection: make_formatter + BOT_LOG_FORMAT end-to-end."""
import os
import subprocess
import sys
from pathlib import Path

from utils.logging import JsonFormatter, KeyValueFormatter, make_formatter

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_make_formatter_json_returns_json_formatter() -> None:
    assert isinstance(make_formatter("json", "%(message)s"), JsonFormatter)


def test_make_formatter_text_returns_keyvalue_formatter() -> None:
    assert isinstance(make_formatter("text", "%(message)s"), KeyValueFormatter)


def test_make_formatter_unknown_falls_back_to_text() -> None:
    assert isinstance(make_formatter("yaml", "%(message)s"), KeyValueFormatter)
    assert isinstance(make_formatter("", "%(message)s"), KeyValueFormatter)


def _run_config_probe(
    *, log_format: str | None, log_file: Path
) -> subprocess.CompletedProcess[str]:
    """Import config in a clean subprocess and report handler[0] formatter."""
    env = {**os.environ, "ALLOWED_CHAT_IDS": "111", "BOT_LOG_FILE": str(log_file)}
    env.pop("BOT_LOG_FORMAT", None)
    if log_format is not None:
        env["BOT_LOG_FORMAT"] = log_format
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(type(config.logger.handlers[0].formatter).__name__)",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_env_json_selects_json_formatter(tmp_path: Path) -> None:
    result = _run_config_probe(log_format="json", log_file=tmp_path / "json.log")
    assert result.returncode == 0, result.stderr
    assert "JsonFormatter" in result.stdout


def test_env_unset_selects_text_formatter(tmp_path: Path) -> None:
    result = _run_config_probe(log_format=None, log_file=tmp_path / "text.log")
    assert result.returncode == 0, result.stderr
    assert "KeyValueFormatter" in result.stdout


def test_env_typo_falls_back_to_text_and_warns(tmp_path: Path) -> None:
    log_file = tmp_path / "typo.log"
    result = _run_config_probe(log_format="typo", log_file=log_file)
    assert result.returncode == 0, result.stderr
    assert "KeyValueFormatter" in result.stdout

    content = log_file.read_text(encoding="utf-8")
    assert "log_format_fallback" in content, (
        f"fallback warning missing from {log_file}: {content!r}"
    )


def test_env_json_handler_emits_json_lines(tmp_path: Path) -> None:
    """Real app handlers, JSON mode: an emitted record is one JSON object."""
    log_file = tmp_path / "emit.log"
    env = {
        **os.environ,
        "ALLOWED_CHAT_IDS": "111",
        "BOT_LOG_FILE": str(log_file),
        "BOT_LOG_FORMAT": "json",
    }
    script = "import config; config.logger.info('hola', extra={'event': 'probe_json', 'n': 7})"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    import json

    lines = [
        line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert lines, "file handler emitted nothing"
    payload = json.loads(lines[-1])
    assert payload["event"] == "probe_json"
    assert payload["n"] == 7
