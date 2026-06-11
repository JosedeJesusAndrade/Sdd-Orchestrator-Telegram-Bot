"""I18n: returns the appropriate strings module for the given language."""
from typing import Any

_LOCALES: dict[str, Any] = {}

def _load_locales() -> None:
    if _LOCALES:
        return
    from locales import es
    _LOCALES["es"] = es

def get_strings(lang: str = "es"):
    _load_locales()
    return _LOCALES.get(lang, _LOCALES["es"])
