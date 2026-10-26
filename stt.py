import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ленивая инициализация клиентов
_openai_client = None
_groq_client = None
_mistral_client = None
_cerebras_client = None
_local_model = None

def get_openai_client():
    global _openai_client
    if _openai_client is None and config.OPENAI_API_KEY:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client

def get_groq_client():
    global _groq_client
    if _groq_client is None and config.GROQ_API_KEY:
        from openai import AsyncOpenAI
        _groq_client = AsyncOpenAI(
            api_key=config.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client

def get_mistral_client():
    global _mistral_client
    if _mistral_client is None and config.MISTRAL_API_KEY:
        from openai import AsyncOpenAI
        _mistral_client = AsyncOpenAI(
            api_key=config.MISTRAL_API_KEY,
            base_url="https://api.mistral.ai/v1",
        )
    return _mistral_client

def get_cerebras_client():
    global _cerebras_client
    if _cerebras_client is None and config.CEREBRAS_API_KEY:
        from openai import AsyncOpenAI
        _cerebras_client = AsyncOpenAI(
            api_key=config.CEREBRAS_API_KEY,
            base_url="https://api.cerebras.ai/v1",
        )
    return _cerebras_client

# Единый helper для срезания заголовков результата ("📝 Текст:\n" и т.д.)
HEADER_PREFIXES = (
    "📝 Текст:\n",
    "🎭 Текст с интонацией:\n",
    "✅ Проверено:\n",
    "📌 Кратко:\n",
)


def strip_header(text: str) -> str:
    """Убирает один ведущий заголовок результата, возвращает чистый текст."""
    t = (text or "").strip()
    for p in HEADER_PREFIXES:
        if t.startswith(p):
            return t[len(p):].lstrip()
    return t

def _is_retryable_chat_error(e: Exception) -> bool:
    """Определяет, стоит ли фолбэчить на следующий провайдер при ошибке чата."""
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(e, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in (429, 500, 502, 503, 504):
        return True
    name = type(e).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "network", "ratelimit")):
        return True
    # По умолчанию пробуем следующий провайдер — лучше дать шанс, чем упасть.
    return True


async def chat_with_fallback(messages, temperature: float, max_tokens: int, model: str | None = None):
    """Пробует Groq -> Cerebras -> Mistral -> OpenAI по цепочке. Логика: 429/5xx/сеть/таймаут — ретрай на следующем, иначе сразу успех."""
    errs = []
    # 1. Groq
    groq = get_groq_client()
    if groq:
        m = model or config.GROQ_CHAT_MODEL
        try:
            r = await groq.chat.completions.create(model=m, messages=messages, temperature=temperature, max_tokens=max_tokens)
            return r, f"groq:{m}"
        except Exception as e:
            errs.append(f"groq:{e}")
            if not _is_retryable_chat_error(e):
                # если это не ретрайбл (напр. 400) — не фолбэчим, возвращаем как есть
                # но для совместимости попробуем следующий если это 429/5xx
                pass
            logger.warning(f"groq chat failed {e}, try cerebras")
    # 2. Cerebras
    cere = get_cerebras_client()
    if cere:
        m = config.CEREBRAS_MODEL
        try:
            r = await cere.chat.completions.create(model=m, messages=messages, temperature=temperature, max_tokens=max_tokens)
            return r, f"cerebras:{m}"
        except Exception as e:
            errs.append(f"cerebras:{e}")
            logger.warning(f"cerebras chat failed {e}, try mistral")
    # 3. Mistral
    mist = get_mistral_client()
    if mist:
        m = config.MISTRAL_MODEL
        try:
            r = await mist.chat.completions.create(model=m, messages=messages, temperature=temperature, max_tokens=max_tokens)
            return r, f"mistral:{m}"
        except Exception as e:
            errs.append(f"mistral:{e}")
            logger.warning(f"mistral chat failed {e}, try openai")
    # 4. OpenAI
    oai = get_openai_client()
    if oai:
        m = "gpt-4o-mini"
        # если у groq модель не подходит для openai — форсим
        if model and model.startswith("openai/"):
            m = "gpt-4o-mini"
        try:
            r = await oai.chat.completions.create(model=m, messages=messages, temperature=temperature, max_tokens=max_tokens)
            return r, f"openai:{m}"
        except Exception as e:
            errs.append(f"openai:{e}")
            raise Exception(f"all chats failed: {'; '.join(errs)}") from e
    raise Exception(f"no chat client / all failed: {'; '.join(errs) if errs else 'no keys'}")

def get_local_model():
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel
        _local_model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type="int8" if config.WHISPER_DEVICE == "cpu" else "float16",
            cpu_threads=config.WHISPER_CPU_THREADS,
            num_workers=1,
        )
    return _local_model


