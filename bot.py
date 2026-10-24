import asyncio
import datetime
import logging
import os
import re
import shutil
import time
import tempfile
from pathlib import Path
from collections import OrderedDict, deque
from typing import Optional

BOT_START_TIME = datetime.datetime.now()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommandScopeChat, BotCommandScopeDefault, FSInputFile

import checker as checker_module
import config
import prefs
import stt
import summary as summary_module
import tone as tone_module
try:
    import stats  # приватно, игнор via .gitignore
except Exception as e:
    stats = None  # type: ignore
    logging.getLogger(__name__).warning(f"stats import failed {e} — админка отключена")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Приветствие
START_TEXT = (
    "👋 Привет! Я превращаю голосовые, кружки и видео в текст.\n\n"
    "Просто пришли:\n"
    "• 🎤 голосовое сообщение\n"
    "• 🔵 кружок (video_note)\n"
    "• 🎬 видео / аудио / файл (mp3, m4a, wav, ogg)\n\n"
    "Я сразу верну текст. Без команд.\n\n"
    "🌍 Язык: по умолчанию авто. Напиши /ru, /en, /de — сразу перерасшифрует последнее ГС.\n"
    "🎭 Интонация: /ton — перерасшифрует с интонацией на 1 сообщение.\n"
    "✅ Проверка: /check — перепроверит логичность, маты оставлю.\n"
    "📌 Пересказ: /summary — кратко 3-5 предл. для длинных.\n\n"
    "Подробнее: /help  •  /about — GitHub и если хочешь поддержать ☕"
)

def get_help_text(user_id: Optional[int] = None) -> str:
    mode = config.get_effective_mode()
    mode_name = {"groq": "Groq (быстро, бесплатно)", "api": "OpenAI API", "local": "локальная модель"}.get(mode, mode)
    lang_code = prefs.get_user_lang(user_id) if user_id else None
    lang_disp = prefs.lang_display(lang_code)
    return (
        "ℹ️ Как пользоваться:\n"
        "1. Пришли голосовое, кружок или видео как обычно\n"
        "2. Подожди 2-5 сек — я пришлю текст\n\n"
        f"Лимиты: файл до 20МБ, длительность до ~10-15 мин оптимально.\n"
        f"Текущий STT режим: `{mode}` ({mode_name})\n"
        f"Твой язык: `{lang_disp}`\n\n"
        "Команды:\n"
        "• /start — приветствие\n"
        "• /help — эта справка\n"
        "• /about — о проекте, GitHub, донаты\n"
        "• /ru /en /de /fr /es /ja /zh ... — любой язык (99 шт.), сразу перерасшифрует последнее ГС или то, на которое ответил\n"
        "• /auto — сбросить в авто + перерасшифровать\n"
        "• /ton — перерасшифрует с интонацией на 1 сообщение (ответь на ГС или просто /ton для последнего)\n"
        "• /check — перепроверить логичность (маты сохраню, только после) — тот же принцип: ответом на своё ГС/ВС или на сообщение бота, иначе последнее\n"
        "• /summary — краткий пересказ (от 1 мин)\n\n"
        "—\nБесплатно, открыто: /about — GitHub, если захочешь поддержать ☕"
    )

# Ограничитель параллельных транскрибаций чтобы не забить CPU (i3-1315U 8 потоков)
transcribe_semaphore = asyncio.Semaphore(2)
# Антиспам: 5 ГС/мин на юзера
_user_times: dict[int, list[float]] = {}

# Прекомпилированные регэкспы — оптимизация (не компилировать на каждое сообщение)
LANG_RE = re.compile(r"^/([a-z]{2,3})(?:@\w+)?\s*$")

_MAX_CHAT_CACHE = 500
_MAX_MEDIA_TEXT_CACHE = 1000

# Метрики скорости ответа (в памяти процесса)
_response_times = deque(maxlen=50)   # секунды обработки последних запросов
_request_errors = 0
_last_request_at = None
_active_requests = 0


class LRUDict(OrderedDict):
    """OrderedDict с LRU-вытеснением по maxsize и move_to_end при get."""
    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self.maxsize = maxsize

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
        return super().get(key, default)

    def __getitem__(self, key):
        val = super().__getitem__(key)
        self.move_to_end(key)
        return val

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)


# Кэш последнего медиа по чату — для /ton и /ru без реплая
last_media_by_chat = LRUDict(_MAX_CHAT_CACHE)
# Кэш последнего текста бота — для /check (только после, без Было)
last_text_by_chat = LRUDict(_MAX_CHAT_CACHE)
# Кэш текст по ID исходного ГС/ВС — для /check ответом на своё ГС
last_text_by_media_id = LRUDict(_MAX_MEDIA_TEXT_CACHE)
# Кэш msg_id бота -> текст (для кнопок под сообщением)
last_text_by_msg_id = LRUDict(1000)
# Кэш msg_id бота -> file_id/lang для повторных действий кнопками
last_file_by_msg_id = LRUDict(1000)
last_lang_by_msg_id = LRUDict(1000)
# Кэш msg_id бота -> author user_id (защита от IDOR в act:* кнопках)
last_author_by_msg_id = LRUDict(1000)

# Поддерживаемые расширения для document
AUDIO_DOC_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".flac", ".aac", ".wma", ".opus", ".mp4", ".mov", ".avi", ".mkv"}

def is_audio_document(message: Message) -> bool:
    if not message.document:
        return False
    ext = Path(message.document.file_name or "").suffix.lower()
    mime = (message.document.mime_type or "").lower()
    if ext in AUDIO_DOC_EXTS:
        return True
    if mime.startswith("audio/") or mime.startswith("video/"):
        return True
    return False

