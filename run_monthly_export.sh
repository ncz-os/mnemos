#!/bin/bash
source /opt/mnemos/.env
cd /opt/mnemos

LOG_FILE=~/.mnemos/backups/export.log

echo "[Thu Feb  5 14:55:03 EST 2026] Starting monthly export" >> $LOG_FILE

python3 export_to_json_shards.py >> $LOG_FILE 2>&1

# Backup to ARGONAS NFS
if [ -d /mnt/argonas/backups ]; then
    mkdir -p /mnt/argonas/backups/mnemos_json_shards_$(date +%Y%m%d)
    cp -r ~/.mnemos/backups/mnemos_backup_*.json /mnt/argonas/backups/mnemos_json_shards_$(date +%Y%m%d)/ 2>/dev/null || true
    echo "[Thu Feb  5 14:55:03 EST 2026] Backed up to ARGONAS" >> $LOG_FILE
fi

echo "[Thu Feb  5 14:55:03 EST 2026] Monthly export completed" >> $LOG_FILE
