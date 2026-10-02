"""Logging utilities: chat ID masking, request_id context, formatters.

This module is the SINGLE SOURCE OF TRUTH for:
  - `request_id` context propagation (via contextvars — asyncio-safe)
  - `RequestIdFilter` (injects request_id into every log record)
  - `mask_chat_id` (privacy: first-2/last-2 visible digits)
  - `_extract_structured_fields` (shared extras extraction + masking rule)
  - `KeyValueFormatter` (formats log records as `event=NAME key=value "message"`)
  - `JsonFormatter` (same structured data, one JSON object per line)
  - `make_formatter` (selects text vs JSON from the BOT_LOG_FORMAT flag)
  - `get_module_logger()` (namespaces module loggers under `opencode_bot` so
    records propagate to the handlers attached by `config.setup_logger()`)

Why a custom formatter?
  The bot emits structured logs (event=NAME key=value) that need to be both
  human-readable in the terminal AND grep-friendly in bot.log. The standard
  `logging.Formatter` only handles the prefix (timestamp, level, logger name);
  we extend it to extract `extra={}` fields and append them as key=value pairs.

Why a namespaced logger hierarchy?
  Python's logging parent chain follows dotted-name prefixes: a logger named
  `opencode_bot.handlers.messages` propagates up to `opencode_bot.handlers`
  and then to `opencode_bot`. `config.setup_logger()` attaches its handlers
  to the `opencode_bot` logger, so every namespaced child logger inherits
  those handlers automatically — without this, `logging.getLogger(__name__)`
  resolves to siblings of `opencode_bot` (not children) and records are
  silently dropped (see #337 verify-report for the regression this fixed).
"""
from __future__ import annotations

import contextvars
import json
import logging
import uuid


def get_module_logger(name: str | None = None) -> logging.Logger:
    """Return a logger namespaced under opencode_bot.

    Convention: every module uses `get_module_logger(__name__)` instead of
    `logging.getLogger(__name__)`. This keeps all app logs under the
    `opencode_bot.*` hierarchy so they propagate to the root handler attached
    in config.setup_logger(), and so future SDK logs (Flet, httpx, telegram,
    opencode) can be filtered independently.
    """
    if name is None:
        name = __name__
    return logging.getLogger(f"opencode_bot.{name}")


# ── Swallowed exceptions (roadmap item 4) ───────────────────────────────
# WHY A DEDICATED LOGGER NAMESPACE:
#   `log_exception()` records caught-AND-recovered exceptions (the "silent
#   except" pattern) so they leave a forensic trail without becoming user
#   errors. Records go to `opencode_bot.swallowed.<module>`, NOT to the
#   module's normal `opencode_bot.<module>` logger.
#
#   - Traceability: a corrupt sessions.json, a zombie subprocess, or a
#     Telegram edit that never lands is invisible today. This namespace
#     makes every recovery greppable (`grep event=... bot.log`) w/ traceback.
#   - One-knob silencing (ETC): because the name is hierarchical, ONE line
#     in config.setup_logger() silences every swallowed log at once:
#         logging.getLogger("opencode_bot.swallowed").setLevel(logging.WARNING)
#     Children inherit the parent level, so the knob covers all modules.
#   - Hierarchical filtering: raising a single child
#     (`...swallowed.services.message_sender`) isolates one noisy module.
#   - TRADEOFF: records appear under `opencode_bot.swallowed.<module>` instead
#     of the usual `opencode_bot.<module>` name, so the owning module is
#     encoded in the swallowed logger name. Deliberate: it is the price for
#     independent one-knob control and keeps swallowed noise out of the
#     normal module stream.
def log_exception(
    event: str, *, module: str, level: int = logging.DEBUG, **fields: object
) -> None:
    """Log a caught-and-recovered exception with traceback into
    `opencode_bot.swallowed.<module>`.

    MUST be called from inside an `except` block: `exc_info=True` reads
    sys.exc_info() at call time, so no `e` argument is needed.

    Args:
        event: Machine-readable event name (e.g. "session_map_load_error").
        module: Caller's `__name__` — used to build the swallowed namespace.
        level: Severity (default DEBUG). Driven by the design severity table.
        **fields: Structured fields rendered as key=value by KeyValueFormatter
            (e.g. path=..., error_type=..., pid=...).
    """
    logging.getLogger(f"opencode_bot.swallowed.{module}").log(
        level, event, exc_info=True, extra={"event": event, **fields}
    )


