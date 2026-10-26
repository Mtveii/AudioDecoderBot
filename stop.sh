#!/bin/bash
pkill -f "python.*(run|bot)\.py" && echo "🛑 Бот остановлен" || echo "Бот не был запущен"
pgrep -af "python.*(run|bot)\.py" || true
