"""Regression: importing library modules must NOT write to the production log.

`config.py` used to call `setup_logger()` at module level, so ANY
`import config` (and therefore `import opencode.client`, the services, or an
ad-hoc script) attached the real RotatingFileHandler to `bot.log`. These
subprocess tests leave `BOT_LOG_FILE` unset (so config resolves the production
path) and assert `bot.log` is byte-for-byte untouched.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_LOG = PROJECT_ROOT / "bot.log"


def _snapshot() -> tuple[int, int] | None:
    """Return (size, mtime_ns) of bot.log, or None when it does not exist."""
    if not BOT_LOG.exists():
        return None
    stat = BOT_LOG.stat()
    return stat.st_size, stat.st_mtime_ns


def _run(script: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ALLOWED_CHAT_IDS": "111"}
    # Point config back at the REAL production path, as an ad-hoc script would.
    env.pop("BOT_LOG_FILE", None)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_import_config_does_not_touch_bot_log() -> None:
    before = _snapshot()
    result = _run("import config")
    assert result.returncode == 0, result.stderr
    assert _snapshot() == before


def test_import_opencode_client_does_not_touch_bot_log() -> None:
    before = _snapshot()
    result = _run("import opencode.client")
    assert result.returncode == 0, result.stderr
    assert _snapshot() == before


def test_import_opencode_server_package_does_not_touch_bot_log() -> None:
    before = _snapshot()
    result = _run("import services.opencode_server")
    assert result.returncode == 0, result.stderr
    assert _snapshot() == before


def test_import_server_lifecycle_does_not_touch_bot_log() -> None:
    before = _snapshot()
    result = _run("import services.server_lifecycle")
    assert result.returncode == 0, result.stderr
    assert _snapshot() == before


def test_import_does_not_configure_handlers() -> None:
    result = _run("import config; print(len(config.logger.handlers))")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_setup_logger_still_writes_to_configured_file(tmp_path: Path) -> None:
    """The app path keeps working: setup_logger() attaches the file handler."""
    log_file = tmp_path / "app.log"
    env = {**os.environ, "ALLOWED_CHAT_IDS": "111", "BOT_LOG_FILE": str(log_file)}
    script = (
        "import config; config.setup_logger(); "
        "config.logger.warning('hola', extra={'event': 'probe_write'})"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "probe_write" in log_file.read_text(encoding="utf-8")


def test_setup_logger_is_idempotent(tmp_path: Path) -> None:
    """Calling it twice must not duplicate handlers (entry point + embedder)."""
    log_file = tmp_path / "idempotent.log"
    env = {**os.environ, "ALLOWED_CHAT_IDS": "111", "BOT_LOG_FILE": str(log_file)}
    script = (
        "import config; config.setup_logger(); config.setup_logger(); "
        "print(len(config.logger.handlers))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"