async def convert_to_wav(input_path: str, output_path: str) -> None:
    """Конвертирует в wav 16kHz mono с нормализацией громкости и чисткой шума — макс качество."""
    # highpass 200Hz убирает гул, dynaudnorm + loudnorm нормализует громкость — макс качество
    # без lowpass чтобы не резать шипящие, только чистка
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-af", "highpass=f=200,dynaudnorm=f=150:g=15,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        "-y",
        output_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # фолбэк без фильтров — если dynaudnorm/loudnorm не поддержался
        err0 = stderr.decode(errors="ignore")[-2000:]
        fallback_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "0",
            "-i", input_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-y", output_path
        ]
        proc2 = await asyncio.create_subprocess_exec(
            *fallback_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr2 = await proc2.communicate()
        if proc2.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {err0} | fallback {stderr2.decode(errors='ignore')[-1000:]}")
    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise RuntimeError("ffmpeg не создал выходной файл")


async def transcribe_api(wav_path: str, lang_override: Optional[str] = None) -> str:
    """Транскрибация через OpenAI Whisper API — макс качество."""
    client = get_openai_client()
    if not client:
        raise RuntimeError("OPENAI_API_KEY не задан для api режима")
    wav_size = Path(wav_path).stat().st_size
    if wav_size > 24 * 1024 * 1024:
        return await transcribe_api_chunked(wav_path, client, lang_override)
    # per-user lang приоритетнее глобального config
    eff_lang = lang_override if lang_override is not None else config.WHISPER_LANGUAGE
    with open(wav_path, "rb") as f:
        kwargs = dict(model="whisper-1", file=f, response_format="text")
        if eff_lang:
            kwargs["language"] = eff_lang
        if config.WHISPER_INITIAL_PROMPT:
            kwargs["prompt"] = config.WHISPER_INITIAL_PROMPT
        resp = await client.audio.transcriptions.create(**kwargs)
    if isinstance(resp, str):
        return resp.strip()
    return getattr(resp, "text", str(resp)).strip()

async def transcribe_groq_verbose(wav_path: str, lang_override: Optional[str] = None) -> str:
    """Verbose с таймкодами для длинных — 00:00 — текст"""
    client = get_groq_client()
    if not client:
        raise RuntimeError("GROQ_API_KEY не задан")
    eff_lang = lang_override if lang_override is not None else config.WHISPER_LANGUAGE
    with open(wav_path, "rb") as f:
        kwargs = dict(model=config.GROQ_MODEL, file=f, response_format="verbose_json", timestamp_granularities=["segment"])
        if eff_lang and config.GROQ_MODEL != "distil-whisper-large-v3-en":
            kwargs["language"] = eff_lang
        try:
            resp = await client.audio.transcriptions.create(**kwargs)
        except Exception as e:
            if "language" in str(e).lower():
                kwargs.pop("language", None)
                resp = await client.audio.transcriptions.create(**kwargs)
            else:
                raise
    # resp.segments [{start,end,text}]
    segs = getattr(resp, "segments", None) or getattr(resp, "text", "")
    if isinstance(segs, str):
        return segs.strip()
    if not segs:
        return getattr(resp, "text", "").strip() if hasattr(resp, "text") else str(resp).strip()
    out_lines = []
    for s in segs:
        start = getattr(s, "start", 0) if not isinstance(s, dict) else s.get("start",0)
        txt = getattr(s, "text", "") if not isinstance(s, dict) else s.get("text","")
        txt = txt.strip()
        if not txt:
            continue
        mm = int(start)//60
        ss = int(start)%60
        out_lines.append(f"{mm:02d}:{ss:02d} — {txt}")
    return "\n".join(out_lines).strip() if out_lines else getattr(resp, "text", "").strip()

async def transcribe_groq(wav_path: str, lang_override: Optional[str] = None) -> str:
    """Транскрибация через Groq Whisper API — САМЫЙ БЫСТРЫЙ Бесплатный (0% CPU)."""
    client = get_groq_client()
    if not client:
        raise RuntimeError("GROQ_API_KEY не задан")
    wav_size = Path(wav_path).stat().st_size
    if wav_size > 24 * 1024 * 1024:
        return await transcribe_groq_chunked(wav_path, client, lang_override)
    eff_lang = lang_override if lang_override is not None else config.WHISPER_LANGUAGE
    with open(wav_path, "rb") as f:
        # Groq модели: whisper-large-v3-turbo (самый быстрый), whisper-large-v3
        kwargs = dict(model=config.GROQ_MODEL, file=f, response_format="json")
        # Groq не поддерживает language/prompt для всех моделей, пробуем с ними, fallback без
        if eff_lang and config.GROQ_MODEL != "distil-whisper-large-v3-en":
            kwargs["language"] = eff_lang
        try:
            resp = await client.audio.transcriptions.create(**kwargs)
        except Exception as e:
            # fallback без language если Groq ругается
            if "language" in str(e).lower():
                kwargs.pop("language", None)
                resp = await client.audio.transcriptions.create(**kwargs)
            else:
                raise
    # Groq возвращает объект с text
    if isinstance(resp, str):
        return resp.strip()
    return getattr(resp, "text", str(resp)).strip()

async def _transcribe_chunked(wav_path: str, client, model: str, response_format: str = "json", lang_override: Optional[str] = None) -> str:
    """Общий чанкинг для Groq/OpenAI: режет wav по 10 мин и склеивает."""
    tmpdir = tempfile.mkdtemp(prefix="whisper_chunks_")
    try:
        pattern = str(Path(tmpdir) / "chunk_%03d.wav")
        # используем config.MAX_DURATION_SEC (600 = 10 мин) вместо хардкода
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", wav_path, "-f", "segment", "-segment_time", str(config.MAX_DURATION_SEC), "-c", "copy", pattern]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"chunk ffmpeg failed: {stderr.decode(errors='ignore')[-1000:]}")
        chunks = sorted(Path(tmpdir).glob("chunk_*.wav"))
        if not chunks:
            raise RuntimeError("Не удалось нарезать на чанки")
        texts = []
        for ch in chunks:
            with open(ch, "rb") as f:
                kwargs = dict(model=model, file=f, response_format=response_format)
                if lang_override:
                    # chunked тоже пробуем с языком, fallback без если ошибка
                    kwargs["language"] = lang_override
                try:
                    r = await client.audio.transcriptions.create(**kwargs)
                except Exception as e:
                    if "language" in str(e).lower() and "language" in kwargs:
                        kwargs.pop("language", None)
                        r = await client.audio.transcriptions.create(**kwargs)
                    else:
                        raise
                t = r.strip() if isinstance(r, str) else getattr(r, "text", str(r)).strip()
                if t:
                    texts.append(t)
        return " ".join(texts).strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def transcribe_api_chunked(wav_path: str, client, lang_override: Optional[str] = None) -> str:
    return await _transcribe_chunked(wav_path, client, "whisper-1", "text", lang_override)

async def transcribe_groq_chunked(wav_path: str, client, lang_override: Optional[str] = None) -> str:
    return await _transcribe_chunked(wav_path, client, config.GROQ_MODEL, "json", lang_override)


def transcribe_local(wav_path: str, lang_override: Optional[str] = None) -> str:
    """Транскрибация локально — МАКСИМАЛЬНОЕ КАЧЕСТВО."""
    model = get_local_model()
    eff_lang = lang_override if lang_override is not None else config.WHISPER_LANGUAGE
    # макс качество: beam=5, best_of=5, temp 0, condition_on_previous_text, vad 500мс
    # threshold'ы из дефолта Whisper, без without_timestamps=False чтобы модель видела контекст
    segments, info = model.transcribe(
        wav_path,
        language=eff_lang,  # None=авто, иначе ru — точнее если знаешь
        beam_size=config.WHISPER_BEAM_SIZE,  # 5
        best_of=5,
        temperature=0.0,
        condition_on_previous_text=True,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=config.WHISPER_VAD_MIN_SILENCE_MS,
            speech_pad_ms=400,
        ),
        # пороги качества — отбрасывает галлюцинации
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        initial_prompt=config.WHISPER_INITIAL_PROMPT,
        without_timestamps=True,
    )
    logger.info(f"Whisper detected lang={info.language} prob={info.language_probability:.2f} duration={info.duration:.1f}s")
    texts = [seg.text.strip() for seg in segments if seg.text.strip()]
    return " ".join(texts).strip()


