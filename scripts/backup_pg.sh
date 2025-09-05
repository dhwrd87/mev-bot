#!/usr/bin/env bash
set -euo pipefail
STAMP=$(date +%Y%m%d-%H%M%S)
docker exec -i mev-db pg_dump -U mev_user -d mev_bot | gzip > /mnt/nas/mev/backups/pgdump-$STAMP.sql.gz
find /mnt/nas/mev/backups -type f -name 'pgdump-*.sql.gz' -mtime +14 -delete
