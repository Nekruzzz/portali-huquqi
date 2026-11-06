#!/bin/sh
# Daily backup of the portal: a consistent copy of the database plus uploaded files and the logo.
# Run from the folder that holds docker-compose.yml:   ./backup.sh
# Cron (every night at 02:30):   30 2 * * * cd /opt/portal/portali-huquqi && ./backup.sh >> backups/backup.log 2>&1
# Keeps the last 14 days in ./backups. Copy that folder off the server regularly.
set -eu
cd "$(dirname "$0")"
STAMP=$(date +%Y-%m-%d_%H%M)
mkdir -p backups

docker compose exec -T app python -c "
import sqlite3, tarfile
src = sqlite3.connect('/data/portal.db'); dst = sqlite3.connect('/data/backup.db')
src.backup(dst); dst.close(); src.close()
with tarfile.open('/data/backup.tar.gz', 'w:gz') as t:
    t.add('/data/backup.db', 'portal.db')
    for d in ('uploads', 'branding'):
        t.add('/data/' + d, d)
"
docker compose cp app:/data/backup.tar.gz "backups/portal_$STAMP.tar.gz"
docker compose exec -T app rm -f /data/backup.db /data/backup.tar.gz

find backups -name 'portal_*.tar.gz' -mtime +14 -delete
echo "$(date '+%F %T') ok backups/portal_$STAMP.tar.gz $(du -h "backups/portal_$STAMP.tar.gz" | cut -f1)"