# ── Request ID context propagation ────────────────────────────────────────

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    """Generate a short unique request ID (8 hex chars) and store in context.

    Why 8 hex chars (32 bits)? Enough entropy to be unique within a bot session
    while keeping the file log readable. Storing in `request_id_var` (a
    contextvars.ContextVar) makes it asyncio-safe — each coroutine sees its
    own value without explicit threading.

    Returns:
        The generated request_id (also stored in request_id_var).
    """
    rid = uuid.uuid4().hex[:8]
    request_id_var.set(rid)
    return rid


class RequestIdFilter(logging.Filter):
    """Inject request_id from context into every log record.

    The bot's file handler uses `[req=%(request_id)s]` in its format template,
    so every record must have a `request_id` attribute. This filter runs on
    every record and pulls the current value from the contextvar.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


# ── Chat ID masking (privacy) ─────────────────────────────────────────────

def mask_chat_id(chat_id: int | str) -> str:
    """Partially mask chat ID for log privacy — first 2 and last 2 digits visible.

    Examples:
        8664220427 → "86******27"  (6 chars hidden in the middle)
        42         → "42"          (short IDs preserved for debugging)
        1234       → "****"        (4-char threshold; fully masked)

    Why first-2 + last-2? Common pattern for partial PII redaction. Reveals
    enough context to correlate logs across a session while hiding the bulk
    of the identifier. Telegram chat IDs are typically 9-10 digits.
    """
    s = str(chat_id)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# ── KeyValueFormatter ─────────────────────────────────────────────────────

# Standard LogRecord attributes — copied from the Python logging module docs
# (https://docs.python.org/3/library/logging.html#logrecord-attributes).
# We skip these when iterating record.__dict__ to find user-provided extras.
_STANDARD_RECORD_FIELDS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    # Our additions — populated by RequestIdFilter or our formatter, never user extras:
    "request_id",
})


def _extract_structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return the ordered user extras from `record`, applying the privacy rule.

    This is the SINGLE extraction implementation shared by BOTH formatters
    (text and JSON). Keeping it in one place guarantees the JSON output
    inherits the exact same security contract — a re-implementation could
    drift and leak a raw `chat_id`.

    Iteration order: Python's object.__dict__ preserves insertion order
    (CPython 3.7+ guarantee). Since Logger.makeRecord sets extras via
    setattr() in iteration order of the extras dict, the order in the output
    matches the order of the extras dict passed to logger.*().

    Skips `_STANDARD_RECORD_FIELDS` (stdlib LogRecord attributes) and any
    key starting with "_" (private/internal attributes).

    Privacy contract: `chat_id` is ALWAYS emitted as `chat_id_masked`,
    regardless of its type (int or str). If the caller passes both
    `chat_id` and `chat_id_masked`, the raw `chat_id` is dropped (the
    formatter's auto-mask is authoritative) to prevent raw IDs leaking
    into the log sink.
    """
    fields: dict[str, object] = {}
    for key in record.__dict__:
        if key in _STANDARD_RECORD_FIELDS:
            continue
        if key.startswith("_"):
            continue
        value = record.__dict__[key]

        if key == "chat_id":
            # Security: never emit raw chat_id, even if the caller also
            # passed a pre-masked value. The formatter's auto-mask wins.
            if "chat_id_masked" in record.__dict__:
                continue
            key = "chat_id_masked"
            value = mask_chat_id(value if isinstance(value, (int, str)) else str(value))

        fields[key] = value
    return fields


