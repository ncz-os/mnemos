# STYX — daily encrypted MNEMOS backup, off-fleet

STYX is mnemos-core's fail-safe backup feature: once a day, per host, it exports
that host's own MNEMOS store, encrypts it, uploads it to a destination of
your choice, and prunes old copies to a grandfather-father-son retention
schedule. It exists because the fleet's previous generic NFS backup job
failed *silently* for five weeks — STYX is built so a failure is loud
instead: it exits non-zero on any failed integrity gate and never reports a
partial success (see `mnemos/tools/styx/__main__.py`'s module docstring).

Code: `mnemos/tools/styx/` (`config.py`, `crypto.py`, `destination.py`,
`drive.py`, `errors.py`, `retention.py`, `runner.py`, `source.py`). Deploy
assets: `deploy/styx/{mnemos-styx.service,mnemos-styx.timer,styx.env.example}`.
CI-green (unit-tested with mocked destinations/age/retention fakes —
`tests/test_styx_*.py`), and **proven end-to-end in production** on the
fleet's authoritative Oracle-backed instance (PYTHIA) against both the `r2`
and `rclone` destinations with a real, complete 18,651-record export — see
"Status as of 2026-09-18" below.

## Design, in short

- **Per-host, not centralized.** Each host runs its own STYX against its own
  local store. A single centralized puller was explicitly rejected — it has
  the same failure shape as a past incident where one central job failed
  silently and nobody caught it for weeks. Per-host means one broken host
  loses one host's backups, not the fleet's.
- **Asymmetric encryption (`age`).** The bundle is encrypted to a public
  `age1...` recipient. The private identity that can decrypt it is generated
  **offline, by a human, off the fleet** — STYX's own config validation
  (`mnemos/tools/styx/config.py::StyxConfig.__post_init__`) refuses to start
  if handed a private `AGE-SECRET-KEY-1...` value where a recipient is
  expected. This is deliberate: a total fleet loss must not also take away
  the ability to read the backups.
- **Whole-bundle encryption**, not per-field.
- **Grandfather-father-son retention**: 14 daily / 8 weekly / 12 monthly
  (`DEFAULT_RETAIN_*` in `config.py`).
- **Success is gated on all of**: record floor met, non-empty archive, an
  `age` header present in the ciphertext, size floor met, AND a post-upload
  re-read from the destination confirming the uploaded object's size+digest
  match before any pruning happens. Only then does the receipt report `ok`.
  Every destination (Drive, R2, rclone) implements this re-read honestly —
  see `mnemos/tools/styx/destination.py`'s module docstring for why each one
  can.

## Choosing a destination

`MNEMOS_STYX_DESTINATION` selects one of three, each with its own
`mnemos/tools/styx/` module. Pick whichever matches your actual setup —
none is "more correct" than another, the tradeoff is setup ceremony vs.
credential isolation:

| Destination | `MNEMOS_STYX_DESTINATION` | Setup | Good fit |
|---|---|---|---|
| Cloudflare R2 | `r2` | One scoped API token + one bucket, no OAuth | You already use Cloudflare (see prerequisite 2a) — the lowest-ceremony option |
| Personal Google Drive via rclone | `rclone` | `rclone config`, once, interactively | You want a plain personal Drive account and don't want this process to ever hold a Drive credential at all |
| Google Drive, GCP service account | `gdrive-service-account` (default) | GCP Console: enable API, create service account, download key, share a folder | You want a fully independent per-host identity with IAM-manageable revocation, e.g. for a business/managed Google Workspace deployment |

The default is `gdrive-service-account` so an already-deployed host's config
keeps working unchanged if you don't set `MNEMOS_STYX_DESTINATION` at all.

## Prerequisites — one or two things only a human can supply

Every destination needs the `age` keypair (prerequisite 1). Which of the
other two you also need depends on which destination you picked above.

### 1. An `age` keypair, generated offline (needed for every destination)

```
python -m mnemos.tools.styx keygen
```

Run this **off the fleet**, ideally air-gapped. It prints:
- a **private identity** (`AGE-SECRET-KEY-1...`) — store it in at least two
  physically separate places the fleet cannot reach (a password manager not
  synced to any fleet host, a printed copy in a safe, etc.). This is the
  only thing that can ever decrypt a STYX backup. It must never be written
  to a fleet host, a repo, or any destination.
- a **public recipient** (`age1...`) — safe to commit, safe to deploy to
  every host. This is the value that goes into every host's
  `MNEMOS_STYX_AGE_RECIPIENT`.

Run this once for the whole fleet (one keypair, deployed as the same public
recipient everywhere) unless you have a specific reason to want per-host
decryption boundaries.

### 2a. Cloudflare R2 (for `MNEMOS_STYX_DESTINATION=r2`)

No OAuth flow at all — a scoped API token is the whole setup. In the
Cloudflare dashboard:

1. **R2 → Create bucket.** Give it a name dedicated to STYX backups, e.g.
   `mnemos-styx-backups`. **Do not reuse a bucket that already serves
   another purpose** (this fleet has an existing `ncz-apt` bucket for its
   apt repo — that one is off-limits for STYX; a shared bucket makes it
   easy for an unrelated cleanup of one purpose to prune the other).
2. **R2 → Manage API tokens → Create API token.** Scope it to **Object
   Read & Write** on the one bucket from step 1 only — not account-wide.
   This gives you an access key id + secret access key pair.
3. Note your **Cloudflare account id** (shown on the R2 overview page, or
   in the dashboard URL) — this is `MNEMOS_STYX_R2_ACCOUNT_ID`.

That's it — three values (`MNEMOS_STYX_R2_ACCOUNT_ID`,
`MNEMOS_STYX_R2_BUCKET`, plus the access key id/secret pair) and you're
done. No consent screen, no service-account JSON to distribute.