def has_media(message: Optional[Message]) -> bool:
    if not message:
        return False
    return bool(message.voice or message.video_note or message.video or message.audio or is_audio_document(message))

def _extract_lang_code(text: str) -> Optional[str]:
    """Вытаскивает код языка из /<code>[@bot] — используется в хэндлерах"""
    m = LANG_RE.match(text.strip().lower())
    return m.group(1) if m else None


def _get_target_media(message: Message) -> Optional[Message]:
    """Общий хелпер: приоритет — реплайнутое ГС, иначе последнее в чате (как /ru /ton /check)"""
    if message.reply_to_message and has_media(message.reply_to_message):
        return message.reply_to_message
    return last_media_by_chat.get(message.chat.id)


def _cache_text(chat_id: int, media_msg_id: Optional[int], text: str, msg_id: Optional[int] = None, file_id: Optional[str] = None, lang: Optional[str] = None, author_id: Optional[int] = None) -> None:
    """Кэширует текст с ограничением размера (LRU-подобно через удаление старых)"""
    clean = text.strip()
    last_text_by_chat[chat_id] = clean
    if media_msg_id is not None:
        last_text_by_media_id[media_msg_id] = clean
        if len(last_text_by_media_id) > _MAX_MEDIA_TEXT_CACHE:
            oldest = next(iter(last_text_by_media_id))
            last_text_by_media_id.pop(oldest, None)
    if msg_id is not None:
        last_text_by_msg_id[msg_id] = clean
        if file_id is not None:
            last_file_by_msg_id[msg_id] = file_id
        if lang is not None:
            last_lang_by_msg_id[msg_id] = lang
        if author_id is not None:
            last_author_by_msg_id[msg_id] = author_id
        # лимит msg кэша
        if len(last_text_by_msg_id) > 1000:
            oldest = next(iter(last_text_by_msg_id))
            last_text_by_msg_id.pop(oldest, None)
            last_file_by_msg_id.pop(oldest, None)
            last_lang_by_msg_id.pop(oldest, None)
            last_author_by_msg_id.pop(oldest, None)
    if len(last_text_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_text_by_chat))
        last_text_by_chat.pop(oldest, None)
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)

def _save_media_copy(input_path: str, user_id: int, message_id: int, media_type: str) -> Optional[str]:
    """Сохраняет копию ГС/ВС на диск в media/YYYY-MM-DD/ — только для тебя, приватно"""
    try:
        # определяем расширение по типу
        ext_map = {"голосовое": "ogg", "кружок": "mp4", "видео": "mp4", "аудио": "mp3", "файл": "ogg"}
        ext = ext_map.get(media_type, "ogg")
        import datetime as _dt
        # имя: media/2025-09-11/12345_67890_voice.ogg
        date_str = _dt.datetime.now().strftime("%Y-%m-%d")
        media_dir = Path(config.MEDIA_DIR) / date_str
        media_dir.mkdir(parents=True, exist_ok=True)
        # безопасное имя
        fname = f"{user_id}_{message_id}_{media_type}.{ext}".replace(" ", "_")
        dest = media_dir / fname
        shutil.copy(input_path, dest)
        return str(dest)
    except Exception as e:
        logger.warning(f"media save failed {e}")
        return None

def _is_rate_limited(user_id: int) -> bool:
    import time
    now = time.time()
    lst = _user_times.get(user_id, [])
    # чистим окно 60с
    lst = [t for t in lst if now - t < 60]
    if len(lst) >= config.RATE_LIMIT_PER_MIN:
        _user_times[user_id] = lst
        return True
    lst.append(now)
    _user_times[user_id] = lst
    return False

# Донаты — значения берутся из config (env DONATE_CARD / DONATE_URL), без хардкода секретов
CARD_NUMBER = config.DONATE_CARD
MONOBANK_URL = config.DONATE_URL


def _build_donate_kb() -> InlineKeyboardMarkup:
    rows = []
    if MONOBANK_URL:
        rows.append(InlineKeyboardButton(text="☕ Monobank", url=MONOBANK_URL))
    # callback-кнопка всегда доступна (номер показывается через alert)
    rows.append(InlineKeyboardButton(text="💳 Показать номер карты", callback_data="donate:show_card"))
    return InlineKeyboardMarkup(inline_keyboard=[rows])


# Клавиатура доната: Monobank URL + кнопка показать номер карты (callback alert)
DONATE_KB = _build_donate_kb()

# Текст для rate limit и суточных лимитов
RATE_LIMIT_TEXT = (
    "⏳ Слишком часто, подожди 1 мин (лимит {limit}/мин)\n\n"
    "Если бот полезен — поддержи разработку:\n"
    "💳 Карта: <code>{card}</code>\n"
    "☕ Monobank: {url}"
)

DAILY_LIMIT_TEXT = (
    "—\n"
    "Если бот полезен — поддержи разработку:\n"
    "💳 Карта: <code>{card}</code>\n"
    "☕ Monobank: {url}"
)

# Суточные вехи и исключения — из config (env SUPPORT_MILESTONES / DONATE_EXCLUDED_IDS)
SUPPORT_MILESTONES = config.SUPPORT_MILESTONES
# Исключения из донат-сообщений
DONATE_EXCLUDED_USERS = config.DONATE_EXCLUDED_USERS

