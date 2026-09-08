import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "whisper-large-v3-turbo").strip()  # turbo=самый быстрый, large-v3=точнее
# auto = groq если есть ключ > api если есть ключ > local, можно форсить groq/api/local
STT_MODE = os.getenv("STT_MODE", "auto").strip().lower()
# для локальной модели — МАКСИМАЛЬНОЕ КАЧЕСТВО (small 244M, можно medium/large-v3-turbo если нужно ещё точнее)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small").strip()  # tiny/base/small/medium/large-v3/large-v3-turbo
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu").strip()  # cpu/cuda
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))  # 5=макс качество, 1=быстро
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "8"))  # под i3-1315U 8 потоков
WHISPER_VAD_MIN_SILENCE_MS = int(os.getenv("WHISPER_VAD_MIN_SILENCE_MS", "500"))  # 500=баланс, 300=ловит короткие маты
# доп тюнинг качества
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip() or None  # None=авто, ru/en/uk — форсить если знаешь язык
WHISPER_INITIAL_PROMPT = os.getenv("WHISPER_INITIAL_PROMPT", "").strip() or None  # подсказка: "Привет, это разговор на русском с матом"
# лимиты
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))  # 10 мин на чанк, но принимаем до 60 мин

def validate_config():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN не задан или неверный. Создай бота в @BotFather и добавь токен в .env: BOT_TOKEN=123456:ABC..."
        )
    if STT_MODE not in ("auto", "api", "local", "groq"):
        raise ValueError("STT_MODE должен быть auto/api/local/groq")
    if STT_MODE == "api" and not OPENAI_API_KEY:
        raise ValueError("STT_MODE=api но OPENAI_API_KEY не задан")
    if STT_MODE == "groq" and not GROQ_API_KEY:
        raise ValueError("STT_MODE=groq но GROQ_API_KEY не задан")
    return True

def get_effective_mode() -> str:
    """Возвращает реальный режим с учетом наличия ключей. Приоритет groq > api > local."""
    if STT_MODE == "auto":
        if GROQ_API_KEY:
            return "groq"
        if OPENAI_API_KEY:
            return "api"
        return "local"
    return STT_MODE
