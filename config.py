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
# Groq Chat для интонации
GROQ_CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-20b").strip()  # openai/gpt-oss-20b | llama-3.1-8b-instant | qwen/qwen3-32b
# Резервные LLM — цепочка Groq -> Cerebras -> Mistral -> OpenAI
MISTRAL_API_KEY = (os.getenv("MISTRAL_API_KEY", "") or os.getenv("MISTRAL_KEY", "")).strip()
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip()
CEREBRAS_API_KEY = (os.getenv("CEREBRAS_API_KEY", "") or os.getenv("CEREBRAS_KEY", "")).strip()
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama3.1-8b").strip()
# персист per-user настроек
USER_PREFS_PATH = os.getenv("USER_PREFS_PATH", "user_prefs.json").strip()
MEDIA_DIR = os.getenv("MEDIA_DIR", "media").strip()
MEDIA_RETENTION_DAYS = int(os.getenv("MEDIA_RETENTION_DAYS", "30"))
# admin
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
USAGE_DB_PATH = os.getenv("USAGE_DB_PATH", "usage.db").strip()
# На Fly env-переменная может прийти искажённой (MSYS/Windows path conversion) —
# в таком случае используем стандартный путь volume.
if ("Program Files" in USAGE_DB_PATH or "Git" in USAGE_DB_PATH
        or "C:" in USAGE_DB_PATH or not os.path.isabs(USAGE_DB_PATH)):
    if os.path.exists("/app/data"):
        USAGE_DB_PATH = "/app/data/usage.db"
    elif not os.path.isabs(USAGE_DB_PATH):
        USAGE_DB_PATH = os.path.abspath(USAGE_DB_PATH)
# автопроверка логичности (как /check но автоматом)
AUTO_PROOF = os.getenv("AUTO_PROOF", "false").strip().lower() in ("1", "true", "yes", "on")
# лимиты
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))  # 10 мин на чанк, но принимаем до 60 мин
# антиспам
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5"))
# донаты — через env, без хардкода секретов
DONATE_CARD = os.getenv("DONATE_CARD", "").strip()
DONATE_URL = os.getenv("DONATE_URL", "").strip()


def _parse_int_set(raw: str, default: set[int]) -> set[int]:
    if not raw.strip():
        return set(default)
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out or set(default)


DONATE_EXCLUDED_USERS: set[int] = _parse_int_set(os.getenv("DONATE_EXCLUDED_IDS", ""), set())
SUPPORT_MILESTONES: set[int] = _parse_int_set(
    os.getenv("SUPPORT_MILESTONES", "5,7,9,11,13,15,17,19,21"), {5, 7, 9, 11, 13, 15, 17, 19, 21}
)

def is_admin(user_id: int | str) -> bool:
    return str(user_id) in ADMIN_IDS

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
