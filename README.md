# AudioDecoderBot — транскрибация голосовых и кружков в текст

Кинул ГС / кружок / видео → получаешь текст за 1-3 сек. Работает в личке и группах.

> Стек: `aiogram 3` + `faster-whisper` / `Groq` / `OpenAI` + `ffmpeg` + `Docker` + `Groq Chat` для интонаций

## Что умеет

- 🎤 `voice` (ogg) · 🔵 `video_note` (кружки) · 🎬 `video`/`audio`/`document` (mp3, m4a, wav, ogg, flac и т.д.)
- Автоопределение языка `ru / uk / en` + **форс любого языка** `99` штук через `/ru` `/en` `/de` ... (спасает на плохом аудио от латиницы)
- 🎭 **Интонация** `/ton` — `[спокойный тон] [радостный] [злой] [шёпот] [крик] [сарказм/ирония]` — интуитивно перед каждым предложением
- ✅ **Проверка** `/check` — перепроверяет логичность, маты сохраняет дословно, показывает только после
- Предобработка `highpass + dynaudnorm + loudnorm` → чистый звук
- Чанкинг `>24MB` по 10 мин (лимит API 25MB)
- Семафор `2` параллельные задачи — не кладёт CPU
- 3 режима STT: `groq` → `api` → `local` с автофолбэком + пер-язык прокидывается во все движки
- Per-user настройки (язык + тон) в `user_prefs.json` — переживают рестарт

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

## Команды бота

| Команда | Что делает | Пример |
|---------|------------|--------|
| `/start` | Приветствие | |
| `/help` | Справка + твой текущий язык/тон/режим STT | |
| `/ru`, `/en`, `/de`, `/fr`, `/ja`, `/zh` ... | Любой язык (99 шт.) — сразу перерасшифрует последнее ГС или то, на которое ответил | `/ru` → русский + ре-транскрибация |
| `/auto` | Сбросить в авто + перерасшифровать последнее/реплей | |
| `/ton` | Сразу перерасшифрует последнее ГС/ВС с интонацией (или то, на которое ответил), иначе вкл/выкл | `/ton` → 🎭 последнее ГС с `[тон]` |
| `/check` | Перепроверить логичность (маты дословно, только после) — тот же принцип: ответом на своё ГС/ВС или на 📝, иначе последнее | `/check` ответом на ГС → проверит именно его |

**Примеры:**

```bash
# Плохое аудио — латиница лезет, уже кинул ГС и получил латиницу
/ru               # → сразу перерасшифрует последнее ГС как русский (без латиницы)
/en               # то же для английского

# заранее выбрать язык для всех следующих ГС
/ru
# кинь новый войс → тоже точно кириллица
/auto            # сброс в авто

# Хочешь эмоции для последнего ГС (без реплая)
/ton              # → 🎭 [злой] Я же говорил не трогать мои вещи. (берёт последнее ГС/ВС)
# или ответом на своё ГС:
/ru или /ton ответом на ГС → перерасшифрует именно его

# Тоггл на будущее (когда нет ГС в чате)
/ton             # ✅ ВКЛ — теперь все новые ГС с интонацией
/ton             # ☑️ ВЫКЛ

# Перепроверка логичности (маты сохранит)
/check           # → ✅ Проверено: Привет, как дела? Всё хорошо, блять.
# или ответом на сообщение бота:
ответь на 📝 сообщением /check → проверит именно его
```

**Метки интонации:** `[спокойный тон]` `[радостный]` `[грустный]` `[злой]` `[взволнованный]` `[шёпот]` `[крик]` `[сарказм/ирония]` `[уверенный]` `[сомневающийся]` — ставятся перед каждым предложением, интуитивно понятно.

```text
Вход войса: "Привет, как дела? Всё отлично, давно не виделись!"
Выход без /ton: Привет, как дела? Всё отлично, давно не виделись!
Выход с /ton: [спокойный тон] Привет, как дела? [радостный] Всё отлично, давно не виделись!
```

## Конфигурация `.env`

