"""Tests for JsonFormatter: schema, key parity, masking, exc gating, safety.

The masking tests are the security contract: JSON mode MUST apply the same
`chat_id -> chat_id_masked` rule as text mode (shared _extract_structured_fields).
"""
import json
import logging
import sys
from types import TracebackType

from utils.logging import (
    JsonFormatter,
    KeyValueFormatter,
    _extract_structured_fields,
    mask_chat_id,
)


def _record(level: int, msg: str, exc_info=None, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=0,
        msg=msg, args=(), exc_info=exc_info,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def _caught_exc_info() -> (
    tuple[type[BaseException] | None, BaseException | None, TracebackType | None]
):
    try:
        raise ValueError("explota")
    except ValueError:
        return sys.exc_info()


def test_output_is_valid_json_on_one_line() -> None:
    out = JsonFormatter().format(_record(logging.INFO, "hola", event="prueba", n=1))
    assert "\n" not in out, "JSON output must be one physical line"
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["msg"] == "hola"
    assert payload["event"] == "prueba"
    assert payload["n"] == 1


def test_key_parity_with_text_formatter() -> None:
    record = _record(logging.INFO, "paridad", event="evt", k="v", n=3, flag=True)
    fields = _extract_structured_fields(record)

    payload = json.loads(JsonFormatter().format(record))
    for key, value in fields.items():
        assert key in payload, f"field {key!r} lost in JSON"
        assert payload[key] == value, f"field {key!r} value drifted in JSON"

    json_only = set(payload) - set(fields)
    assert json_only <= {"ts", "level", "logger", "req", "msg", "exc"}, (
        f"JSON invented unexpected fields: {json_only}"
    )

    text = KeyValueFormatter("%(levelname)s: %(message)s").format(record)
    for key in fields:
        assert f"{key}=" in text, f"field {key!r} lost in text"


def test_chat_id_is_masked_in_json() -> None:
    out = JsonFormatter().format(
        _record(logging.INFO, "msg", event="x", chat_id=8664220427)
    )
    assert "8664220427" not in out, f"raw chat_id leaked into JSON: {out}"
    payload = json.loads(out)
    assert payload["chat_id_masked"] == mask_chat_id(8664220427)
    assert "chat_id" not in payload


def test_string_chat_id_is_masked_in_json() -> None:
    out = JsonFormatter().format(
        _record(logging.INFO, "msg", event="x", chat_id="8664220427")
    )
    assert "8664220427" not in out
    assert json.loads(out)["chat_id_masked"] == mask_chat_id("8664220427")


def test_both_chat_id_and_premasked_drops_raw_in_json() -> None:
    out = JsonFormatter().format(
        _record(
            logging.INFO,
            "msg",
            event="x",
            chat_id=8664220427,
            chat_id_masked="CALLER_PRE_MASKED",
        )
    )
    assert "8664220427" not in out
    assert json.loads(out)["chat_id_masked"] == "CALLER_PRE_MASKED"


def test_exc_only_present_with_exc_info() -> None:
    plain = json.loads(JsonFormatter().format(_record(logging.INFO, "sin error", event="x")))
    assert "exc" not in plain

    payload = json.loads(
        JsonFormatter().format(
            _record(logging.ERROR, "con error", exc_info=_caught_exc_info(), event="x")
        )
    )
    assert "Traceback" in payload["exc"], f"traceback missing: {payload['exc']!r}"
    assert "ValueError" in payload["exc"]


def test_unsafe_value_becomes_string() -> None:
    obj = object()
    payload = json.loads(
        JsonFormatter().format(_record(logging.INFO, "obj", event="x", obj=obj))
    )
    assert isinstance(payload["obj"], str)
    assert "object object" in payload["obj"]


def test_circular_reference_degrades_without_crash() -> None:
    loop: list = []
    loop.append(loop)
    out = JsonFormatter().format(_record(logging.INFO, "circular", event="x", loop=loop))
    payload = json.loads(out)
    assert isinstance(payload["loop"], str), "circular container must degrade to a string"
    assert payload["event"] == repr("x"), "extras degrade to repr() strings (design D4)"
    assert payload["msg"] == repr("circular"), "degraded payload must keep the other fields"


def test_spanish_accents_are_not_escaped() -> None:
    out = JsonFormatter().format(
        _record(logging.INFO, "sesión creada", event="x", usuario="María")
    )
    assert "sesión" in out, "ensure_ascii=False must keep accents readable"
    assert "María" in out


def test_req_defaults_to_dash_without_filter() -> None:
    payload = json.loads(JsonFormatter().format(_record(logging.INFO, "x", event="e")))
    assert payload["req"] == "-"


def test_req_reads_request_id_attribute() -> None:
    record = _record(logging.INFO, "x", event="e")
    record.request_id = "abc12345"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["req"] == "abc12345"


def test_field_order_is_event_then_extras_then_msg() -> None:
    payload = json.loads(
        JsonFormatter().format(_record(logging.INFO, "hola", event="evt", k="v"))
    )
    assert list(payload) == ["ts", "level", "logger", "req", "event", "k", "msg"]
