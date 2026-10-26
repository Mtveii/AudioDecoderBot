#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ ! -f .env ]; then
  echo "❌ .env не найден. Скопируй из .env.example и вставь BOT_TOKEN"
  exit 1
fi
if [ ! -d .venv ]; then
  echo "❌ .venv не найден. Создай: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
# убить старый если висит
pkill -f "python.*(run|bot)\.py" 2>/dev/null || true
sleep 1
setsid .venv/bin/python run.py > bot.log 2>&1 < /dev/null &
echo "✅ Бот запущен PID $! | лог: bot.log"
echo "Проверь: tail -f bot.log"
echo "Остановить: ./stop.sh или pkill -f 'python.*run.py'"