async def transcribe_file(input_path: str, lang_override: Optional[str] = None) -> str:
    """
    Главный оркестратор: конвертирует входной файл в wav и транскрибирует
    выбранным движком. Приоритет groq (самый быстрый бесплатный) > api > local.
    lang_override: per-user язык (None=авто/глобальный config.WHISPER_LANGUAGE)
    """
    mode = config.get_effective_mode()
    tmpdir = tempfile.mkdtemp(prefix="stt_")
    wav_path = str(Path(tmpdir) / "audio.wav")
    try:
        await convert_to_wav(input_path, wav_path)
        
        if mode == "groq":
            try:
                text = await transcribe_groq(wav_path, lang_override)
            except Exception as e:
                logger.warning(f"Groq failed {e}, fallback to local")
                loop = asyncio.get_running_loop()
                text = await loop.run_in_executor(None, transcribe_local, wav_path, lang_override)
        elif mode == "api":
            text = await transcribe_api(wav_path, lang_override)
        else:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, transcribe_local, wav_path, lang_override)
        
        return text.strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def get_audio_duration(input_path: str) -> Optional[float]:
    """Пытается получить длительность через ffprobe, возвращает секунды или None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-threads", "0", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return float(stdout.decode().strip())
    except Exception:
        pass
    return None
