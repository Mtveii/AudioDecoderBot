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
    """Добавляет метки интонации через Chat с фолбэком Groq->Cerebras->Mistral->OpenAI."""
    if not text or not text.strip():
        return text
    truncated = text.strip()
    if len(truncated) > 3000:
        truncated = truncated[:3000] + "..."
    try:
        resp, used = await stt.chat_with_fallback(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": truncated}],
            temperature=0.25, max_tokens=2048, model=model
        )
        annotated = resp.choices[0].message.content.strip() if resp.choices else ""
        if not annotated or "[" not in annotated or "]" not in annotated:
            logger.warning(f"tone: no labels via {used}")
            return text
        logger.info(f"tone {len(text)}->{len(annotated)} via {used}")
        return annotated
    except Exception as e:
        logger.warning(f"tone failed {e}")
        return text
