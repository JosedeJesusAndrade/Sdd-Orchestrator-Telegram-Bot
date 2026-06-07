"""Logging utilities: chat ID masking for privacy."""
from ..config import logger as _logger


def mask_chat_id(chat_id: int) -> str:
    """Partially mask chat ID for log privacy — first 2 and last 2 digits visible."""
    s = str(chat_id)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]
