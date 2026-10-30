"""Regression: ``bot_start`` must self-document the effective backend.

Finding A of the 6.8 live verification: the CLI mode was only inferable from
the indirect ``permission_presenter_skipped backend=cli`` event, so a silent
revert from ``server`` (the default) to ``cli`` (the rollback) went unnoticed.
"""

from __future__ import annotations

import logging

import pytest

import bot


def test_bot_start_logs_the_effective_backend(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(bot, "OPENCODE_BACKEND", "cli")

    with caplog.at_level(logging.INFO, logger="opencode_bot.bot"):
        bot._log_bot_start()

    records = [record for record in caplog.records if getattr(record, "event", "") == "bot_start"]
    assert len(records) == 1
    assert getattr(records[0], "backend", None) == "cli"