async def _check_rate_limit(message) -> bool:
    """Проверяет rate limit. Если превышен — отправляет сообщение с донатом и возвращает True (заблокировано)."""
    from aiogram.types import CallbackQuery as _CQ

    if isinstance(message, _CQ):
        user_id = message.from_user.id if message.from_user else 0
        if stats and stats.is_blocked(user_id):
            try:
                await message.answer("⛔ Вы заблокированы", show_alert=True)
            except Exception:
                pass
            return True
        if user_id in DONATE_EXCLUDED_USERS:
            return False
        if _is_rate_limited(user_id):
            try:
                await message.answer("⏳ Слишком часто, подожди 1 мин", show_alert=True)
            except Exception:
                pass
            return True
        return False
    user_id = message.from_user.id if message.from_user else 0
    if stats and stats.is_blocked(user_id):
        try:
            await message.answer("⛔ Вы заблокированы")
        except Exception:
            pass
        return True
    if user_id in DONATE_EXCLUDED_USERS:
        return False
    if _is_rate_limited(user_id):
        limit = config.RATE_LIMIT_PER_MIN
        card = CARD_NUMBER or "—"
        url = MONOBANK_URL or "—"
        text = RATE_LIMIT_TEXT.format(limit=limit, card=card, url=url)
        await message.answer(text, parse_mode="HTML", reply_markup=DONATE_KB)
        return True
    return False

async def _maybe_send_daily_support(chat_id: int, user_id: int) -> None:
    """Если пользователь попал в суточную веху — шлёт отдельное сообщение с донатом."""
    if not stats or user_id <= 0 or user_id in DONATE_EXCLUDED_USERS:
        return
    try:
        count = await stats.get_daily_count_async(user_id)
        if count in SUPPORT_MILESTONES:
            text = DAILY_LIMIT_TEXT.format(card=CARD_NUMBER or "—", url=MONOBANK_URL or "—")
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=DONATE_KB)
    except Exception:
        pass

# ---------- Автоочистка старых медиа (только админ / фон) ----------
async def _cleanup_media(days: int = config.MEDIA_RETENTION_DAYS) -> int:
    import time
    cutoff = time.time() - days * 86400
    root = Path(config.MEDIA_DIR)
    removed = 0
    if root.exists():
        for p in root.rglob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
        for p in sorted(root.rglob("*"), reverse=True):
            if p.is_dir():
                try:
                    p.rmdir()
                except Exception:
                    pass
    return removed


async def cmd_cleanup(message: Message):  # только для админки, без публичной команды
    if not config.is_admin(message.from_user.id if message.from_user else 0):
        return
    parts = (message.text or "").split()
    days = config.MEDIA_RETENTION_DAYS
    if len(parts) > 1 and parts[1].isdigit():
        days = max(1, int(parts[1]))
    removed = await _cleanup_media(days)
    await message.answer(f"🧹 Удалено файлов старше {days} дней: {removed}")


async def _cleanup_loop():
    while True:
        try:
            await _cleanup_media(config.MEDIA_RETENTION_DAYS)
        except Exception:
            pass
        await asyncio.sleep(86400)


# Callback для показа номера карты
@dp.callback_query(F.data == "donate:show_card")
async def donate_show_card(cb: CallbackQuery):
    card = CARD_NUMBER or "не указана (см. Monobank)"
    await cb.answer(f"Номер карты: {card}\nНажми и держи для копирования", show_alert=True)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if await _check_rate_limit(message):
        return
    await message.answer(START_TEXT)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await _check_rate_limit(message):
        return
    uid = message.from_user.id if message.from_user else None
    await message.answer(get_help_text(uid), parse_mode="Markdown")

@dp.message(Command("about"))
async def cmd_about(message: Message):
    if await _check_rate_limit(message):
        return
    about_rows = [InlineKeyboardButton(text="📦 GitHub", url="https://github.com/Mtveii/AudioDecoderBot")]
    if MONOBANK_URL:
        about_rows.append(InlineKeyboardButton(text="☕ Поддержать (Monobank)", url=MONOBANK_URL))
    kb = InlineKeyboardMarkup(inline_keyboard=[about_rows])
    await message.answer(
        "🤖 <b>AudioDecoderBot</b> — превращает голосовые, кружки и видео в текст.\n\n"
        "🔧 <b>Технологии:</b>\n"
        "• STT: Groq Whisper Large v3 Turbo (бесплатно, быстро) + fallback на локальную faster-whisper\n"
        "• LLM пост-обработка: Groq → Cerebras → Mistral → OpenAI (авто-фейловер)\n"
        "• 99 языков Whisper, автоопределение\n\n"
        "✨ <b>Функции:</b>\n"
        "• /ru /en /de... — язык на 1 сообщение (реплей или последнее)\n"
        "• /ton — интонация [спокойный] [радостный] [сарказм]...\n"
        "• /check — правка логики, маты оставляю\n"
        "• /summary — краткий пересказ 3-5 предл. (от 1 мин)\n"
        "• Кнопки под каждым результатом — без ввода команд\n\n"
        "🆓 Бесплатно, открыто, без слежки.\n"
        "Если проект полезен — можно поддержать разработку ☕ (полностью добровольно)",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True
    )

# ---------- Language handlers ----------

@dp.message(Command("auto"))
async def cmd_auto(message: Message):
    if await _check_rate_limit(message):
        return
    uid = message.from_user.id
    prefs.set_user_lang(uid, None)
    await message.answer("✅ Язык сброшен в автоопределение (auto).")
    target = _get_target_media(message)
    if target:
        hint = "то аудио, на которое ты ответил, " if target == message.reply_to_message else "последнее аудио "
        await message.answer(f"🔄 Перерасшифровываю {hint}с автоопределением...")
        await process_transcription(target, lang_override=None, reply_to_message=message)

