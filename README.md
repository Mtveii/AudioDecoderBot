# AudioDecoderBot — транскрибация голосовых и кружков в текст

Кинул ГС / кружок / видео → получаешь текст за 1-3 сек. Работает в личке и группах.

> Стек: `aiogram 3` + `faster-whisper` / `Groq` / `OpenAI` + `ffmpeg` + `Docker`

## Что умеет

- 🎤 `voice` (ogg)
- 🔵 `video_note` (кружки)
- 🎬 `video` / `audio` / `document` (mp3, m4a, wav, ogg, flac и т.д.)
- Автоопределение языка `ru / uk / en`, маты сохраняет дословно
- Предобработка `highpass + dynaudnorm + loudnorm` → чистый звук
- Чанкинг `>24MB` по 10 мин (лимит API 25MB)
- Семафор `2` параллельные задачи — не кладёт CPU
- 3 режима STT: `groq` → `api` → `local` с автофолбэком

## Быстрый старт (локально)

1. Создай бота в [@BotFather](https://t.me/BotFather) → `/newbot` → скопируй токен
2. Настрой окружение:
```bash
cp .env.example .env
nano .env  # вставь BOT_TOKEN и GROQ_API_KEY (бесплатно: console.groq.com/keys)
```
3. Установи зависимости:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
```
4. Запусти:
```bash
./start.sh        # setsid + лог bot.log
tail -f bot.log
./stop.sh
# или напрямую
.venv/bin/python bot.py
```

## Запуск через Docker

```bash
docker compose up -d --build   # сборка + запуск
docker ps | grep AudioDecoderBot
docker logs -f AudioDecoderBot
docker compose down            # остановить
```

Автозапуск после ребута уже настроен: `restart: unless-stopped` (`docker.service` enabled). Подробнее смотри `DOCKER.md:1`.

## Конфигурация `.env`

| Переменная | Пример | Описание |
|---|---|---|
| `BOT_TOKEN` | `123456:AAH...` | Токен от @BotFather (обязательно) |
| `GROQ_API_KEY` | `gsk_...` | Groq, бесплатно 14k запросов/день, ~2с на 60с |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Модель Groq |
| `OPENAI_API_KEY` | `sk-proj-...` | Альтернатива Groq, платно `$0.006/мин` |
| `STT_MODE` | `auto` | `auto` = groq > openai > local \| `groq` \| `api` \| `local` |
| `WHISPER_MODEL` | `small` | Локальный фолбэк: `tiny`/`base`/`small`/`medium` |
| `WHISPER_BEAM_SIZE` | `5` | 5 = макс качество, 1 = быстро |
| `WHISPER_CPU_THREADS` | `8` | Под i3-1315U |
| `MAX_FILE_SIZE_MB` | `20` | Лимит Bot API |

```ini
# .env.example:1
BOT_TOKEN=1234567890:AAH_твой_токен_от_BotFather
GROQ_API_KEY=gsk_...
GROQ_MODEL=whisper-large-v3-turbo
OPENAI_API_KEY=
STT_MODE=auto
WHISPER_MODEL=small
```

Приоритет режимов в `config.py:38` — `get_effective_mode()`: `groq` > `api` > `local`.

## Лимиты

- `20MB` на файл (Bot API) — ГС ~1ч, видео ~5мин
- `25MB` на wav (Groq) — режется автоматически на чанки по 10 мин
- Длительность >60 мин отклоняется

## Структура проекта

```
AudioDecoderBot/
├── bot.py              # aiogram хендлеры, семафор, предзагрузка модели
├── stt.py              # ffmpeg → Groq/OpenAI/local (единый чанкинг)
├── config.py           # .env + get_effective_mode()
├── requirements.txt    # зависимости
├── Dockerfile
├── docker-compose.yml
├── DOCKER.md           # доку по Docker
├── start.sh / stop.sh  # запуск без Docker
└── .env.example
```

- `bot.py:1` — точка входа, `handle_media`, `process_transcription`
- `stt.py:1` — `transcribe_file()`, `get_audio_duration()`, `get_local_model()`
- `config.py:1` — валидация токенов

## Команды бота

- `/start` — приветствие
- `/help` — справка + текущий режим `STT_MODE`

Просто пришли голосовое/кружок/видео — бот вернёт `📝 Текст:` (до 4096 символов на сообщение, длинные режет).

## Разработка

```bash
# проверить что всё игнорируется
git status

# логи
tail -f bot.log
docker logs --tail 50 AudioDecoderBot
```

## Лицензия

MIT — делай что хочешь, но без гарантий.
