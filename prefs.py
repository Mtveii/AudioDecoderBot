import json
import logging
import threading
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Whisper supported languages (99) + auto
# https://github.com/openai/whisper/blob/main/whisper/tokenizer.py
WHISPER_LANGS = {
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "zh", "yue"
}

# Human readable names for help
LANG_NAMES = {
    "ru": "русский", "en": "английский", "uk": "українська", "be": "беларуская",
    "kk": "қазақша", "uz": "oʻzbek", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "pl": "Polski", "tr": "Türkçe",
    "ar": "العربية", "he": "עברית", "hi": "हिन्दी", "ja": "日本語",
    "zh": "中文", "ko": "한국어", "pt": "Português", "nl": "Nederlands",
    "sv": "Svenska", "no": "Norsk", "fi": "Suomi", "cs": "Čeština",
    "sk": "Slovenčina", "ro": "Română", "bg": "Български", "sr": "Српски",
    "hr": "Hrvatski", "hu": "Magyar", "el": "Ελληνικά", "th": "ไทย",
    "vi": "Tiếng Việt", "id": "Indonesia", "ms": "Melayu", "fa": "فارسی",
    "ka": "ქართული", "hy": "Հայերեն", "az": "Azərbaycan", "yue": "粵語",
}

# In-memory storage
_user_lang: dict[str, str] = {}  # user_id(str) -> lang code
_user_tone: dict[str, bool] = {}  # user_id(str) -> enabled

_lock = threading.Lock()
_prefs_path = Path(config.USER_PREFS_PATH)

# Reserved commands that should NOT be treated as lang codes
RESERVED_CODES = {"ton", "check", "auto", "start", "help", "lang"}


def _load() -> None:
    if not _prefs_path.exists():
        return
    try:
        data = json.loads(_prefs_path.read_text(encoding="utf-8"))
        langs = data.get("langs", {})
        tones = data.get("tones", {})
        # validate
        for k, v in langs.items():
            if isinstance(v, str) and v.lower() in WHISPER_LANGS:
                _user_lang[str(k)] = v.lower()
        for k, v in tones.items():
            _user_tone[str(k)] = bool(v)
        logger.info(f"prefs loaded: {len(_user_lang)} langs, {len(_user_tone)} tones from {_prefs_path}")
    except Exception as e:
        logger.warning(f"prefs load failed {e}")


def _save() -> None:
    try:
        data = {"langs": _user_lang, "tones": _user_tone}
        tmp = _prefs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_prefs_path)
    except Exception as e:
        logger.warning(f"prefs save failed {e}")


def init_prefs() -> None:
    with _lock:
        _load()


def get_user_lang(user_id: int) -> Optional[str]:
    return _user_lang.get(str(user_id))


def set_user_lang(user_id: int, lang: Optional[str]) -> None:
    with _lock:
        key = str(user_id)
        if lang is None or lang.lower() == "auto":
            _user_lang.pop(key, None)
        else:
            code = lang.lower().strip()
            if code not in WHISPER_LANGS:
                raise ValueError(f"Неизвестный язык '{code}'")
            _user_lang[key] = code
        _save()


def get_tone_enabled(user_id: int) -> bool:
    return _user_tone.get(str(user_id), False)


def set_tone_enabled(user_id: int, enabled: bool) -> None:
    with _lock:
        _user_tone[str(user_id)] = bool(enabled)
        _save()


def toggle_tone(user_id: int) -> bool:
    with _lock:
        key = str(user_id)
        new_val = not _user_tone.get(key, False)
        _user_tone[key] = new_val
        _save()
        return new_val


def is_valid_lang(code: str) -> bool:
    return code.lower() in WHISPER_LANGS


def lang_display(code: Optional[str]) -> str:
    if not code:
        return "авто (автоопределение)"
    name = LANG_NAMES.get(code, code)
    return f"{name} ({code})"


# auto-load on import for bot runtime
try:
    init_prefs()
except Exception:
    pass