# ---------- Tone handler ----------
@dp.message(Command("ton"))
async def cmd_ton(message: Message):
    if await _check_rate_limit(message):
        return
    target = _get_target_media(message)
    if target:
        await process_transcription(target, force_tone=True, reply_to_message=message)
        return
    await message.answer("❌ Нет аудио для тон. Пришли ГС/ВС или ответь на ГС командой /ton — покажу с метками [спокойный тон] на 1 сообщение.")

# Динамический хэндлер для /ru /en /ja /de ... любой ISO код
@dp.message(lambda m: bool(m.text and _extract_lang_code(m.text)))
async def cmd_dynamic_lang(message: Message):
    if await _check_rate_limit(message):
        return
    code = _extract_lang_code(message.text or "")
    if not code:
        return
    # не перехватывать зарезервированные команды (ton, lang, auto уже выше, но на всякий)
    if code in prefs.RESERVED_CODES:
        return
    # если не валидный язык — игнорируем (пусть fallback сработает)
    if not prefs.is_valid_lang(code):
        return
    # это точно язык — ставим
    uid = message.from_user.id
    prefs.set_user_lang(uid, code)
    await message.answer(f"✅ Язык установлен: {prefs.lang_display(code)}\nСбросить: /auto")
    # и сразу перерасшифровываем: тот же принцип что и /ton
    target = _get_target_media(message)
    if target:
        reply_hint = "то аудио, на которое ты ответил, " if target == message.reply_to_message else "последнее аудио "
        await message.answer(f"🔄 Перерасшифровываю {reply_hint}с языком {code}...")
        await process_transcription(target, lang_override=code, reply_to_message=message)

# ---------- Checker handler ----------
@dp.message(Command("check"))
async def cmd_check(message: Message):
    if await _check_rate_limit(message):
        return
    # ручная перепроверка логичности, только после (БЫЛО не показываем)
    target_text = None
    lang_hint = None

    # 1. если ответ на сообщение бота с текстом — берём его
    if message.reply_to_message:
        rt = message.reply_to_message
        # текст бота может быть в rt.text (📝/🎭/✅)
        if rt.text and ("📝 Текст:" in rt.text or "🎭 Текст" in rt.text or "✅ Проверено:" in rt.text):
            # вырезаем заголовок
            target_text = stt.strip_header(rt.text)
            # язык — из prefs автора команды
            lang_hint = prefs.get_user_lang(message.from_user.id) if message.from_user else None
        # если ответ на своё ГС/ВС — берём текст именно этого ГС (тот же принцип что и /ru /ton)
        elif has_media(rt):
            # приоритет: текст именно этого медиа по ID, иначе последний в чате
            target_text = last_text_by_media_id.get(rt.message_id)
            if not target_text:
                target_text = last_text_by_chat.get(message.chat.id)
            if target_text:
                lang_hint = prefs.get_user_lang(message.from_user.id) if message.from_user else None

    # 2. иначе — последний текст бота в чате (без реплая — как /ru /ton берут последнее ГС)
    if not target_text:
        target_text = last_text_by_chat.get(message.chat.id)

    if not target_text or not target_text.strip():
        await message.answer("❌ Нет текста для проверки. Сначала пришли ГС/ВС, я его расшифрую, потом напиши /check или ответь на моё сообщение с текстом командой /check.")
        return

    # не проверяем слишком короткий
    if len(target_text.strip()) < 5:
        await message.answer("❌ Текст слишком короткий для проверки.")
        return

    status = await (message.reply_to_message or message).reply("🔍 Проверяю логичность...")

    # язык для чекера — из prefs
    if not lang_hint and message.from_user:
        lang_hint = prefs.get_user_lang(message.from_user.id)

    try:
        fixed = await checker_module.proofread(target_text, lang=lang_hint)
    except Exception as e:
        logger.exception("check failed")
        await status.edit_text(f"❌ Ошибка проверки: {e}")
        return

    # только после — без Было
    # если изменений нет — скажем уже логично
    if fixed.strip() == target_text.strip():
        await status.edit_text("✅ Текст уже логичен, исправлений не нужно.")
        return

    # иначе — только исправленный
    header = "✅ Проверено:\n"
    full = header + fixed.strip()
    try:
        if len(full) <= 4096:
            await status.edit_text(full)
        else:
            await status.edit_text(full[:4096])
            for i in range(4096, len(full), 4096):
                await (message.reply_to_message or message).reply(full[i:i+4096])
                await asyncio.sleep(0.2)
        _cache_text(message.chat.id, None, fixed.strip(),
                    author_id=message.from_user.id if message.from_user else None)
        logger.info(f"check {len(target_text)}->{len(fixed)} lang={lang_hint or 'auto'}")
    except Exception as e:
        await status.edit_text(f"❌ Не удалось отправить: {e}")

def _result_kb(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\U0001f3ad Тон", callback_data=f"act:ton:{msg_id}"),
            InlineKeyboardButton(text="\u2705 Check", callback_data=f"act:check:{msg_id}"),
        ],
        [
            InlineKeyboardButton(text="\U0001f4dd Summary", callback_data=f"act:sum:{msg_id}"),
            InlineKeyboardButton(text="\U0001f30d EN", callback_data=f"act:lang:{msg_id}:en"),
        ],
    ])


def _check_act_owner(cb: CallbackQuery, msg_id: int) -> bool:
    """Проверка автора для act:* кнопок (защита от IDOR/накрутки LLM).
    Возвращает True если действие разрешено, иначе False (ответ уже отправлен)."""
    clicker = cb.from_user.id if cb.from_user else 0
    author = last_author_by_msg_id.get(msg_id)
    if author is not None:
        if clicker != author and not config.is_admin(clicker):
            return False
        return True
    # автора определить нельзя (старый кэш): в личке разрешаем свой msg_id,
    # в группах/каналах чужой отклоняем
    try:
        if cb.message and cb.message.chat.type == "private":
            return True
    except Exception:
        pass
    return False


