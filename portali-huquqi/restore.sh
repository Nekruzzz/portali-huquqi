#!/bin/sh
# Restore the portal from a backup made by backup.sh (database, uploaded files, logo).
# Run from the folder that holds docker-compose.yml:   ./restore.sh backups/portal_2026-11-06_0230.tar.gz
# The current data is first saved to backups/before-restore_<time>.tar.gz, so a restore can be undone.
set -eu
cd "$(dirname "$0")"
[ $# -eq 1 ] && [ -f "$1" ] || { echo "Usage: ./restore.sh backups/portal_YYYY-MM-DD_HHMM.tar.gz"; exit 1; }
ARCHIVE=$1
tar tzf "$ARCHIVE" | grep -qx "portal.db" || { echo "Not a portal backup: $ARCHIVE has no portal.db"; exit 1; }

printf "Replace ALL current portal data with %s? Type yes: " "$ARCHIVE"
read -r answer
[ "$answer" = "yes" ] || { echo "Cancelled."; exit 1; }

STAMP=$(date +%Y-%m-%d_%H%M%S)
mkdir -p backups
echo "Saving the current data to backups/before-restore_$STAMP.tar.gz ..."
docker compose exec -T app python -c "
import sqlite3, tarfile
src = sqlite3.connect('/data/portal.db'); dst = sqlite3.connect('/data/backup.db')
src.backup(dst); dst.close(); src.close()
with tarfile.open('/data/backup.tar.gz', 'w:gz') as t:
    t.add('/data/backup.db', 'portal.db')
    for d in ('uploads', 'branding'):
        t.add('/data/' + d, d)
"
docker compose cp app:/data/backup.tar.gz "backups/before-restore_$STAMP.tar.gz"
docker compose exec -T app rm -f /data/backup.db /data/backup.tar.gz

echo "Restoring ..."
docker compose stop scheduler
docker compose cp "$ARCHIVE" app:/data/restore.tar.gz
docker compose exec -T app python -c "
import os, shutil, sqlite3, tarfile
os.makedirs('/data/restore', exist_ok=True)
with tarfile.open('/data/restore.tar.gz') as t:
    t.extractall('/data/restore', filter='data')
check = sqlite3.connect('/data/restore/portal.db')
assert check.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'backup database is damaged'
n = check.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
check.close()
# copy the database through SQLite so that the running app sees a consistent file
src = sqlite3.connect('/data/restore/portal.db'); dst = sqlite3.connect('/data/portal.db')
src.backup(dst); dst.close(); src.close()
for d in ('uploads', 'branding'):
    shutil.rmtree('/data/' + d, ignore_errors=True)
    shutil.copytree('/data/restore/' + d, '/data/' + d)
shutil.rmtree('/data/restore'); os.remove('/data/restore.tar.gz')
print('documents restored:', n)
"
docker compose restart app
docker compose start scheduler
echo "$(date '+%F %T') restored from $ARCHIVE (previous data: backups/before-restore_$STAMP.tar.gz)"