### 2b. rclone remote (for `MNEMOS_STYX_DESTINATION=rclone`)

rclone owns the entire OAuth flow and token storage for a personal Google
Drive account — this process never sees, stores, or can leak that
credential. One-time setup, on whichever host will actually run STYX (the
consent flow needs a browser, so either run this on a machine with one and
then copy `~/.config/rclone/rclone.conf` to the fleet host, or use
`rclone authorize` for a headless flow):

```
rclone config
```

Walk through: `n` (new remote) → name it (e.g. `gdrive-personal`) → storage
type `drive` (Google Drive) → leave client id/secret blank to use rclone's
own → scope `drive.file` if offered (same narrow-scope reasoning as the
service-account path: it only ever sees what it created) → complete the
browser consent flow when prompted → `n` to "configure as team drive" unless
you actually want that.

Then create a folder for backups in that Drive account (via the web UI or
`rclone mkdir gdrive-personal:mnemos-backups`) and set
`MNEMOS_STYX_RCLONE_REMOTE=gdrive-personal:mnemos-backups`.

### 2c. Google Cloud service-account credential (for `MNEMOS_STYX_DESTINATION=gdrive-service-account`, the default)

STYX only supports service-account auth (`google.oauth2.service_account`,
scope `drive.file` — see `mnemos/tools/styx/drive.py`), not an interactive
OAuth2 consent flow. `drive.file` is deliberately narrow: it only grants
access to files the service account itself created, not your whole Drive.

Steps (Google Cloud Console, one-time):

1. Create or pick a GCP project.
2. **APIs & Services → Library** — enable the **Google Drive API**.
3. **APIs & Services → Credentials → Create Credentials → Service account.**
   Name it something like `mnemos-styx-backup`. No roles need to be granted
   at the project/IAM level — Drive access is granted later via the folder
   share, not IAM.
