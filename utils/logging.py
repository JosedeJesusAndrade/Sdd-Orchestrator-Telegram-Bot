"""Logging utilities: chat ID masking, request_id context, key=value formatter.

This module is the SINGLE SOURCE OF TRUTH for:
  - `request_id` context propagation (via contextvars — asyncio-safe)
  - `RequestIdFilter` (injects request_id into every log record)
  - `mask_chat_id` (privacy: first-2/last-2 visible digits)
  - `KeyValueFormatter` (formats log records as `event=NAME key=value "message"`)
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
        return self.formatMessage(record)

    def _format_extras(self, record: logging.LogRecord) -> str:
        """Return 'key=value key=value ...' from record's non-standard fields.

        Iteration order: Python's object.__dict__ preserves insertion order
        (CPython 3.7+ guarantee). Since Logger.makeRecord sets extras via
        setattr() in iteration order of the extras dict, the order in the
        log output matches the order of the extras dict passed to logger.*().

        Privacy contract: `chat_id` is ALWAYS emitted as `chat_id_masked`,
        regardless of its type (int or str). If the caller passes both
        `chat_id` and `chat_id_masked`, the raw `chat_id` is dropped (the
        formatter's auto-mask is authoritative) to prevent raw IDs leaking
        into the log sink.
        """
        parts: list[str] = []
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

            parts.append(f"{key}={_format_value(value)}")
        return " ".join(parts)


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
