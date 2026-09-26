"""Tests for KeyValueFormatter output shape and security (chat_id masking)."""
import logging

from utils.logging import KeyValueFormatter


def _capture(formatter: KeyValueFormatter, level: int, msg: str, **extra) -> str:
    """Emit one record and return the formatted output."""
    record = logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return formatter.format(record)


def test_output_with_extras_renders_key_value() -> None:
    fmt = KeyValueFormatter("%(levelname)s %(name)s: %(message)s")
    out = _capture(fmt, logging.INFO, "Session created",
                   event="session_created", session_name="foo")
    assert "event=session_created" in out
    assert "session_name=foo" in out
    assert "Session created" in out


def test_output_without_extras_shows_only_message() -> None:
    fmt = KeyValueFormatter("%(levelname)s %(name)s: %(message)s")
    out = _capture(fmt, logging.INFO, "just a message")
    assert "just a message" in out
    assert " = " not in out and "=None" not in out


def test_chat_id_is_always_masked() -> None:
    fmt = KeyValueFormatter("%(levelname)s %(name)s: %(message)s")
    out1 = _capture(fmt, logging.INFO, "msg", event="x", chat_id=8664220427)
    assert "8664220427" not in out1, f"raw chat_id leaked: {out1}"
    assert "chat_id_masked=" in out1

    out2 = _capture(fmt, logging.INFO, "msg", event="x",
                    chat_id=8664220427, chat_id_masked="CALLER_PRE_MASKED")
    assert "8664220427" not in out2, f"raw chat_id leaked even when masked was passed: {out2}"
    assert "chat_id_masked=CALLER_PRE_MASKED" in out2, (
        "the caller's pre-masked value should be kept verbatim when both "
        "chat_id and chat_id_masked are passed (raw id is the security risk)"
    )


def test_string_chat_id_is_also_masked() -> None:
    fmt = KeyValueFormatter("%(levelname)s %(name)s: %(message)s")
    out = _capture(fmt, logging.INFO, "msg", event="x", chat_id="8664220427")
    assert "8664220427" not in out, f"raw string chat_id leaked: {out}"
    assert "chat_id_masked=" in out


def test_multi_handler_no_duplication() -> None:
    """Two handlers attached to the same logger should each see ONE record, not two."""
    logger = logging.getLogger("test.multi")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = KeyValueFormatter("%(levelname)s: %(message)s")
    sink_a: list[str] = []
    sink_b: list[str] = []

    class _Sink(logging.Handler):
        def __init__(self, sink: list[str]) -> None:
            super().__init__()
            self.sink = sink
        def emit(self, record: logging.LogRecord) -> None:
            self.sink.append(fmt.format(record))

    logger.addHandler(_Sink(sink_a))
    logger.addHandler(_Sink(sink_b))

    logger.info("hello", extra={"event": "x", "k": "v"})

    assert len(sink_a) == 1, f"sink_a should have 1, got {len(sink_a)}: {sink_a}"
    assert len(sink_b) == 1, f"sink_b should have 1, got {len(sink_b)}: {sink_b}"
    for s in (sink_a[0], sink_b[0]):
        assert s.count("event=") == 1, f"event= duplicated in {s}"
