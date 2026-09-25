"""Internationalization engine with OS language auto-detection."""
import json
import os
import sys
from pathlib import Path
from typing import Optional

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_DEFAULT_LANG = "en"
_LANG_KEY = "app_language"

# In-memory cache: {lang_code: {key: value}}
_strings: dict[str, dict[str, str]] = {}
_current_lang: str = _DEFAULT_LANG


def _detect_os_language() -> str:
    """Detect Windows UI language. Returns 'pt' or 'en'."""
    try:
        if sys.platform == "win32":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            primary = lang_id & 0xFF
            if primary == 0x16:  # Portuguese
                return "pt"
    except Exception:
        pass
    
    # Fallback: check LANG env var
    lang_env = os.getenv("LANG", "") or os.getenv("LC_ALL", "")
    if "pt" in lang_env.lower():
        return "pt"
    
    return "en"


def _load_locale(lang: str) -> dict[str, str]:
    """Load JSON locale file."""
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def init(lang: Optional[str] = None) -> str:
    """Initialize translation system. Auto-detects if no lang specified.
    
    Returns the active language code.
    """
    global _current_lang, _strings
    
    if lang is None:
        lang = _detect_os_language()
    
    # Load requested language
    strings = _load_locale(lang)
    if not strings and lang != _DEFAULT_LANG:
        strings = _load_locale(_DEFAULT_LANG)
        lang = _DEFAULT_LANG
    
    _strings[lang] = strings
    _current_lang = lang
    return lang


def set_language(lang: str) -> None:
    """Switch active language dynamically."""
    global _current_lang
    if lang not in _strings:
        _strings[lang] = _load_locale(lang)
    if _strings[lang]:
        _current_lang = lang


def t(key: str, default: str = "") -> str:
    """Translate a key. Falls back to default or key itself."""
    strings = _strings.get(_current_lang, {})
    return strings.get(key, default or key)


def current_lang() -> str:
    return _current_lang


def available_languages() -> list[tuple[str, str]]:
    """Return list of (code, name) for available languages."""
    result = []
    for f in sorted(_LOCALES_DIR.glob("*.json")):
        code = f.stem
        strings = _load_locale(code)
        name = strings.get("lang.name", code)
        result.append((code, name))
    return result


# Convenience alias
_ = t
