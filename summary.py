import logging
from typing import Optional

import config
import stt

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — суммаризатор русской и английской речи. Сократи до сути, но не до одного слова — оставь ~30-50% от оригинала, 3-5 предложений.\n"
    "Правила:\n"
    "- Сохраняй ключевые факты, имена, цифры, даты, маты дословно если важны для смысла.\n"
    "- Не добавляй выдумок, только из текста, не перефразируй в одно слово.\n"
    "- Сохраняй логику и последовательность, сокращай воду, но оставь суть.\n"
    "- Пунктуация по смыслу, верни только пересказ без заголовков.\n"
    "\n"
    "Примеры:\n"
    "Вход: Привет, как дела? Всё отлично, давно не виделись! Слушай, я хотел рассказать как мы ездили на море, там было классно, купались каждый день, ели шашлык, жарили на костре, потом вернулись домой уставшие но довольные. Короче, было круто, всем советую.\n"
    "Выход: Привет, давно не виделись. Ездили на море — каждый день купались, ели шашлык и жарили на костре. Вернулись домой уставшие, но довольные, всем советуют.\n"
)

async def summarize(text: str, lang: Optional[str] = None, model: Optional[str] = None) -> str:
    if not text or not text.strip():
        return text
    raw = stt.strip_header(text)
    if len(raw) < 300:
        return ""
    if len(raw) > 3500:
        raw = raw[:3500] + "..."
    lang_hint = f"\nЯзык: {lang}." if lang and lang != "auto" else ""
    try:
        resp, used = await stt.chat_with_fallback(
            messages=[{"role": "system", "content": SYSTEM_PROMPT + lang_hint}, {"role": "user", "content": raw}],
            temperature=0.3, max_tokens=700, model=model
        )
        out = resp.choices[0].message.content.strip() if resp.choices else ""
        if not out:
            return ""
        logger.info(f"summary {len(raw)}->{len(out)} via {used}")
        return out
    except Exception as e:
        logger.warning(f"summary failed {e}")
        return ""
