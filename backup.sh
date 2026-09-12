#!/bin/bash
# Бэкап usage.db + user_prefs.json + media (ты чистишь сам, keep 7)
set -e
DATE=$(date +%Y%m%d_%H%M)
BACKUP_DIR="backup"
mkdir -p "$BACKUP_DIR/media" 2>/dev/null || true
echo "📦 Backup $DATE"
# db + prefs
tar -czf "$BACKUP_DIR/backup_${DATE}.tar.gz" usage.db user_prefs.json 2>/dev/null || tar -czf "$BACKUP_DIR/backup_${DATE}.tar.gz" --ignore-failed-read usage.db user_prefs.json 2>/dev/null || echo "no db yet"
# media отдельно (может быть большим) - только если есть
if [ -d "media" ] && [ "$(ls -A media 2>/dev/null)" ]; then
  tar -czf "$BACKUP_DIR/media_${DATE}.tar.gz" media 2>/dev/null || echo "media backup skip"
  echo "media backup done"
fi
# keep 7
ls -t "$BACKUP_DIR"/backup_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -- 2>/dev/null || true
ls -t "$BACKUP_DIR"/media_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -- 2>/dev/null || true
echo "✅ Backup done: $BACKUP_DIR/backup_${DATE}.tar.gz"
ls -lh "$BACKUP_DIR" | tail -10
