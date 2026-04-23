# MNEMOS Backup Scaffolding

Generic backup templates that any operator can adapt. Three layers:

1. **Per-host push** — systemd timer fires `mnemos-backup-daily.sh` on
   each Linux host; the script rsyncs configured directories (and
   optionally `pg_dump`s configured databases) to a central target.
2. **Central retention** — `retention.sh` runs on the backup target and
   thins out old `pg_dump` files to daily/weekly/monthly snapshots.
3. **Off-site** — the backup target itself should be replicated
   off-site; see "Off-site replication" below.

Nothing in this directory is fleet-specific; each host reads
`/etc/mnemos-backup.conf` for its own list of paths and databases.

---

## Installation (per Linux host)

```bash
# 1. copy the script to a privileged path
sudo install -m 0755 tools/backup/mnemos-backup-daily.sh \
    /usr/local/bin/mnemos-backup-daily.sh

# 2. copy the config example and customize
sudo install -m 0644 tools/backup/mnemos-backup.conf.example \
    /etc/mnemos-backup.conf
sudo editor /etc/mnemos-backup.conf

# 3. install systemd units
sudo install -m 0644 tools/backup/mnemos-backup-daily.service \
    /etc/systemd/system/
sudo install -m 0644 tools/backup/mnemos-backup-daily.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload

# 4. enable and start
sudo systemctl enable --now mnemos-backup-daily.timer

# 5. verify
systemctl list-timers mnemos-backup-daily.timer
journalctl -u mnemos-backup-daily.service --since -1h
```

The timer defaults to **03:00 local with up to 30-minute random delay**.
Override per-host to stagger multiple backups:

```bash
sudo systemctl edit mnemos-backup-daily.timer
# drop-in contents:
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:15:00
```

### Dry run

```bash
sudo MNEMOS_BACKUP_CONFIG=/etc/mnemos-backup.conf \
    /usr/local/bin/mnemos-backup-daily.sh
```

---

## Per-host configuration

### Hosts with Postgres (MNEMOS API nodes)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/home/jasonperlow/.claude:/etc/mnemos:/opt/mnemos
INCLUDE_PG=true
PG_DATABASES=mnemos
```

For **container-hosted Postgres** (e.g. `mnemos-prod-pg` on CERBERUS):

```conf
INCLUDE_PG=true
PG_DATABASES=mnemos
PG_CONTAINER=mnemos-prod-pg
PG_USER=mnemos
```

The container user (`jasonperlow` in the default service unit) needs
to be in the `docker` group so `docker exec` works from the timer.

### Hosts without Postgres (agent / edge nodes)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/home/jasonperlow/.claude:/home/jasonperlow/src
INCLUDE_PG=false
```

### Read-mostly edge (Pi, Jetson)

```conf
BACKUP_TARGET=/mnt/argonas/datapool/backups
BACKUP_DIRECTORIES=/etc/wpa_supplicant:/boot/extlinux.conf:/home/pi/configs
INCLUDE_PG=false
```

Weekly is usually enough for edge devices:

```bash
sudo systemctl edit mnemos-backup-daily.timer
[Timer]
OnCalendar=
OnCalendar=Sun 04:00
```

---

## Retention (run on the backup target)

Copy `retention.sh` to `/usr/local/bin/mnemos-backup-retention.sh` on
ARGONAS (or whatever host has write access to the backup target) and
schedule it with a systemd timer or cron:

```bash
# Example: nightly at 05:00 UTC
0 5 * * * /usr/local/bin/mnemos-backup-retention.sh >> /var/log/mnemos-retention.log 2>&1
```

Retention defaults:
- Keep all `pg_dump`s for 7 days
- Keep one per ISO week for 4 prior weeks
- Keep one per calendar month for 12 prior months
- Delete older

`fs/` rsync trees are not rotated — they're mirrored in place. Use ZFS
snapshots on the backup dataset if you want historical filesystem
views.

---

## Mac Time Machine (STUDIO, ULTRA)

Macs get **two Time Machine destinations** — local USB (already
configured) plus an SMB network share on ARGONAS:

1. Ensure ARGONAS exports `/mnt/datapool/timemachine` over SMB. TrueNAS:
   Storage → Shares → SMB → confirm `timemachine` share exists with
   "Time Machine" preset enabled.
2. On the Mac: **System Settings → General → Time Machine → + →
   Other ARGONAS shares**. Pick `timemachine`. Enter credentials.
3. Time Machine automatically rotates between both disks. Each disk
   gets its own full retention (24 hourly / 7 daily / 4 weekly).

No scripts needed — this is a native macOS feature.

---

## Off-site replication (ARGONAS itself)

Everything backs up *to* ARGONAS. If ARGONAS fails, you lose
everything. Use whatever cloud storage you already pay for; the
format below works with any rclone remote.

