"""Verify module loggers actually propagate records to handlers.

This test exists because Phase 2 regressed logging: a switch to
`logging.getLogger(__name__)` made records propagate to root (which had no
handlers) instead of the `opencode_bot` logger that HAS handlers. The 33
existing tests didn't catch this because they assert business behavior, not
log delivery. This test asserts the chain end-to-end.
"""
import logging


def test_module_logger_propagates_to_opencode_bot() -> None:
    from utils.logging import get_module_logger

    probe = logging.getLogger("opencode_bot")
    records: list[logging.LogRecord] = []

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    probe_handler = _Probe(level=logging.DEBUG)
    probe.addHandler(probe_handler)

    try:
        logger = get_module_logger("handlers.test_propagation")
        logger.info("hello", extra={"event": "propagation_test"})

        assert len(records) == 1, "module logger did not propagate to opencode_bot"
        assert records[0].name == "opencode_bot.handlers.test_propagation"
        assert records[0].msg == "hello"
    finally:
        probe.removeHandler(probe_handler)
