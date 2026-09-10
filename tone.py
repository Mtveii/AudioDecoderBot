import logging
from typing import Optional

import config
import stt

logger = logging.getLogger(__name__)

# Метки интонации — интуитивно понятны
TONE_LABELS = [
    "спокойный тон",
    "радостный",
    "грустный",
    "злой",
    "взволнованный",
    "шёпот",
    "крик",
    "сарказм/ирония",
    "уверенный",
    "сомневающийся",
]

SYSTEM_PROMPT = (
    "Ты — разметчик интонации русской/английской речи.\n"
    "Твоя задача: вернуть ИСХОДНЫЙ текст, вставляя перед КАЖДЫМ предложением одну метку из списка:\n"
    f"{', '.join(f'[{l}]' for l in TONE_LABELS)}\n"
    "Правила:\n"
    "- Не меняй слова, пунктуацию и порядок — только добавляй [метку] в начале предложения.\n"
    "- Одно предложение — одна метка. Если текст 1 предложение — одна метка в начале.\n"
    "- Если тон нейтральный/обычный — ставь [спокойный тон].\n"
    "- Шёпот/крик — только если явно тихая/громкая речь по смыслу.\n"
    "- Сарказм/ирония — если есть ирония, насмешка, противоположный смысл.\n"
    "- Верни только размеченный текст, без пояснений.\n"
    "\n"
    "Примеры:\n"
    "Вход: Привет, как дела? Всё отлично, давно не виделись!\n"
    "Выход: [спокойный тон] Привет, как дела? [радостный] Всё отлично, давно не виделись!\n"
    "\n"
    "Вход: Я же говорил не трогать мои вещи. Отлично, просто отлично, опять всё испортил.\n"
    "Выход: [злой] Я же говорил не трогать мои вещи. [сарказм/ирония] Отлично, просто отлично, опять всё испортил.\n"
    "\n"
    "Вход: эй, ты где? я уже час жду\n"
    "Выход: [взволнованный] эй, ты где? [грустный] я уже час жду\n"
)


async def annotate_tone(text: str, model: Optional[str] = None) -> str:
    """Добавляет метки интонации через Groq Chat. Фолбэк — исходный текст."""
    if not text or not text.strip():
        return text

    # выбираем клиент: Groq приоритет, иначе OpenAI
    client = stt.get_groq_client()
    chat_model = model or config.GROQ_CHAT_MODEL
    base_is_groq = True

    if not client or not config.GROQ_API_KEY:
        client = stt.get_openai_client()
        base_is_groq = False
        if not client:
            logger.warning("tone: no chat client (no GROQ/OPENAI key), returning raw text")
            return text
        # для OpenAI используем gpt-4o-mini если groq модель не подходит
        if chat_model.startswith("openai/") or "gpt-oss" in chat_model:
            chat_model = "gpt-4o-mini"

    # если текст слишком длинный — обрежем для LLM (4096 лимит телеграм всё равно)
    truncated = text.strip()
    if len(truncated) > 3000:
        truncated = truncated[:3000] + "..."

    try:
        resp = await client.chat.completions.create(
            model=chat_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": truncated},
            ],
            temperature=0.25,
            max_tokens=2048,
        )
        annotated = resp.choices[0].message.content.strip() if resp.choices else ""
        if not annotated:
            return text
        # базовая валидация: должен содержать хотя бы одну метку
        if "[" not in annotated or "]" not in annotated:
            logger.warning(f"tone: no labels in response, fallback to raw")
            return text
        logger.info(f"tone annotated {len(text)} -> {len(annotated)} chars via {chat_model}")
        return annotated
    except Exception as e:
        err = str(e).lower()
        # Groq rate limit / model not found — пробуем фолбэк модель
        logger.warning(f"tone failed with {chat_model}: {e}")
        if base_is_groq and "model" in err and chat_model != "llama-3.1-8b-instant":
            try:
                fallback = "llama-3.1-8b-instant" if "gpt-oss" in chat_model else "openai/gpt-oss-20b"
                if fallback == chat_model:
                    return text
                resp = await client.chat.completions.create(
                    model=fallback,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": truncated},
                    ],
                    temperature=0.25,
                    max_tokens=2048,
                )
                annotated = resp.choices[0].message.content.strip() if resp.choices else ""
                if annotated and "[" in annotated:
                    logger.info(f"tone fallback {fallback} ok")
                    return annotated
            except Exception as e2:
                logger.warning(f"tone fallback failed {e2}")
        # иначе возвращаем как есть, чтобы не ломать основной флоу
        return text
