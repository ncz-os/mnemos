# STYX — off-fleet MNEMOS backup deployment

STYX exports a host's MNEMOS data, encrypts it to an `age` recipient, uploads
it to Cloudflare R2, rclone, or Google Drive, verifies the bytes that landed,
and prunes old artifacts on a grandfather-father-son schedule. See
[`docs/STYX.md`](../../docs/STYX.md) for destination-specific setup.

## Why it is shaped this way

**Per-host, not a central puller.** Every MNEMOS host backs up its own data
on its own timer. A central puller would need reachability and credentials to
every instance — and it is structurally the same thing as the NFS backup job
on this fleet that reported success while writing nothing for five weeks: one
process, one schedule, one place for a silent failure to hide. Independent
per-host runs fail visibly and separately.

When an instance is down at backup time the result is a **missing** backup
that shows up as a failed unit, not a **stale** one that a puller quietly
reported as fine. Missing-and-alerted beats present-and-wrong.

**Complete, therefore encrypted.** A backup that omits the operator's own
credentials is not a backup, so STYX exports with the vault namespace
included. That means the bundle holds live secrets before it reaches Drive,
and client-side encryption stops being optional.

**Asymmetric, private half off the fleet.** The disaster STYX exists for is
total fleet loss. A symmetric key would have to live on the fleet to encrypt
and would burn with it, leaving Drive full of undecryptable noise on exactly
the day it mattered. STYX only ever holds the **public** recipient; the
identity that can decrypt is generated once by the operator and never touches
a fleet host. STYX refuses to start if it is handed a private identity.

**Whole-bundle, not just the vault.** Encrypting only the vault namespace
would leave ~94% browsable without the key, which is a real convenience — but
it depends on having perfectly enumerated where secrets can land. That
enumeration was wrong once already (secrets are auto-vaulted *on ingest*, and
`verbatim_content` shipped a plaintext credential that the concept layer
never emits). One encryption boundary cannot be got wrong by
misclassification. Backups are for emergencies, not browsing.

## One-time key ceremony

Run this **off the fleet** — your laptop, ideally air-gapped. Never on a host
STYX runs on.

```bash
python -m mnemos.tools.styx keygen
```

It prints two things:

- **`AGE-SECRET-KEY-1...`** — the private identity. The *only* thing that can
  ever decrypt a STYX backup. Store it in at least two physically separate
  places the fleet cannot reach (a password manager entry plus a paper or
  metal copy in a safe). If you lose it, every backup in Drive is noise.
- **`age1...`** — the public recipient. Safe to commit, safe to deploy
  everywhere. This is what goes in `styx.env`.

Test the round trip **before** trusting it:

```bash
echo canary | age -e -r age1YOUR_RECIPIENT | age -d -i identity.txt
```

If that does not print `canary`, stop and fix it now — not during a restore.

## Destination setup

Choose `r2`, `rclone`, or `gdrive-service-account` and follow the exact setup
in [`docs/STYX.md`](../../docs/STYX.md). The enterprise container includes a
pinned rclone binary; bare-metal installs must provide the system binary when
the rclone destination is selected.

## Install on a host

```bash
install -m 0640 -o root -g mnemos styx.env.example /etc/mnemos/styx.env
$EDITOR /etc/mnemos/styx.env          # folder id, recipient, source mode
install -m 0644 mnemos-styx.service mnemos-styx.timer /etc/systemd/system/
systemctl daemon-reload

# Prove the pipeline works before arming the timer. Exports and encrypts,
# uploads nothing.
MNEMOS_STYX_DRY_RUN=1 /opt/mnemos/venv/bin/python -m mnemos.tools.styx backup

systemctl enable --now mnemos-styx.timer
systemctl list-timers mnemos-styx.timer
```

## Restore

A STYX artifact is an ordinary `age` file wrapping a gzipped tar. Decrypt and
extract it first:

```bash
age -d -i identity.txt mnemos-pythia-20260917T024000Z.mif.tar.gz.age \
  | tar xzf - -C ./restored

find restored -maxdepth 2 -type f
```

For `MNEMOS_STYX_SOURCE_MODE=backend`, the archive contains a MIF bundle:

```python
from mnemos.portability import charon
await charon.import_bundle_to_backend(backend, "./restored", redact_vault=False)
```

`mif-manifest.json` records `vault_included` and `vault_redacted`, so you can
tell a complete backup from one that dropped the vault before you rely on it.

For `MNEMOS_STYX_SOURCE_MODE=http`, the archive contains `export.mpf.json`.
Restore it with:

```bash
python -m mnemos.tools.memory_import json \
  --file ./restored/export.mpf.json --preserve-metadata \
  --endpoint http://target:5002 --api-key "$MNEMOS_API_TOKEN"
```

## What makes a run "successful"

STYX exits 0 only when **all** of these passed:

1. the export produced at least `MNEMOS_STYX_MIN_RECORDS` records;
2. the archive is non-empty;
3. the encrypted artifact starts with the `age` header — proof we are not
   about to upload the plaintext bundle full of live credentials;
4. the artifact is at least `MNEMOS_STYX_MIN_BYTES`;
5. Drive, re-read after upload, reports a **non-zero** size that **matches**
   what was sent, and an `md5Checksum` that **matches** the local digest.

Pruning happens only after all of that. A failed run leaves the previous
backup set completely untouched, and every run — pass or fail — writes
`styx-last-run.json` into the work directory.

Raise `MNEMOS_STYX_MIN_RECORDS` on the authoritative instance to something
near its real record count. An export that suddenly returns 12 records
instead of 18,000 is a broken export, not a small one, and only that floor
will catch it.
