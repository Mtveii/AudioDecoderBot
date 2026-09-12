# AudioDecoderBot — транскрибация голосовых и кружков в текст

Кинул ГС / кружок / видео → получаешь текст за 1-3 сек. Работает в личке и группах.

> Стек: `aiogram 3` + `faster-whisper` / `Groq` / `OpenAI` + `ffmpeg` + `Docker`

## Что умеет

- 🎤 `voice` (ogg) · 🔵 `video_note` (кружки) · 🎬 `video`/`audio`/`document` (mp3, m4a, wav, ogg, flac и т.д.)
- Автоопределение языка `ru / uk / en` + **форс любого языка** `99` штук через `/ru` `/en` `/de` ... (спасает на плохом аудио)
- 🎭 **Интонация** `/ton` — `[спокойный тон] [радостный] [злой] [шёпот] [крик] [сарказм/ирония]`
- ✅ **Проверка** `/check` — логичность, маты дословно, только после (от 1 мин `📌 /summary`)
- Предобработка `highpass + dynaudnorm + loudnorm` → чистый звук
- Чанкинг `>24MB` по 10 мин, кэш по `file_id` — повторный `/ru` мгновенно
- Таймкоды `00:00 —` для длинных `>2.5 мин` (`verbose_json`)
- Семафор `2` + антиспам `5/мин` — не кладёт CPU

## Быстрый старт

```bash
cp .env.example .env
nano .env  # BOT_TOKEN + GROQ_API_KEY (console.groq.com/keys)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
./start.sh && tail -f bot.log
```

## Docker (прод)

```bash
docker compose up -d --build
docker ps | grep ai-dehif_bot
docker logs -f ai-dehif_bot
```

`restart: unless-stopped` + `healthcheck 30s` + `limits 2cpu/1.5G` + `volumes` для персиста.

## Команды

| Команда | Что делает |
|---------|------------|
| `/start` `/help` | Приветствие, справка |
| `/ru` `/en` `/de` … | Любой язык 99 шт. — перерасшифрует последнее/реплей |
| `/auto` | Авто + перерасшифровать |
| `/ton` | С интонацией (кнопки `🎭 Check 📝 Summary` под каждым ответом) |
| `/check` | Логичность, маты сохранены |
| `/summary` | Кратко 3-5 предл., от 1 мин (`300` симв) |

Кнопки под сообщением: `🎭 Тон` `✅ Check` `📌 Summary` `🌍 EN` — без печати команд.

## Конфиг `.env`

```ini
BOT_TOKEN=123456:AAH...
GROQ_API_KEY=gsk_...
GROQ_MODEL=whisper-large-v3-turbo
GROQ_CHAT_MODEL=openai/gpt-oss-20b
# резерв Groq→Cerebras→Mistral→OpenAI
MISTRAL_API_KEY=
CEREBRAS_API_KEY=
STT_MODE=auto
WHISPER_MODEL=small
AUTO_PROOF=false
RATE_LIMIT_PER_MIN=5
```

Резерв LLM: `429/5xx/timeout` → `Cerebras llama3.1-8b` → `Mistral small` → `OpenAI gpt-4o-mini`.

## Как работает

- `bot.py` — `process_transcription` `file_id` кэш + `verbose` + `AUTO_PROOF` + `tone`
- `stt.py` — `ffmpeg` `highpass` + `Groq`/`local` + `chunk 600с`
- `tone.py`/`checker.py`/`summary.py` — `Groq Chat` с фолбэком
- `prefs.py` — `user_prefs.json` per-user

## Лимиты

`20MB` файл, `25MB` wav, `>60 мин` отклоняется, `Groq Chat +0.4с` `~$0.00004`.

## Разработка

```bash
python3 -m py_compile bot.py stt.py
.venv/bin/pytest tests/test_checker.py  # маты остаются
./backup.sh  # tar usage.db + user_prefs
```

MIT
