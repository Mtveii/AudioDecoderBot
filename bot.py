import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand

import checker as checker_module
import config
import prefs
import stt
import tone as tone_module

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
    "🎭 Интонация: /ton — сразу перерасшифрует последнее ГС с интонацией.\n"
    "✅ Проверка: /check — перепроверит логичность последнего текста, маты сохранит.\n"
    "Подробнее: /help"
)

def get_help_text(user_id: Optional[int] = None) -> str:
    mode = config.get_effective_mode()
    mode_name = {"groq": "Groq (быстро, бесплатно)", "api": "OpenAI API", "local": "локальная модель"}.get(mode, mode)
    # per-user prefs
    lang_code = prefs.get_user_lang(user_id) if user_id else None
    lang_disp = prefs.lang_display(lang_code)
    tone_on = prefs.get_tone_enabled(user_id) if user_id else False
    tone_disp = "вкл ✅" if tone_on else "выкл"
    return (
        "ℹ️ Как пользоваться:\n"
        "1. Пришли голосовое, кружок или видео как обычно\n"
        "2. Подожди 2-5 сек — я пришлю текст\n\n"
        f"Лимиты: файл до 20МБ, длительность до ~10-15 мин оптимально.\n"
        f"Текущий STT режим: `{mode}` ({mode_name})\n"
        f"Твой язык: `{lang_disp}`\n"
        f"Режим интонации: `{tone_disp}`\n\n"
        "Команды:\n"
        "• /start — приветствие\n"
        "• /help — эта справка\n"
        "• /ru /en /de /fr /es /ja /zh ... — любой язык (99 шт.), сразу перерасшифрует последнее ГС или то, на которое ответил\n"
        "• /auto — сбросить в авто + перерасшифровать\n"
        "• /ton — сразу перерасшифрует последнее ГС/ВС с интонацией (или то, на которое ответил)\n"
        "  • Ответь на своё ГС/ВС сообщением /ton — перерасшифрует именно его с интонацией\n"
        "• /check — перепроверить логичность (маты сохраню, только после) — тот же принцип: ответом на своё ГС/ВС или на сообщение бота, иначе последнее\n"
    )

# Ограничитель параллельных транскрибаций чтобы не забить CPU (i3-1315U 8 потоков)
transcribe_semaphore = asyncio.Semaphore(2)

# Прекомпилированные регэкспы — оптимизация (не компилировать на каждое сообщение)
LANG_RE = re.compile(r"^/([a-z]{2,3})(?:@\w+)?\s*$")

# Кэш последнего медиа по чату — для /ton и /ru без реплая
# chat_id -> Message (voice/video_note/video/audio/document)
last_media_by_chat: dict[int, Message] = {}
# Кэш последнего текста бота — для /check (только после, без Было)
last_text_by_chat: dict[int, str] = {}
# Кэш текст по ID исходного ГС/ВС — для /check ответом на своё ГС (тот же принцип что и /ru/ton)
last_text_by_media_id: dict[int, str] = {}

# Лимит кэшей — защита от безграничного роста памяти
_MAX_CHAT_CACHE = 500
_MAX_MEDIA_TEXT_CACHE = 1000

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


def _cache_text(chat_id: int, media_msg_id: Optional[int], text: str) -> None:
    """Кэширует текст с ограничением размера (LRU-подобно через удаление старых)"""
    clean = text.strip()
    last_text_by_chat[chat_id] = clean
    if media_msg_id is not None:
        last_text_by_media_id[media_msg_id] = clean
        # лимит по кол-ву медиа-текстов
        if len(last_text_by_media_id) > _MAX_MEDIA_TEXT_CACHE:
            # удалить самый старый (первый вставленный — dict сохраняет порядок с Python 3.7)
            oldest = next(iter(last_text_by_media_id))
            last_text_by_media_id.pop(oldest, None)
    # лимит по чатам
    if len(last_text_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_text_by_chat))
        last_text_by_chat.pop(oldest, None)
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(START_TEXT)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    uid = message.from_user.id if message.from_user else None
    await message.answer(get_help_text(uid), parse_mode="Markdown")

# ---------- Language handlers ----------

@dp.message(Command("auto"))
async def cmd_auto(message: Message):
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
    uid = message.from_user.id
    target = _get_target_media(message)
    if target:
        await process_transcription(target, force_tone=True, reply_to_message=message)
        return
    # 3. иначе — обычный тоггл для будущих аудио
    new_state = prefs.toggle_tone(uid)
    if new_state:
        await message.answer(
            "✅ Режим интонации *ВКЛ* ✅\n"
            "Теперь все аудио будут с метками:\n"
            "`[спокойный тон]` `[радостный]` `[грустный]` `[злой]` `[взволнованный]` `[шёпот]` `[крик]` `[сарказм/ирония]` ...\n"
            "Повтори /ton чтобы выключить.\n"
            "Или просто напиши /ton когда есть ГС в чате — сразу перерасшифрую последнее.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "☑️ Режим интонации *ВЫКЛ*\n"
            "Теперь аудио будут без меток. /ton — снова вкл. или напиши /ton на ГС чтобы разово.",
            parse_mode="Markdown"
        )