@dp.callback_query(F.data.startswith("act:"))
async def act_callback(cb: CallbackQuery):
    # rate-limit против накрутки LLM
    if await _check_rate_limit(cb):
        return
    data = cb.data or ""
    await cb.answer()
    try:
        # извлекаем msg_id
        parts = data.split(":")
        if len(parts) < 3:
            await cb.answer("Неверные данные", show_alert=True)
            return
        action = parts[1]
        try:
            msg_id = int(parts[2])
        except Exception:
            await cb.answer("Неверный ID", show_alert=True)
            return
        if not _check_act_owner(cb, msg_id):
            await cb.answer("Это не твоё сообщение", show_alert=True)
            return
        orig_text = last_text_by_msg_id.get(msg_id)
        if not orig_text:
            await cb.answer("Текст уже устарел, пришли ГС заново", show_alert=True)
            return
        # убираем заголовки для обработки
        clean = stt.strip_header(orig_text)
        if action == "ton":
            await cb.message.edit_text(f"🎭 Обрабатываю...", reply_markup=None)
            annotated = await tone_module.annotate_tone(clean)
            if not annotated or "[" not in annotated:
                await cb.answer("Не удалось", show_alert=True)
                await cb.message.edit_text(f"📝 Текст:\n{clean}", reply_markup=_result_kb(msg_id))
                return
            header = "🎭 Текст с интонацией:\n"
            await cb.message.edit_text(header + annotated, reply_markup=_result_kb(msg_id))
            # кэш
            _cache_text(cb.message.chat.id, None, annotated, msg_id=cb.message.message_id,
                        author_id=cb.from_user.id if cb.from_user else None)
        elif action == "check":
            await cb.message.edit_text(f"🔍 Проверяю...", reply_markup=None)
            lang = last_lang_by_msg_id.get(msg_id)
            fixed = await checker_module.proofread(clean, lang=lang)
            if not fixed or fixed.strip() == clean.strip():
                await cb.message.edit_text(f"✅ Уже логично:\n{clean}", reply_markup=_result_kb(msg_id))
                return
            await cb.message.edit_text(f"✅ Проверено:\n{fixed}", reply_markup=_result_kb(msg_id))
            _cache_text(cb.message.chat.id, None, fixed, msg_id=cb.message.message_id,
                        author_id=cb.from_user.id if cb.from_user else None)
        elif action == "sum":
            if len(clean) < 300:
                await cb.answer("Слишком коротко (нужно от 1 мин)", show_alert=True)
                return
            await cb.message.edit_text(f"📌 Сжимаю...", reply_markup=None)
            out = await summary_module.summarize(clean)
            if not out:
                await cb.message.edit_text(f"❌ Не удалось сжать", reply_markup=_result_kb(msg_id))
                return
            await cb.message.edit_text(f"📌 Кратко:\n{out}", reply_markup=_result_kb(msg_id))
        elif action == "lang":
            if len(parts) < 4:
                await cb.answer("Нет языка", show_alert=True)
                return
            lang_code = parts[3]
            file_id = last_file_by_msg_id.get(msg_id)
            if not file_id:
                await cb.answer("Файл уже устарел, нужен повторный ГС", show_alert=True)
                return
            # скачиваем по file_id и перерасшифровываем
            await cb.message.edit_text(f"🌍 Перерасшифровываю как {lang_code}...", reply_markup=None)
            # создаем временный файл
            tmpdir = tempfile.mkdtemp(prefix="act_lang_")
            tmp_path = str(Path(tmpdir) / "audio.ogg")
            try:
                tg_file = await bot.get_file(file_id)
                await bot.download_file(tg_file.file_path, destination=tmp_path)
                # лимит 20MB как в process_transcription
                try:
                    size_mb = Path(tmp_path).stat().st_size / 1024 / 1024
                    if size_mb > config.MAX_FILE_SIZE_MB:
                        await cb.message.edit_text(
                            f"❌ Файл слишком большой ({size_mb:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB",
                            reply_markup=_result_kb(msg_id),
                        )
                        return
                except Exception:
                    pass
                text = await stt.transcribe_file(tmp_path, lang_override=lang_code)
                if not text:
                    await cb.message.edit_text(f"❌ Пусто после {lang_code}", reply_markup=_result_kb(msg_id))
                    return
                await cb.message.edit_text(f"📝 Текст ({lang_code}):\n{text}", reply_markup=_result_kb(cb.message.message_id))
                _cache_text(cb.message.chat.id, None, text, msg_id=cb.message.message_id, file_id=file_id, lang=lang_code,
                            author_id=cb.from_user.id if cb.from_user else None)
            except Exception as e:
                logger.exception(f"act lang failed {e}")
                await cb.message.edit_text(f"❌ Ошибка {lang_code}: {e}", reply_markup=_result_kb(msg_id))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            await cb.answer("Неизвестно", show_alert=True)
    except Exception as e:
        logger.exception(f"act cb failed {e}")
        try:
            await cb.message.answer(f"❌ Ошибка: {e}")
        except Exception:
            pass

