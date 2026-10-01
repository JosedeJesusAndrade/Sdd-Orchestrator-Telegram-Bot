"""Tests for log_exception() and the `opencode_bot.swallowed` namespace.

These assert actual log delivery (not just business behavior) because that is
the #337/#338 regression class: a logger resolving outside the `opencode_bot`
hierarchy is silently dropped. They also cover the one-knob silence mechanism
and the field shape of a representative inline weak-log fix.
"""
import logging

from utils.logging import log_exception

SWALLOWED = "opencode_bot.swallowed"


class _Probe(logging.Handler):
    """Collects records emitted to a logger, for assertions."""

    def __init__(self, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _raise() -> None:
    raise RuntimeError("boom")


def test_swallowed_record_reaches_namespace() -> None:
    logger = logging.getLogger(SWALLOWED)
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    probe = _Probe(level=logging.DEBUG)
    logger.addHandler(probe)
    try:
        try:
            _raise()
        except RuntimeError:
            log_exception(
                "boom_event", module="tests.swallowed", level=logging.DEBUG, k="v"
            )

        assert len(probe.records) == 1, "swallowed record did not reach namespace"
        record = probe.records[0]
        assert record.name == "opencode_bot.swallowed.tests.swallowed"
        assert record.msg == "boom_event"
        assert record.__dict__["event"] == "boom_event"
        assert record.__dict__["k"] == "v"
        assert record.exc_info is not None
    finally:
        logger.removeHandler(probe)
        logger.setLevel(original_level)


def test_level_is_honored() -> None:
    logger = logging.getLogger(SWALLOWED)
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    probe = _Probe(level=logging.DEBUG)
    logger.addHandler(probe)
    try:
        try:
            _raise()
        except RuntimeError:
            log_exception(
                "warn_event", module="tests.levels", level=logging.WARNING
            )

        assert len(probe.records) == 1
        assert probe.records[0].levelno == logging.WARNING
    finally:
        logger.removeHandler(probe)
        logger.setLevel(original_level)


def test_one_knob_silences_swallowed_tree() -> None:
    """Setting the parent namespace to WARNING mutes child DEBUG records."""
    logger = logging.getLogger(SWALLOWED)
    original_level = logger.level
    parent = logging.getLogger("opencode_bot")
    parent_probe = _Probe(level=logging.DEBUG)
    parent.addHandler(parent_probe)
    logger.setLevel(logging.WARNING)
    try:
        try:
            _raise()
        except RuntimeError:
            log_exception(
                "muted_event", module="tests.muted", level=logging.DEBUG
            )

        assert parent_probe.records == []
    finally:
        logger.setLevel(original_level)
        parent.removeHandler(parent_probe)


def test_configured_handlers_pass_debug_end_to_end() -> None:
    """Regression #358: config handlers must not filter DEBUG.

    Verbosity is controlled by LOGGERS, not handlers: `opencode_bot` sits at
    INFO (normal verbosity) and `opencode_bot.swallowed` at SWALLOWED_LOG_LEVEL
    (swallowed verbosity). If a handler were set to INFO it would silently drop
    every DEBUG swallowed record, making SWALLOWED_LOG_LEVEL ineffective. This
    asserts the handlers pass DEBUG and that a DEBUG swallowed record is
    delivered end-to-end through the real logger chain.
    """
    import config

    handlers = config.logger.handlers
    assert handlers, "config.logger has no handlers attached"
    assert all(h.level <= logging.DEBUG for h in handlers), (
        "handler filters DEBUG: "
        f"{[(type(h).__name__, h.level) for h in handlers]}"
    )

    # Delivery proof without writing to the real bot.log: temporarily detach the
    # real handlers (which target bot.log) and attach an in-memory probe.
    probe = _Probe(level=logging.NOTSET)
    saved = list(handlers)
    for handler in saved:
        config.logger.removeHandler(handler)
    config.logger.addHandler(probe)
    try:
        try:
            _raise()
        except RuntimeError:
            log_exception(
                "probe_event", module="tests.handler_level", level=logging.DEBUG
            )

        assert len(probe.records) == 1, "DEBUG swallowed record dropped by handlers"
        assert probe.records[0].levelno == logging.DEBUG
    finally:
        config.logger.removeHandler(probe)
        for handler in saved:
            config.logger.addHandler(handler)


async def test_inline_weak_log_has_event_and_exc_info() -> None:
    """Representative inline weak-log fix: event= plus exc_info attached."""
    from services.message_sender import MessageSender

    class _BoomBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("nope")

    logger = logging.getLogger("opencode_bot.services.message_sender")
    probe = _Probe(level=logging.DEBUG)
    logger.addHandler(probe)
    try:
        sender = MessageSender(_BoomBot())
        await sender.send_plain(123456789, "hi")

        assert len(probe.records) == 1
        record = probe.records[0]
        assert record.__dict__["event"] == "send_plain_error"
        assert record.exc_info is not None
    finally:
        logger.removeHandler(probe)
