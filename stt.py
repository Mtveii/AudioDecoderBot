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
    stdout, stderr = await proc.communicate()
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


async def transcribe_api(wav_path: str) -> str:
    """Транскрибация через OpenAI Whisper API — макс качество."""
    client = get_openai_client()
    if not client:
        raise RuntimeError("OPENAI_API_KEY не задан для api режима")
    wav_size = Path(wav_path).stat().st_size
    if wav_size > 24 * 1024 * 1024:
        return await transcribe_api_chunked(wav_path, client)
    with open(wav_path, "rb") as f:
        kwargs = dict(model="whisper-1", file=f, response_format="text")
        if config.WHISPER_LANGUAGE:
            kwargs["language"] = config.WHISPER_LANGUAGE
        if config.WHISPER_INITIAL_PROMPT:
            kwargs["prompt"] = config.WHISPER_INITIAL_PROMPT
        resp = await client.audio.transcriptions.create(**kwargs)
    if isinstance(resp, str):
        return resp.strip()
    return getattr(resp, "text", str(resp)).strip()

async def transcribe_groq(wav_path: str) -> str:
    """Транскрибация через Groq Whisper API — САМЫЙ БЫСТРЫЙ Бесплатный (0% CPU)."""
    client = get_groq_client()
    if not client:
        raise RuntimeError("GROQ_API_KEY не задан")
    wav_size = Path(wav_path).stat().st_size
    if wav_size > 24 * 1024 * 1024:
        return await transcribe_groq_chunked(wav_path, client)
    with open(wav_path, "rb") as f:
        # Groq модели: whisper-large-v3-turbo (самый быстрый), whisper-large-v3
        kwargs = dict(model=config.GROQ_MODEL, file=f, response_format="json")
        # Groq не поддерживает language/prompt для всех моделей, пробуем с ними, fallback без
        if config.WHISPER_LANGUAGE and config.GROQ_MODEL != "distil-whisper-large-v3-en":
            kwargs["language"] = config.WHISPER_LANGUAGE
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

async def _transcribe_chunked(wav_path: str, client, model: str, response_format: str = "json") -> str:
    """Общий чанкинг для Groq/OpenAI: режет wav по 10 мин и склеивает."""
    tmpdir = tempfile.mkdtemp(prefix="whisper_chunks_")
    try:
        pattern = str(Path(tmpdir) / "chunk_%03d.wav")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", wav_path, "-f", "segment", "-segment_time", "600", "-c", "copy", pattern]
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
                r = await client.audio.transcriptions.create(model=model, file=f, response_format=response_format)
                t = r.strip() if isinstance(r, str) else getattr(r, "text", str(r)).strip()
                if t:
                    texts.append(t)
        return " ".join(texts).strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def transcribe_api_chunked(wav_path: str, client) -> str:
    return await _transcribe_chunked(wav_path, client, "whisper-1", "text")

async def transcribe_groq_chunked(wav_path: str, client) -> str:
    return await _transcribe_chunked(wav_path, client, config.GROQ_MODEL, "json")


def transcribe_local(wav_path: str) -> str:
    """Транскрибация локально — МАКСИМАЛЬНОЕ КАЧЕСТВО."""
    model = get_local_model()
    # макс качество: beam=5, best_of=5, temp 0, condition_on_previous_text, vad 500мс
    # threshold'ы из дефолта Whisper, без without_timestamps=False чтобы модель видела контекст
    segments, info = model.transcribe(
        wav_path,
        language=config.WHISPER_LANGUAGE,  # None=авто, иначе ru — точнее если знаешь
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


async def transcribe_file(input_path: str) -> str:
    """
    Главный оркестратор: конвертирует входной файл в wav и транскрибирует
    выбранным движком. Приоритет groq (самый быстрый бесплатный) > api > local.
    """
    mode = config.get_effective_mode()
    tmpdir = tempfile.mkdtemp(prefix="stt_")
    wav_path = str(Path(tmpdir) / "audio.wav")
    try:
        await convert_to_wav(input_path, wav_path)
        
        if mode == "groq":
            try:
                text = await transcribe_groq(wav_path)
            except Exception as e:
                logger.warning(f"Groq failed {e}, fallback to local")
                loop = asyncio.get_running_loop()
                text = await loop.run_in_executor(None, transcribe_local, wav_path)
        elif mode == "api":
            text = await transcribe_api(wav_path)
        else:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, transcribe_local, wav_path)
        
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