class KeyValueFormatter(logging.Formatter):
    """Format log records as `key=value key=value "message"`.

    Inherits from logging.Formatter; the standard template (with
    %(asctime)s, %(levelname)s, %(name)s, %(message)s) is preserved.
    The %(message)s placeholder gets the human-readable message PLUS
    all extra={} fields.

    Output shape:
        <prefix from template>event=NAME key=value "message"

    Example with template `"%(asctime)s [%(levelname)s] %(name)s: %(message)s"`:
        logger.info("Session created",
                    extra={"event": "session_created",
                           "session_name": "foo",
                           "chat_id": 8664220427})
        →
        "2026-09-06 17:25:42 [INFO] services.session_store: event=session_created session_name=foo chat_id_masked=86******27 \"Session created\""

    Special handling:
        - `chat_id` field → auto-renamed to `chat_id_masked` with masked value
        - Values containing whitespace → wrapped in double quotes
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record by enriching %(message)s with extras.

        We override format() instead of formatMessage() so we can preserve
        the standard `super().format()` machinery (asctime/levelname/name
        placeholder substitution) and only intercept the `%(message)s` part.

        Mutates `record.message` so the template's `%(message)s` expands to
        our key=value enriched version. This is safe because logging is
        single-threaded per handler.
        """
        # Step 1: standard LogRecord processing
        record.message = record.getMessage()
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)

        # Step 2: build key=value portion from extras
        extras_str = self._format_extras(record)
        user_message = record.message

        # Step 3: overwrite record.message so %(message)s in the template
        # expands to our enriched version. If no extras, just quote the message.
        if extras_str:
            record.message = f'{extras_str} "{user_message}"'
        else:
            record.message = f'"{user_message}"'

        # Step 4: delegate to formatMessage (template substitution)
        s = self.formatMessage(record)

        # Step 5: replicate the stdlib exception/stack tail. We cannot call
        # super().format(record): it would reset record.message to the bare
        # message, wiping the key=value enrichment built above. stdlib
        # Formatter.format() appends record.exc_text / stack_info AFTER the
        # template — this mirrors that behavior verbatim, so the traceback of
        # every log_exception(..., exc_info=True) record actually reaches the
        # sink.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s and not s.endswith("\n"):
                s += "\n"
            s += record.exc_text
        if record.stack_info:
            if s and not s.endswith("\n"):
                s += "\n"
            s += self.formatStack(record.stack_info)
        return s

    def _format_extras(self, record: logging.LogRecord) -> str:
        """Return 'key=value key=value ...' from the shared structured fields.

        Thin join over `_extract_structured_fields` (the single source of
        truth for extraction + the chat_id masking contract, shared with
        JsonFormatter).
        """
        return " ".join(
            f"{key}={_format_value(value)}"
            for key, value in _extract_structured_fields(record).items()
        )


def _format_value(value: object) -> str:
    """Format a single value for key=value output.

    Rules:
      - bool → "True" / "False" (bare)
      - int / float → bare (e.g. status=429, duration_s=1.234)
      - str with whitespace → wrapped in double quotes (e.g. error="timeout")
      - str without whitespace → bare (e.g. event=session_created)
      - other (None, list, dict) → repr wrapped in single quotes
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if any(c.isspace() for c in value):
            return f'"{value}"'
        return value
    return f"'{value!r}'"


# ── JsonFormatter (roadmap item 6) ────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one JSON line.

    Sibling of KeyValueFormatter: same data model (extra={} fields), different
    representation (DTO per use case). Selected by BOT_LOG_FORMAT=json.

    Payload order (stable — easy to eyeball and jq):
        ts, level, logger, req, event, <extras...>, msg [, exc]

    - `_extract_structured_fields` supplies the extras, so the
      `chat_id -> chat_id_masked` security rule is identical to text mode
      (one implementation, no drift).
    - `exc` is included ONLY when `record.exc_info` is set.
    - NO pretty-print: one record = one physical line (JSON Lines); the
      traceback's newlines are escaped by json.dumps.
    - `ensure_ascii=False` so Spanish accents render correctly under the
      utf-8 handlers.
    - `default=str` stringifies datetimes/Exceptions/objects; circular
      containers bypass it and raise, hence the degraded re-dump below.

    Timestamp precision: seconds, same as text mode. The prompt preferred
    milliseconds, but `logging.Formatter.formatTime` delegates to
    `time.strftime`, which does NOT portably support `%f`; adding them would
    require a custom formatTime override. Second precision keeps both formats
    correlatable (design D3 explicitly chose the same precision as text).
    """

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        fields = _extract_structured_fields(record)
        event = fields.pop("event", None)

        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "req": getattr(record, "request_id", "-"),
        }
        if event is not None:
            payload["event"] = event
        payload.update(fields)
        payload["msg"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # `default=str` covers objects/datetimes/exceptions, but circular
            # containers bypass it and raise. A log line must NEVER be lost to
            # serialization: degrade every value to repr() and retry — ugly
            # output beats a missing log.
            degraded = {key: repr(value) for key, value in payload.items()}
            return json.dumps(degraded, ensure_ascii=False)


def make_formatter(
    format_name: str, template: str, *, datefmt: str | None = None
) -> logging.Formatter:
    """Return the formatter for 'text' (default) or 'json'.

    Unknown values fall back to text, matching the forgiving
    `BOT_LOG_FORMAT` contract in config.py (a typo must never crash the bot).
    """
    if format_name == "json":
        return JsonFormatter()
    return KeyValueFormatter(template, datefmt=datefmt)