| Переменная | Пример | Описание |
|---|---|---|
| `BOT_TOKEN` | `123456:AAH...` | Токен от @BotFather (обязательно) |
| `GROQ_API_KEY` | `gsk_...` | Groq, бесплатно 14k запросов/день, ~2с на 60с — и для STT и для Chat |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Модель STT Groq |
| `GROQ_CHAT_MODEL` | `openai/gpt-oss-20b` | Модель Chat для `/ton` и `/check` (`openai/gpt-oss-20b`/`llama-3.1-8b-instant`) |
| `OPENAI_API_KEY` | `sk-proj-...` | Альтернатива Groq, платно `$0.006/мин` |
| `STT_MODE` | `auto` | `auto` = groq > openai > local \| `groq` \| `api` \| `local` |
| `WHISPER_MODEL` | `small` | Локальный фолбэк: `tiny`/`base`/`small`/`medium` |
| `WHISPER_LANGUAGE` | `ru` | Глобальный форc языка (перекрывается per-user `/ru`) |
| `USER_PREFS_PATH` | `user_prefs.json` | Файл per-user настроек |
| `MAX_FILE_SIZE_MB` | `20` | Лимит Bot API |

```ini
# .env.example:1
BOT_TOKEN=1234567890:AAH_твой_токен_от_BotFather
GROQ_API_KEY=gsk_...
GROQ_MODEL=whisper-large-v3-turbo
GROQ_CHAT_MODEL=openai/gpt-oss-20b
OPENAI_API_KEY=
STT_MODE=auto
WHISPER_MODEL=small
USER_PREFS_PATH=user_prefs.json
```

Приоритет STT в `config.py:38` `get_effective_mode()`: `groq` > `api` > `local`. Язык берётся `per-user` из `prefs.py:30` (`/ru` → `set_user_lang`) → иначе `config.WHISPER_LANGUAGE` → иначе `auto`.

99 языков Whisper (`prefs.py:6`): `af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue`

## Как работает

1. `bot.py:214 process_transcription(lang_override)` — скачивает `bot.get_file` → `ffmpeg` → `stt.py:194 transcribe_file(lang_override)` → если `/ton` → `tone.py:40 annotate_tone()` → ответ `📝`/`🎭`. Кэш `bot.py:68 last_media_by_chat` + `last_text_by_chat` для `/ton`/`/ru`/`/check` (реплай приоритет). `checker.py:40 proofread()` — логичность, маты дословно, только после.
2. `stt.py:48 convert_to_wav()` — `highpass+dynaudnorm+loudnorm` 16kHz mono.
3. `prefs.py:40` — `user_prefs.json` атомарно `tmp+replace`, `threading.Lock`.
4. `tone.py:20` / `checker.py:20` — `SYSTEM_PROMPT` few-shot, `temperature 0.25/0.15`, фолбэк на `llama-3.1-8b-instant` / raw.

## Лимиты

- `20MB` на файл (Bot API) — ГС ~1ч, видео ~5мин
- `25MB` на wav (Groq) — режется автоматически на чанки по 10 мин (`stt.py:134`)
- Длительность >60 мин отклоняется
- Groq Chat `/ton`/`/check` +0.3–0.6с, `~$0.00004` на 1 запрос

## Структура проекта

```
AudioDecoderBot/
├── bot.py              # aiogram хендлеры, семафор, /ru /ton /check, process_transcription
├── stt.py              # ffmpeg → Groq/OpenAI/local (единый чанкинг, lang_override)
├── tone.py             # Groq Chat интонация annotate_tone()
├── checker.py          # Groq Chat проверка логичности proofread(), маты дословно
├── prefs.py            # per-user язык/тон, WHISPER_LANGS, user_prefs.json
├── config.py           # .env + get_effective_mode() + GROQ_CHAT_MODEL
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── DOCKER.md
├── start.sh / stop.sh
└── .env.example
```

- `bot.py:27 START_TEXT` / `40 get_help_text()` — приветствие + /check
- `bot.py:163 cmd_auto()` / `180 cmd_ton()` / `212 cmd_dynamic_lang()` / `~240 cmd_check()` — языки/тон/проверка
- `stt.py:87 transcribe_groq()` / `165 transcribe_local()` — прокинут `lang_override`
- `tone.py:40 annotate_tone()` / `checker.py:40 proofread()` — LLM разметка/проверка
- `prefs.py:30 set_user_lang()` / `toggle_tone()` — персист

## Разработка

```bash
# проверить что всё игнорируется
git status

# логи
tail -f bot.log
docker logs --tail 50 AudioDecoderBot

# тесты без Telegram
.venv/bin/python -m py_compile bot.py stt.py prefs.py tone.py checker.py
.venv/bin/python -c "import prefs; print(prefs.is_valid_lang('ru'))"
```

## Лицензия

MIT — делай что хочешь, но без гарантий.