@dp.message(Command("summary", "sum"))
async def cmd_summary(message: Message):
    if await _check_rate_limit(message):
        return
    target_text = None
    lang_hint = None
    if message.reply_to_message:
        rt = message.reply_to_message
        if rt.text and ("📝 Текст:" in rt.text or "🎭 Текст" in rt.text or "✅ Проверено:" in rt.text or "📌 Кратко:" in rt.text):
            target_text = stt.strip_header(rt.text)
            lang_hint = prefs.get_user_lang(message.from_user.id) if message.from_user else None
        elif has_media(rt):
            target_text = last_text_by_media_id.get(rt.message_id)
            if not target_text:
                target_text = last_text_by_chat.get(message.chat.id)
    if not target_text:
        target_text = last_text_by_chat.get(message.chat.id)
    if not target_text or len(target_text.strip()) < 300:
        await message.answer("❌ Слишком коротко (нужно от 1 мин). Пришли ГС/ВС от 1 мин.")
        return
    status = await (message.reply_to_message or message).reply("📌 Сжимаю...")
    if not lang_hint and message.from_user:
        lang_hint = prefs.get_user_lang(message.from_user.id)
    try:
        out = await summary_module.summarize(target_text, lang=lang_hint)
        if not out:
            await status.edit_text("❌ Не удалось сжать")
            return
        header = "📌 Кратко:\n"
        full = header + out.strip()
        if len(full) <= 4096:
            await status.edit_text(full)
        else:
            await status.edit_text(full[:4096])
            for i in range(4096, len(full), 4096):
                await (message.reply_to_message or message).reply(full[i:i+4096])
        logger.info(f"summary {len(target_text)}->{len(out)}")
    except Exception as e:
        logger.exception(f"summary failed {e}")
        await status.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice | F.video_note | F.video | F.audio)
async def handle_media(message: Message):
    if await _check_rate_limit(message):
        return
    # кэшируем для /ton и /ru без реплая с лимитом
    last_media_by_chat[message.chat.id] = message
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)
    await process_transcription(message)

@dp.message(lambda m: is_audio_document(m))
async def handle_document(message: Message):
    if await _check_rate_limit(message):
        return
    last_media_by_chat[message.chat.id] = message
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)
    await process_transcription(message)