**Important — Google Drive and other per-file-API stores:** uploading
a tree of many small files is *glacial* (one API call per file, rate
limited to ~3/sec). Always tar the backup tree into a single archive
before uploading. This section assumes that pattern.

### Option A — Google Drive via TrueNAS Cloud Sync (recommended)

Zero marginal cost if you already have a Google One plan (5 TB is
more than enough for the full ARGONAS backup corpus for years).

**Step 1 — ARGONAS-side nightly tar script.** Run after the daily
retention sweep. Outputs one encrypted tarball per day into a staging
dir that Cloud Sync watches.

```bash
#!/bin/bash
# /usr/local/bin/mnemos-offsite-pack.sh
set -u -o pipefail
STAGE=/mnt/datapool/offsite-stage
STAMP=$(date -u +%Y-%m-%d)
PASS_FILE=/root/.mnemos-offsite.passphrase   # chmod 600

mkdir -p "$STAGE"
tar -cf - /mnt/datapool/backups /mnt/datapool/git \
    | gpg --batch --yes --symmetric --cipher-algo AES256 \
          --passphrase-file "$PASS_FILE" \
    > "$STAGE/mnemos-offsite-${STAMP}.tar.gpg"

# Keep last 14 local copies (Cloud Sync uploads from here)
ls -1t "$STAGE"/mnemos-offsite-*.tar.gpg | tail -n +15 | xargs -r rm -f
```

Schedule via systemd timer or cron, 05:00 daily (after the retention
sweep at 04:00).

**Step 2 — TrueNAS Cloud Sync task:**
1. **Credentials → Backup Credentials → Cloud Credentials → Add**
   - Provider: Google Drive
   - Authenticate: opens OAuth flow in browser; sign in with the
     Google account whose quota you want to use
2. **Data Protection → Cloud Sync Tasks → Add**
   - Direction: PUSH
   - Transfer mode: SYNC (mirrors staging dir → Drive)
   - Source: `/mnt/datapool/offsite-stage/`
   - Remote path: `mnemos-offsite/` (auto-creates on Drive)
   - Schedule: daily 06:00
   - Encryption: unnecessary (GPG already handled it at tar time)
   - Remote Encryption (rclone `crypt`): **disable** — tarball is
     already encrypted, double-encrypting wastes CPU

**Step 3 — verify restore path.** Pick a random tarball from Drive,
download, `gpg --decrypt`, `tar -tf` to list contents. Do this on a
host that doesn't share a passphrase filesystem with ARGONAS — if
ARGONAS dies and takes the passphrase with it, you're locked out.
Store the GPG passphrase in a password manager (1Password, etc.).

### Option B — rclone on ARGOS (fallback if TrueNAS UI is unavailable)

ARGOS already has `/mnt/argonas/datapool` NFS-mounted, so it can read
the tarball staging dir. rclone handles Google Drive natively.

```bash
# One-time setup (interactive OAuth via SSH port-forward)
sudo apt install -y rclone
rclone config        # add "gdrive" remote, follow prompts
# Test
rclone ls gdrive:mnemos-offsite/

# Scheduled (systemd timer or cron, 06:00 daily)
rclone sync /mnt/argonas/datapool/offsite-stage gdrive:mnemos-offsite \
    --log-file /var/log/rclone-mnemos.log --log-level INFO
```

### Option C — Second NAS via `zfs send | receive`

If you have a second physical box, periodic snapshot replication is
free and faster than cloud:

```bash
# on the source NAS (ARGONAS)
zfs snapshot -r datapool/backups@$(date -u +%Y%m%d-%H%M%S)
zfs send -i datapool/backups@<prev> datapool/backups@<new> \
    | ssh root@backup-box zfs receive datapool/argonas-backups
```

### Option D — External USB rotated weekly

Cheapest manual fallback. Plug in disk, `zfs send` snapshot, unplug,
take off-site, repeat with a second disk next week.

---

## Recovery drills

A backup you haven't restored from is a backup you don't have. At
minimum, once per quarter:

1. Pick a random `pg_dump` file from ARGONAS.
2. Spin up a throwaway Postgres container (`pgvector/pgvector:pg17`).
3. `pg_restore` into it.
4. Verify row counts match the production DB as of the dump timestamp.
5. Delete the throwaway.

Document the last successful restore drill in the ARGONAS backup
directory's README so the next operator can see when the coverage was
last proven.

---

## Monitoring

Each run writes `$BACKUP_TARGET/<hostname>/last-backup.json` with a
timestamp. A simple monitor:

```bash
# Alert if any host's last-backup.json is stale (>36h old)
find /mnt/argonas/datapool/backups -name last-backup.json -mmin +2160 -print
```

Wire that to your alerting channel of choice.