4. Open the new service account → **Keys → Add key → Create new key → JSON.**
   This downloads the credential file STYX needs
   (`MNEMOS_STYX_GDRIVE_CREDENTIALS_JSON`). Treat it as a secret — it's a
   long-lived bearer credential for that service account's Drive access.
5. In Google Drive (your own account), create a folder to hold backups
   (e.g. "MNEMOS STYX Backups"). Share that folder with the service
   account's email address (`...@<project>.iam.gserviceaccount.com`, found
   on the service account's detail page), Editor access.
6. Get the folder's id from its URL
   (`https://drive.google.com/drive/folders/<THIS PART>`) — this is
   `MNEMOS_STYX_GDRIVE_FOLDER_ID`.

A service account has no meaningful "My Drive" of its own, which is why the
folder-share step is required rather than optional — an upload with no
explicit destination folder is the silent-failure mode again, wearing a
different hat (see `config.py`'s comment on `ENV_GDRIVE_FOLDER_ID`). This is
real setup overhead compared to 2a/2b — reach for it specifically when you
want an independently revocable, IAM-manageable per-host identity (e.g. a
managed Google Workspace deployment), not by default.

## Install (per host)

1. Copy `deploy/styx/styx.env.example` to `/etc/mnemos/styx.env`
   (`chown root:mnemos`, `chmod 0640`) and fill in:
   - `MNEMOS_STYX_DESTINATION` — `r2`, `rclone`, or leave unset for the
     default `gdrive-service-account`.
   - The destination-specific values from whichever prerequisite (2a/2b/2c)
     you completed above.
   - `MNEMOS_STYX_AGE_RECIPIENT` — the `age1...` public recipient from
     prerequisite 1.
   - `MNEMOS_STYX_SOURCE_MODE` + the matching path/endpoint: `backend` +
     `MNEMOS_STYX_SQLITE_PATH` for a SQLite host (cerberus/proteus/achilles);
     `http` + `MNEMOS_STYX_HTTP_ENDPOINT=http://127.0.0.1:5002/v1/export` +
     `MNEMOS_STYX_HTTP_TOKEN` for a pooled backend such as Oracle.
   - Raise `MNEMOS_STYX_MIN_RECORDS` close to the real record count on the
     authoritative host so a truncated export fails loud instead of
     looking like a small-but-valid backup.
2. Install the units:
   ```
   sudo cp deploy/styx/mnemos-styx.service deploy/styx/mnemos-styx.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mnemos-styx.timer
   ```
3. **First run: dry-run it.** Set `MNEMOS_STYX_DRY_RUN=1` in `styx.env`,
   then `sudo systemctl start mnemos-styx.service && journalctl -u
   mnemos-styx.service -f`. This exercises export + encrypt without touching
   the destination or pruning anything. Once it reports `ok`, remove the
   dry-run flag and run it for real once by hand before trusting the daily
   timer: `sudo -u mnemos python -m mnemos.tools.styx backup`.

## Operating it

- **Check what the destination holds for this host**: `python -m
  mnemos.tools.styx verify` — lists what would be kept vs pruned under the
  current retention settings without changing anything.
- **Restore**: `age -d -i <identity-file> backup.mif.tar.gz.age | tar xzf -`
  then import the resulting MIF bundle the normal way
  according to its source mode. `backend` mode produces a MIF bundle for
  `mnemos.portability.charon.import_bundle_to_backend`; `http` mode produces
  an MPF envelope for `python -m mnemos.tools.memory_import json`. Use
  `preserve_owner=true`/`--preserve-metadata` only for an authorized same-owner
  administrative restore.
- **Alerting**: `mnemos-styx.service`'s `OnFailure=status-email@%n.service`
  is a placeholder — wire it to whatever this fleet actually uses for
  alerting before relying on the timer unattended.

## Running multiple destinations from one host

STYX's own service/timer pair (above) assumes one destination per host, a
native venv install, and `User=mnemos` reading the store directly. PYTHIA
runs `mnemos-api` as a container instead, and deliberately backs up to
**both** `r2` and `rclone` in parallel rather than picking one — belt and
suspenders for the fleet's single authoritative Oracle-backed instance. The
pattern that took (two independent env files, two independent
service/timer pairs, staggered so they don't contend for the same export):

- `/etc/mnemos/styx-r2.env` and `/etc/mnemos/styx-rclone.env` — same keys as
  `styx.env.example`, one file per destination, `root:root 0600`.
- `mnemos-styx-r2.service` / `.timer` (02:40 UTC) and
  `mnemos-styx-rclone.service` / `.timer` (03:10 UTC) — each `Type=oneshot`,
  `EnvironmentFile=` pointing at its own env file, `Persistent=true`,
  `RandomizedDelaySec=300`, `After=`/`Requisite=mnemos-api.service`.
- Instead of a native `ExecStart`, each service runs STYX **inside the
  running container**: `ExecStart=/usr/bin/podman exec -e VAR1 -e VAR2 ...
  mnemos-api python3 -m mnemos.tools.styx backup` — every `MNEMOS_STYX_*`
  variable the destination needs is named with a bare `-e VARNAME` (no
  value), so `podman exec` forwards it from the unit's own
  `EnvironmentFile=` into the container rather than needing it duplicated
  in the quadlet's own `Environment=` lines.
- For the `rclone` destination specifically, the container also needs
  `rclone.conf` itself: the quadlet mounts
  `/etc/mnemos/rclone.conf:/root/.config/rclone/rclone.conf:ro`
  (`Volume=` line in `mnemos-api.container`) so the containerized `rclone`
  binary can see the already-authorized remote without the OAuth token ever
  being baked into the image.

This is a per-host customization, not a repo-shipped template — build it
from `styx.env.example` + the generic unit above, split per destination as
shown, when a host needs more than one destination or runs its MNEMOS
instance in a container.

## Status as of 2026-09-18

STYX ships in every base `mnemos-core` install (`boto3`, `pyrage`, and
pinned `rclone` binaries for amd64/arm64 are unconditional
`mnemos-enterprise` image dependencies, not optional extras — the whole
mnemos-charon repo, including STYX, was merged into mnemos-core as
first-party code this date; docling ingestion is the one part that stayed
optional). Its HTTP source consumes the API's paged streaming frames via
the export route's keyset cursor, verifies the completion marker and
declared record total, and materializes a restorable MPF envelope before
encryption — an earlier version of this source miscounted NDJSON protocol
frames as individual records, which looked like data truncation; that is
fixed.

**Proven end-to-end in production, not just CI-green.** On PYTHIA (the
fleet's authoritative Oracle-backed instance) STYX ran against real,
complete data — 18,651 records, not a truncated test — to both `r2` and
`rclone`/Google Drive simultaneously (see "Running multiple destinations
from one host" above). Both destinations were independently verified
outside STYX itself: R2 via direct S3 `ListObjects` (size + ETag matched),
Drive via `rclone lsl` (size matched). Both destinations' retention logic
correctly pruned earlier partial-test artifacts once the real backup
landed. Daily timers are installed and armed on PYTHIA:
`mnemos-styx-r2.timer` (02:40 UTC) and `mnemos-styx-rclone.timer` (03:10
UTC). The `age` keypair for PYTHIA's backups was generated off-fleet per
the design above; only the public recipient exists anywhere on the fleet.

**Not yet done**: cerberus/proteus/achilles (the SQLite-backed satellites)
don't have their own STYX timers yet — only PYTHIA's authoritative instance
is backed up on a schedule so far. `OnFailure=status-email@%n.service` in
the generic unit is still a placeholder pending real alerting wiring.
rclone's shared `client_id` deprecation (retiring during 2026) is a known,
non-urgent follow-up — re-run the `rclone config` ceremony with a
dedicated client id before then.
