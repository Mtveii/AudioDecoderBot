# Docker — автозапуск без SSH

Контейнер уже запущен с `--restart unless-stopped` — переживёт ребут, не требует SSH.

## Управление

```bash
docker ps | grep ai-dehif_bot          # статус
docker logs -f ai-dehif_bot             # логи (как tail -f bot.log)
docker logs --tail 50 ai-dehif_bot
docker restart ai-dehif_bot             # перезапуск
docker stop ai-dehif_bot                # остановить
docker start ai-dehif_bot               # запустить
docker rm -f ai-dehif_bot               # удалить контейнер (образ останется)
```

## Обновление после правок

```bash
cd /home/matveii/TG_BOTS/ai-dehif_bot
docker build -t ai-dehif_bot:latest .
docker rm -f ai-dehif_bot
docker run -d --restart unless-stopped --name ai-dehif_bot --env-file .env ai-dehif_bot:latest
# или через compose:
docker compose up -d --build
```

## Автозапуск после ребута

- `docker.service` уже `enabled` (проверено `systemctl is-enabled docker` → enabled)
- `restart: unless-stopped` → при ребуте `dockerd` сам поднимет `ai-dehif_bot` без логина по SSH
- Проверка: `docker inspect ai-dehif_bot --format '{{.HostConfig.RestartPolicy.Name}}'` → `unless-stopped`

## Логи и .env

- `.env` не копируется в образ (`.dockerignore`), передаётся через `--env-file` — безопасно
- Логи ротируются `max-size 10m` (в compose), смотри `docker logs`
```

