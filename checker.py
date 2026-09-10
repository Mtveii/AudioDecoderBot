import logging
from typing import Optional

import config
import stt

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — корректор транскрибации русской и английской речи.\n"
    "Исправь только явные ошибки распознавания, сохраняя смысл и стиль.\n"
    "Правила:\n"
    "- Сохраняй маты, ругательства, сленг ДОСЛОВНО — не цензурируй, не заменяй на *** и не смягчай.\n"
    "- Не перефразируй и не меняй порядок слов — только точечные исправления.\n"
    "- Исправь склейки и разделения: \"приветкакдела\" → \"привет, как дела\", \"в со\" → \"всё\".\n"
    "- Исправь нелогичные слова по контексту соседних слов; если слово похоже на опечатку — замени на словарное, но если это мат/сленг/имя/бренд — оставь.\n"
    "- Пунктуация и заглавные — минимально, по смыслу.\n"
    "- Верни только исправленный текст, без пояснений и кавычек.\n"
    "\n"
    "Примеры:\n"
    "Вход: привет как дила всо харашо блять\n"
    "Выход: Привет, как дела? Всё хорошо, блять.\n"
    "\n"
    "Вход: я пашёл нахуй отсюда кек\n"
    "Выход: Я пошёл нахуй отсюда, кек.\n"
    "\n"
    "Вход: это пиздец нах*й полный сука\n"
    "Выход: Это пиздец нах*й полный, сука.\n"
    "\n"
    "Вход: privet kak dela vsyo normalno\n"
    "Выход: Привет, как дела? Всё нормально.\n"
)


async def proofread(text: str, lang: Optional[str] = None, model: Optional[str] = None) -> str:
    """Проверяет логичность транскрибации. Сохраняет маты. Фолбэк — исходный текст."""
    if not text or not text.strip():
        return text

    # убираем служебные заголовки 📝 / 🎭 если вдруг передали целиком
    raw = text.strip()
    for prefix in ("📝 Текст:\n", "🎭 Текст с интонацией:\n", "✅ Проверено:\n"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].lstrip()

    # если текст без кириллицы/латиницы — нечего править
    if len(raw) < 3:
        return text

    client = stt.get_groq_client()
    chat_model = model or config.GROQ_CHAT_MODEL
    base_is_groq = True

    if not client or not config.GROQ_API_KEY:
        client = stt.get_openai_client()
        base_is_groq = False
        if not client:
            logger.warning("checker: no chat client, return raw")
            return text
        if chat_model.startswith("openai/") or "gpt-oss" in chat_model:
            chat_model = "gpt-4o-mini"

    # hint по языку — добавляем в промпт если знаем
    lang_hint = ""
    if lang and lang != "auto":
        lang_hint = f"\nЯзык текста: {lang}. Исправляй с учётом этого языка."

    truncated = raw.strip()
    if len(truncated) > 3500:
        truncated = truncated[:3500] + "..."

    try:
        resp = await client.chat.completions.create(
            model=chat_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + lang_hint},
                {"role": "user", "content": truncated},
            ],
            temperature=0.15,
            max_tokens=2048,
        )
        fixed = resp.choices[0].message.content.strip() if resp.choices else ""
        if not fixed:
            return text

        # защита от перефраза/галлюцинации: если длина сильно выросла — вернуть исходник
        if len(fixed) > len(raw) * 1.6:
            logger.warning(f"checker: too long {len(raw)}->{len(fixed)}, fallback raw")
            return text
        # если модель вернула кавычки-обёртку — снять
        if len(fixed) >= 2 and fixed[0] == fixed[-1] and fixed[0] in ('"', "'", "“", "«"):
            fixed = fixed[1:-1].strip()

        logger.info(f"checker {len(raw)}->{len(fixed)} via {chat_model} lang={lang or 'auto'}")
        return fixed
    except Exception as e:
        err = str(e).lower()
        logger.warning(f"checker failed {chat_model}: {e}")
        if base_is_groq and "model" in err and chat_model != "llama-3.1-8b-instant":
            try:
                fallback = "llama-3.1-8b-instant" if "gpt-oss" in chat_model else "openai/gpt-oss-20b"
                if fallback != chat_model:
                    resp = await client.chat.completions.create(
                        model=fallback,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT + lang_hint},
                            {"role": "user", "content": truncated},
                        ],
                        temperature=0.15,
                        max_tokens=2048,
                    )
                    fixed = resp.choices[0].message.content.strip() if resp.choices else ""
                    if fixed:
                        return fixed
            except Exception as e2:
                logger.warning(f"checker fallback failed {e2}")
        return text