async def process_transcription(message: Message, lang_override: Optional[str] = None, force_tone: bool = False, reply_to_message: Optional[Message] = None):
    """Скачивает файл, транскрибирует, отвечает. lang_override=None -> берёт из prefs/auto."""
    global _last_request_at, _active_requests
    _active_requests += 1
    _t0 = time.time()
    _last_request_at = datetime.datetime.now()

    # определяем file_id и имя
    file_id = None
    file_name_hint = "audio.ogg"
    description = "файл"

    if message.voice:
        file_id = message.voice.file_id
        file_name_hint = "voice.ogg"
        description = "голосовое"
        # лимит Bot API 20MB, voice обычно <1MB
        if message.voice.file_size and message.voice.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            target = reply_to_message or message
            await target.reply(f"❌ Файл слишком большой ({message.voice.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB")
            globals()["_active_requests"] = max(0, globals().get("_active_requests", 0) - 1)
            return
    elif message.video_note:
        file_id = message.video_note.file_id
        file_name_hint = "video_note.mp4"
        description = "кружок"
    elif message.video:
        file_id = message.video.file_id
        file_name_hint = "video.mp4"
        description = "видео"
        if message.video.file_size and message.video.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            target = reply_to_message or message
            await target.reply(f"❌ Видео слишком большое ({message.video.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB, попробуйте короче")
            return
    elif message.audio:
        file_id = message.audio.file_id
        file_name_hint = message.audio.file_name or "audio.mp3"
        description = "аудио"
    elif message.document:
        file_id = message.document.file_id
        file_name_hint = message.document.file_name or "document.mp3"
        description = "файл"
        if message.document.file_size and message.document.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            target = reply_to_message or message
            await target.reply(f"❌ Файл слишком большой ({message.document.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB")
            globals()["_active_requests"] = max(0, globals().get("_active_requests", 0) - 1)
            return
    else:
        target = reply_to_message or message
        await target.reply("❌ Неизвестный тип файла")
        return

    # per-user настройки — lang_override имеет приоритет (для /ru реплея последнего)
    lang_uid = reply_to_message.from_user.id if reply_to_message and reply_to_message.from_user else (message.from_user.id if message.from_user else None)
    # если передан явный lang_override (из /ru перерасшифровки) — используем его, иначе из prefs
    if lang_override is not None:
        eff_lang = lang_override  # может быть None для /auto
    else:
        eff_lang = prefs.get_user_lang(lang_uid) if lang_uid else None
    tone_enabled = force_tone  # только на 1 сообщение, авто-тон убран

    # лог
    if eff_lang:
        logger.info(f"user {lang_uid} lang={eff_lang} tone={tone_enabled} force_tone={force_tone}")
    elif tone_enabled:
        logger.info(f"user {lang_uid} lang=auto tone={tone_enabled}")

    # статус сообщение — отвечаем на оригинал или на команду
    status_target = reply_to_message or message
    status_msg = await status_target.reply(f"⏳ Распознаю {description}...")

    tmpdir = tempfile.mkdtemp(prefix="tg_stt_")
    input_path = str(Path(tmpdir) / file_name_hint)

    try:
        # 1. Скачать
        try:
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=input_path)
        except Exception as e:
            logger.exception("download failed")
            await status_msg.edit_text(f"❌ Не удалось скачать файл: {e}")
            return

        # проверка размера после скачки
        size_mb = Path(input_path).stat().st_size / 1024 / 1024
        file_size_bytes = Path(input_path).stat().st_size
        logger.info(f"downloaded {description} {size_mb:.2f}MB -> {input_path} lang={eff_lang or 'auto'} tone={tone_enabled}")
        # --- Слежка: сохраняем оригинал на диск (только для тебя, приватно, чистишь сам) ---
        saved_path = None
        try:
            loop = asyncio.get_running_loop()
            saved_path = await loop.run_in_executor(None, _save_media_copy, input_path, lang_uid or message.from_user.id or 0, message.message_id, description)
            if saved_path:
                logger.info(f"media saved {saved_path}")
        except Exception as e:
            logger.warning(f"media save failed {e}")

        # 2. Длительность (инфо)
        duration = await stt.get_audio_duration(input_path)
        if duration:
            logger.info(f"duration {duration:.1f}s")
            if duration > 3600:
                await status_msg.edit_text("❌ Слишком длинное аудио (>60 мин). Пришлите короче")
                return

        # 3. Транскрибация — кэш по file_id + verbose для длинных + AUTO_PROOF
        try:
            cached = None
            try:
                if stats and file_id:
                    loop = asyncio.get_running_loop()
                    cached = await loop.run_in_executor(None, stats.get_cached, file_id, eff_lang)
            except Exception:
                pass
            if cached:
                logger.info(f"cache hit {file_id} lang={eff_lang}")
                text = cached
            else:
                use_verbose = bool(duration and duration > 150)
                async with transcribe_semaphore:
                    if use_verbose:
                        try:
                            if config.get_effective_mode() == "groq":
                                text = await stt.transcribe_groq_verbose(input_path, lang_override=eff_lang)
                            else:
                                text = await stt.transcribe_file(input_path, lang_override=eff_lang)
                        except Exception as e:
                            logger.warning(f"verbose failed {e}, fallback normal")
                            text = await stt.transcribe_file(input_path, lang_override=eff_lang)
                    else:
                        text = await stt.transcribe_file(input_path, lang_override=eff_lang)
                try:
                    if stats and file_id and text:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, stats.put_cached, file_id, eff_lang, text)
                except Exception:
                    pass
            # AUTO_PROOF
            if config.AUTO_PROOF and text and text.strip() and len(text.strip()) > 20:
                try:
                    await status_msg.edit_text(f"🔍 Автопроверка...")
                    fixed = await checker_module.proofread(text, lang=eff_lang)
                    if fixed and fixed.strip() and fixed.strip() != text.strip():
                        text = fixed
                except Exception as e:
                    logger.warning(f"auto proof failed {e}")
            # 3b. интонация
            if tone_enabled and text and text.strip():
                try:
                    await status_msg.edit_text(f"🎭 Распознаю {description}... (+ интонация)")
                except Exception:
                    pass
                annotated = await tone_module.annotate_tone(text)
                if annotated and annotated.strip() and "[" in annotated:
                    text = annotated
                else:
                    logger.warning("tone annotate returned empty/invalid, using raw")
        except RuntimeError as e:
            # ffmpeg или модель не найдена
            logger.exception("transcribe failed")
            await status_msg.edit_text(f"❌ Ошибка распознавания: {e}")
            return
        except Exception as e:
            logger.exception("transcribe unexpected")
            # если это OpenAI rate limit / auth
            err_str = str(e).lower()
            if "auth" in err_str or "api_key" in err_str or "unauthorized" in err_str:
                await status_msg.edit_text("❌ Ошибка ключа OpenAI. Проверь OPENAI_API_KEY в .env или переключи на STT_MODE=local")
                return
            if "quota" in err_str or "insufficient" in err_str:
                await status_msg.edit_text("❌ Закончился баланс OpenAI API")
                return
            await status_msg.edit_text(f"❌ Не удалось распознать: {e}")
            return

        if not text or not text.strip():
            await status_msg.edit_text("🤷 Не удалось распознать речь — возможно, тишина или неразборчиво")
            return

        # 4. Ответ — текст + сохраняем reply на оригинал
        # Телеграм лимит 4096 на сообщение, режем если длиннее
        header = "🎭 Текст с интонацией:\n" if tone_enabled else "📝 Текст:\n"
        full = header + text.strip()
        kb = _result_kb(status_msg.message_id)
        if len(full) <= 4096:
            await status_msg.edit_text(full, reply_markup=kb)
        else:
            await status_msg.edit_text(full[:4096], reply_markup=kb)
            for i in range(4096, len(full), 4096):
                await status_target.reply(full[i:i+4096])
                await asyncio.sleep(0.3)  # анти-флуд

        # Суточная поддержка — отдельное сообщение после успешной транскрипции
        await _maybe_send_daily_support(status_target.chat.id, lang_uid or 0)

        _cache_text(status_target.chat.id, message.message_id, text.strip(), msg_id=status_msg.message_id, file_id=file_id, lang=eff_lang,
                    author_id=lang_uid or (message.from_user.id if message.from_user else None))
        # логируем для админки /admin (тип+время+превью) — не блокируем, admin
        try:
            if stats:
                # username/first_name берем из исходного сообщения
                uname = getattr(message.from_user, "username", None) if message.from_user else None
                fname = getattr(message.from_user, "first_name", None) if message.from_user else None
                await stats.log_event(
                    user_id=lang_uid or 0,
                    username=uname,
                    first_name=fname,
                    chat_id=status_target.chat.id,
                    chat_type=str(status_target.chat.type),
                    media_type=description,
                    lang_used=eff_lang,
                    tone_used=tone_enabled,
                    text=text.strip(),
                    file_id=file_id,
                    local_path=saved_path,
                    file_size=file_size_bytes if 'file_size_bytes' in locals() else None,
                    duration=duration if 'duration' in locals() else None,
                    tg_message_id=message.message_id,
                )
        except Exception as e:
            logger.warning(f"stats log failed {e}")
        logger.info(f"done {description} -> {len(text)} chars tone={tone_enabled} lang={eff_lang or 'auto'}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            _response_times.append(time.time() - _t0)
        except Exception:
            pass
        globals()["_active_requests"] = max(0, globals().get("_active_requests", 0) - 1)


async def _health_server():
    try:
        from aiohttp import web
        async def health(request):
            return web.Response(text="ok")
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "8080"))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"health server on :{port}")
    except Exception as e:
        logger.warning(f"health server failed: {e}")


