#!/bin/bash
pkill -f "python.*bot.py" && echo "🛑 Бот остановлен" || echo "Бот не был запущен"
ps aux | grep -v grep | grep bot || true
