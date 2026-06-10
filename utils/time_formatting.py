"""Time formatting utilities for human-readable relative times.

Architecture rationale:
  Previously, _relative_time was a private function buried in
  handlers/messages.py — a module that has nothing to do with time.
  It was imported by commands.py and sessions.py, creating a
  dependency on messages.py for a pure utility function.

  Extracting it to utils/ makes it:
    - Discoverable (utilities belong in utils/)
    - Reusable without importing handler modules
    - Public (no underscore = part of the package's API)
"""

from datetime import datetime, timezone


def relative_time(past_dt: datetime) -> str:
    """Return a human-readable relative time string in Spanish.

    Examples:
        relative_time(now - 30s)   → "hace 30 seg"
        relative_time(now - 5min)  → "hace 5 min"
        relative_time(now - 3h)    → "hace 3h"
        relative_time(now - 2h30m) → "hace 2h 30min"
        relative_time(now - 2d)    → "hace 2d 0h"

    Args:
        past_dt: A timezone-aware datetime in the past.

    Returns:
        A Spanish string like "hace X seg/min/h/d".
    """
    seconds = int((datetime.now(timezone.utc) - past_dt).total_seconds())
    if seconds < 0:
        return "ahora"
    if seconds < 60:
        return "hace {} seg".format(seconds)
    minutes = seconds // 60
    if minutes < 60:
        return "hace {} min".format(minutes)
    hours = minutes // 60
    minutes_rem = minutes % 60
    if hours < 24:
        if minutes_rem:
            return "hace {}h {}min".format(hours, minutes_rem)
        return "hace {}h".format(hours)
    days = hours // 24
    hours_rem = hours % 24
    return "hace {}d {}h".format(days, hours_rem)