async def main():
    try:
        config.validate_config()
    except ValueError as e:
        logger.error(str(e))
        print(f"\n❌ {e}\nСоздай .env из .env.example и укажи BOT_TOKEN\n")
        return

    # prefs уже загружены в prefs.py, но логируем
    try:
        langs = len(prefs._user_lang)  # type: ignore
        tones = sum(1 for v in prefs._user_tone.values() if v)  # type: ignore
        logger.info(f"Prefs loaded: {langs} langs, {tones} tones on from {config.USER_PREFS_PATH}")
    except Exception:
        pass
    # stats DB — приватно для /admin
    try:
        if stats:
            stats.init_db()
    except Exception as e:
        logger.warning(f"stats init failed {e}")

    mode = config.get_effective_mode()
    if mode == "groq":
        logger.info(f"Starting bot | mode={mode} groq_model={config.GROQ_MODEL} chat={config.GROQ_CHAT_MODEL} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    elif mode == "local":
        logger.info(f"Starting bot | mode={mode} model={config.WHISPER_MODEL} beam={config.WHISPER_BEAM_SIZE} threads={config.WHISPER_CPU_THREADS} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    else:
        logger.info(f"Starting bot | mode={mode} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    if not shutil.which("ffmpeg"):
        logger.error("ffmpeg не найден! Установи: sudo apt install ffmpeg")
        print("ffmpeg not found - install ffmpeg")
        return
    
    # предзагрузка модели в фоне чтобы первый ГС не ждал (безопасно, кэшируется)
    if mode == "local":
        try:
            logger.info(f"Preloading Whisper model {config.WHISPER_MODEL} ...")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, stt.get_local_model)
            logger.info("Model preloaded ✓")
        except Exception as e:
            logger.warning(f"Preload failed (скачается на первом ГС): {e}")
    
    # проверка что бот живой
    me = await bot.get_me()
    logger.info(f"Bot @{me.username} ({me.id}) started | mode={mode}")

    # команды в меню Telegram — нужные выше, языки внизу (/start скрыт — разовый)
    base_cmds = [
        BotCommand(command="help", description="Справка"),
        BotCommand(command="about", description="О проекте, GitHub, донаты"),
        BotCommand(command="summary", description="Краткий пересказ"),
        BotCommand(command="auto", description="Сбросить язык в авто"),
        BotCommand(command="ru", description="Язык: русский"),
        BotCommand(command="en", description="Language: English"),
        BotCommand(command="de", description="Sprache: Deutsch"),
        BotCommand(command="uk", description="Мова: українська"),
    ]
    # тебе — нужное выше, языки внизу (без дубля summary)
    admin_cmds = [
        BotCommand(command="status", description="🖥 Мониторинг сервера"),
        BotCommand(command="summary", description="Краткий пересказ"),
        BotCommand(command="check", description="Перепроверить логичность (маты сохраню)"),
        BotCommand(command="ton", description="Перерасшифровать с интонацией"),
        BotCommand(command="about", description="О проекте, GitHub, донаты"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="auto", description="Сбросить язык в авто"),
        BotCommand(command="ru", description="Язык: русский"),
        BotCommand(command="en", description="Language: English"),
        BotCommand(command="de", description="Sprache: Deutsch"),
        BotCommand(command="uk", description="Мова: українська"),
    ]
    try:
        # всем — база без новых приватных
        await bot.set_my_commands(base_cmds, scope=BotCommandScopeDefault())
        # тебе — всё + новые
        for aid in config.ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=int(aid)))
                logger.info(f"Admin menu set for {aid}")
            except Exception as e:
                logger.warning(f"admin menu for {aid} failed {e}")
    except Exception as e:
        logger.warning(f"set_my_commands failed {e}")

    # фоновая очистка старых медиа раз в сутки
    asyncio.create_task(_cleanup_loop())
    # health endpoint для Fly
    asyncio.create_task(_health_server())
    # polling с дропом pending апдейтов чтобы не обрабатывать старые ГС
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()

# Регистрация админ-панели (приватный модуль, в git не входит).
# ВАЖНО: запускать через run.py (import bot), иначе from bot import * в
# admin_panel создаст второй экземпляр dp и хэндлеры не сработают.
try:
    import admin_panel  # noqa: E402
    _adm = [h.callback.__name__ for h in dp.message.handlers]
    logger.info(f"admin_panel: {len(_adm)} message handlers")
except ImportError:
    logger.info("admin_panel не найден — админка отключена")

# catch-all на текст — РЕГИСТРИРУЕТСЯ ПОСЛЕДНИМ, иначе он съедает /admin и пр.
@dp.message(F.text & ~F.via_bot)
async def fallback_text(message: Message):
    # не спамим в группах на каждое сообщение
    if message.chat.type != "private":
        return
    # если текст команды уже обработан - сюда не попадем из-за порядка, но на всякий
    if message.text and message.text.startswith("/"):
        # не отвечать на /ru-подобные которые уже обработаны
        if _extract_lang_code(message.text):
            return
        await message.answer("Неизвестная команда. Попробуй /help, /ru, /ton, /check")
        return
    await message.answer("Пришли голосовое, кружок или видео — я превращу его в текст 🎤")

if __name__ == "__main__":
    asyncio.run(main())