# Динамический хэндлер для /ru /en /ja /de ... любой ISO код
@dp.message(lambda m: bool(m.text and _extract_lang_code(m.text)))
async def cmd_dynamic_lang(message: Message):
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
    # ручная перепроверка логичности, только после (БЫЛО не показываем)
    target_text = None
    lang_hint = None

    # 1. если ответ на сообщение бота с текстом — берём его
    if message.reply_to_message:
        rt = message.reply_to_message
        # текст бота может быть в rt.text (📝/🎭/✅)
        if rt.text and ("📝 Текст:" in rt.text or "🎭 Текст" in rt.text or "✅ Проверено:" in rt.text):
            # вырезаем заголовок
            t = rt.text
            for p in ("📝 Текст:\n", "🎭 Текст с интонацией:\n", "✅ Проверено:\n"):
                if t.startswith(p):
                    t = t[len(p):].lstrip()
                    break
            target_text = t
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
        _cache_text(message.chat.id, None, fixed.strip())
        logger.info(f"check {len(target_text)}->{len(fixed)} lang={lang_hint or 'auto'}")
    except Exception as e:
        await status.edit_text(f"❌ Не удалось отправить: {e}")

@dp.message(F.voice | F.video_note | F.video | F.audio)
async def handle_media(message: Message):
    # кэшируем для /ton и /ru без реплая с лимитом
    last_media_by_chat[message.chat.id] = message
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)
    await process_transcription(message)

@dp.message(lambda m: is_audio_document(m))
async def handle_document(message: Message):
    last_media_by_chat[message.chat.id] = message
    if len(last_media_by_chat) > _MAX_CHAT_CACHE:
        oldest = next(iter(last_media_by_chat))
        last_media_by_chat.pop(oldest, None)
    await process_transcription(message)

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

async def process_transcription(message: Message, lang_override: Optional[str] = None, force_tone: bool = False, reply_to_message: Optional[Message] = None):
    """Скачивает файл, транскрибирует, отвечает. lang_override=None -> берёт из prefs/auto."""

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
    tone_enabled = force_tone or (prefs.get_tone_enabled(lang_uid) if lang_uid else False)

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
        logger.info(f"downloaded {description} {size_mb:.2f}MB -> {input_path} lang={eff_lang or 'auto'} tone={tone_enabled}")

        # 2. Длительность (инфо)
        duration = await stt.get_audio_duration(input_path)
        if duration:
            logger.info(f"duration {duration:.1f}s")
            if duration > 3600:
                await status_msg.edit_text("❌ Слишком длинное аудио (>60 мин). Пришлите короче")
                return

        # 3. Транскрибация (с семафором чтобы не положить CPU)
        try:
            async with transcribe_semaphore:
                text = await stt.transcribe_file(input_path, lang_override=eff_lang)
            # 3b. интонация если включена — вне семафора (сеть, не CPU)
            if tone_enabled and text and text.strip():
                try:
                    await status_msg.edit_text(f"🎭 Распознаю {description}... (+ интонация)")
                except Exception:
                    pass
                annotated = await tone_module.annotate_tone(text)
                # если LLM вернул что-то осмысленное — используем его
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
        if len(full) <= 4096:
            await status_msg.edit_text(full)
        else:
            # первое редактируем статус, остальное новыми сообщениями
            await status_msg.edit_text(full[:4096])
            for i in range(4096, len(full), 4096):
                await status_target.reply(full[i:i+4096])
                await asyncio.sleep(0.3)  # анти-флуд

        _cache_text(status_target.chat.id, message.message_id, text.strip())
        logger.info(f"done {description} -> {len(text)} chars tone={tone_enabled} lang={eff_lang or 'auto'}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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

    mode = config.get_effective_mode()
    if mode == "groq":
        logger.info(f"Starting bot | mode={mode} groq_model={config.GROQ_MODEL} chat={config.GROQ_CHAT_MODEL} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    elif mode == "local":
        logger.info(f"Starting bot | mode={mode} model={config.WHISPER_MODEL} beam={config.WHISPER_BEAM_SIZE} threads={config.WHISPER_CPU_THREADS} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    else:
        logger.info(f"Starting bot | mode={mode} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
    if not shutil.which("ffmpeg"):
        logger.error("ffmpeg не найден! Установи: sudo apt install ffmpeg")
        print("❌ ffmpeg не найден — поставь: sudo apt install ffmpeg")
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

    # команды в меню Telegram
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Приветствие"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="check", description="Перепроверить логичность (маты сохраню)"),
            BotCommand(command="auto", description="Сбросить язык в авто"),
            BotCommand(command="ton", description="Перерасшифровать с интонацией"),
            BotCommand(command="ru", description="Язык: русский"),
            BotCommand(command="en", description="Language: English"),
            BotCommand(command="de", description="Sprache: Deutsch"),
            BotCommand(command="uk", description="Мова: українська"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands failed {e}")

    # polling с дропом pending апдейтов чтобы не обрабатывать старые ГС
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
