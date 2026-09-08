import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

import config
import stt

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
    "Я сразу верну текст. Без команд."
)

def get_help_text() -> str:
    mode = config.get_effective_mode()
    mode_name = {"groq": "Groq (быстро, бесплатно)", "api": "OpenAI API", "local": "локальная модель"}.get(mode, mode)
    return (
        "ℹ️ Как пользоваться:\n"
        "1. Пришли голосовое, кружок или видео как обычно\n"
        "2. Подожди 2-5 сек — я пришлю текст\n\n"
        f"Лимиты: файл до 20МБ, длительность до ~10-15 мин оптимально.\n"
        f"Текущий режим: `{mode}` ({mode_name})\n\n"
        "Команды: /start — приветствие, /help — эта справка"
    )

# Ограничитель параллельных транскрибаций чтобы не забить CPU (i3-1315U 8 потоков)
transcribe_semaphore = asyncio.Semaphore(2)

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

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(START_TEXT)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(get_help_text(), parse_mode="Markdown")

@dp.message(F.voice | F.video_note | F.video | F.audio)
async def handle_media(message: Message):
    await process_transcription(message)

@dp.message(lambda m: is_audio_document(m))
async def handle_document(message: Message):
    await process_transcription(message)

@dp.message(F.text & ~F.via_bot)
async def fallback_text(message: Message):
    # не спамим в группах на каждое сообщение
    if message.chat.type != "private":
        return
    # если текст команды уже обработан - сюда не попадем из-за порядка, но на всякий
    if message.text and message.text.startswith("/"):
        return
    await message.answer("Пришли голосовое, кружок или видео — я превращу его в текст 🎤")

async def process_transcription(message: Message):
    """Скачивает файл, транскрибирует, отвечает."""
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
            await message.reply(f"❌ Файл слишком большой ({message.voice.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB")
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
            await message.reply(f"❌ Видео слишком большое ({message.video.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB, попробуйте короче")
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
            await message.reply(f"❌ Файл слишком большой ({message.document.file_size/1024/1024:.1f}MB). Лимит {config.MAX_FILE_SIZE_MB}MB")
            return
    else:
        await message.reply("❌ Неизвестный тип файла")
        return

    # статус сообщение (редактируем потом)
    status_msg = await message.reply(f"⏳ Распознаю {description}...")

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
        logger.info(f"downloaded {description} {size_mb:.2f}MB -> {input_path}")

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
                text = await stt.transcribe_file(input_path)
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
        header = "📝 Текст:\n"
        full = header + text.strip()
        if len(full) <= 4096:
            await status_msg.edit_text(full)
        else:
            # первое редактируем статус, остальное новыми сообщениями
            await status_msg.edit_text(full[:4096])
            for i in range(4096, len(full), 4096):
                await message.reply(full[i:i+4096])
                await asyncio.sleep(0.3)  # анти-флуд

        logger.info(f"done {description} -> {len(text)} chars")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def main():
    try:
        config.validate_config()
    except ValueError as e:
        logger.error(str(e))
        print(f"\n❌ {e}\nСоздай .env из .env.example и укажи BOT_TOKEN\n")
        return

    mode = config.get_effective_mode()
    if mode == "groq":
        logger.info(f"Starting bot | mode={mode} groq_model={config.GROQ_MODEL} | ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}")
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

    # polling с дропом pending апдейтов чтобы не обрабатывать старые ГС
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
