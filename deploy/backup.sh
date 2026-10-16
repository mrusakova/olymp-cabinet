#!/bin/sh
# Ежедневная копия базы кабинета; хранятся последние 14 дней.
# Запускается из cron (см. deploy/cron), вручную: sh deploy/backup.sh
set -e

cd "$(dirname "$0")/.."
DEST=/var/backups/olymp-cabinet
mkdir -p "$DEST"
chmod 700 "$DEST"

. ./.env
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  | gzip > "$DEST/olymp-$(date +%F).sql.gz"

find "$DEST" -name 'olymp-*.sql.gz' -mtime +14 -delete
