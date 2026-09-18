"""STYX orchestration — export, encrypt, upload, verify, prune.

The ordering is the design. Nothing is pruned until the new artifact has
been read back from the destination and proven byte-identical to what was
sent, so a run that fails anywhere leaves the previous backup set untouched.

Every gate below exists because of one specific precedent on this fleet: a
backup job that reported success while writing nothing for five weeks. Its
only success signal was "the command exited 0". STYX's success signal is
"the destination is holding a file whose server-computed digest matches the
digest of the bytes I encrypted, and that file is not empty" — true whether
the destination is Google Drive, Cloudflare R2, or an rclone remote.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mnemos.tools.styx.config import StyxConfig
from mnemos.tools.styx.crypto import encrypt_file, has_age_header, md5_file, sha256_file
from mnemos.tools.styx.destination import Destination
from mnemos.tools.styx.errors import StyxIntegrityError
from mnemos.tools.styx.retention import artifact_name, plan_retention
from mnemos.tools.styx.source import archive_bundle, produce_bundle

logger = logging.getLogger("mnemos.styx")


@dataclass
class BackupReceipt:
    """What a run did, in enough detail to audit it after the fact.

    Written to disk next to the work directory on every run — success or
    failure — because "the timer said it was fine" is precisely the evidence
    that turned out to be worthless last time.
    """

    host_label: str
    started_at: str
    finished_at: str | None = None
    ok: bool = False
    artifact_name: str | None = None
    drive_file_id: str | None = None
    record_count: int = 0
    archive_bytes: int = 0
    encrypted_bytes: int = 0
    sha256: str | None = None
    md5: str | None = None
    remote_md5: str | None = None
    pruned: list[str] = field(default_factory=list)
    retained: int = 0
    dry_run: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gate(condition: bool, message: str) -> None:
    if not condition:
        raise StyxIntegrityError(message)


async def run_backup(
    config: StyxConfig,
    *,
    drive: Destination | None = None,
    now: datetime | None = None,
    allow_remote_http: bool = False,
    keep_workdir: bool = False,
) -> BackupReceipt:
    """Run one complete STYX backup. Raises on any failed gate.

    ``drive`` is injected so the orchestration can be exercised against a
    fake; production callers pass whatever :func:`mnemos.tools.styx.
    destination.build_destination` returns for ``config.destination_kind``
    (Google Drive, Cloudflare R2, or an rclone remote).
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    receipt = BackupReceipt(
        host_label=config.host_label,
        started_at=moment.isoformat(),
        dry_run=config.dry_run,
    )
    run_dir = config.work_dir / f"run-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    bundle_dir = run_dir / "bundle"
    archive_path = run_dir / "bundle.mif.tar.gz"
    name = artifact_name(config.host_label, moment)
    receipt.artifact_name = name

    try:
        run_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. export ────────────────────────────────────────────────────
        bundle = await produce_bundle(config, bundle_dir, allow_remote_http=allow_remote_http)
        receipt.record_count = bundle.record_count
        logger.info(
            "styx: exported %d records from %s (%s)",
            bundle.record_count,
            config.host_label,
            bundle.kind,
        )

        # The gate that would have caught the five-week silent failure on day
        # one. An export that found nothing is not a backup of an empty
        # store, it is a broken export until proven otherwise.
        _gate(
            bundle.record_count >= config.min_records,
            f"export produced {bundle.record_count} records, below the configured "
            f"minimum of {config.min_records}. Refusing to upload what looks like "
            f"an empty or failed export.",
        )

        # ── 2. archive ───────────────────────────────────────────────────
        archive_bundle(bundle_dir, archive_path)
        receipt.archive_bytes = archive_path.stat().st_size
        _gate(receipt.archive_bytes > 0, "archive is zero bytes")

        # ── 3. encrypt ───────────────────────────────────────────────────
        encrypted_path = run_dir / name
        encrypt_file(archive_path, encrypted_path, config.age_recipient)
        receipt.encrypted_bytes = encrypted_path.stat().st_size
        receipt.sha256 = sha256_file(encrypted_path)
        receipt.md5 = md5_file(encrypted_path)

        # Prove we are about to upload ciphertext. The bundle holds live
        # credentials in cleartext, so shipping the plaintext archive by
        # mistake is the single worst outcome available to this tool.
        _gate(
            has_age_header(encrypted_path),
            "encrypted artifact does not carry an age header — refusing to upload, "
            "the payload may be unencrypted plaintext containing live credentials",
        )
        _gate(
            receipt.encrypted_bytes >= config.min_bytes,
            f"encrypted artifact is {receipt.encrypted_bytes} bytes, below the configured floor of {config.min_bytes}",
        )

        if config.dry_run or drive is None:
            receipt.ok = True
            logger.info("styx: dry run complete, %s not uploaded", name)
            return receipt

        # ── 4. upload ────────────────────────────────────────────────────
        uploaded = drive.upload(encrypted_path, name)
        receipt.drive_file_id = uploaded.file_id
        _gate(bool(uploaded.file_id), "destination returned no file id for the upload")

        # ── 5. verify what actually landed ───────────────────────────────
        # Read the metadata back rather than trusting the create response,
        # and compare against the destination's own server-side digest.
        # This is the only evidence STYX accepts that the backup exists.
        remote = drive.stat(uploaded.file_id)
        receipt.remote_md5 = remote.md5
        _gate(remote.size > 0, f"destination reports {name} as zero bytes after upload")
        _gate(
            remote.size == receipt.encrypted_bytes,
            f"size mismatch after upload: sent {receipt.encrypted_bytes} bytes, destination holds {remote.size}",
        )
        _gate(
            remote.md5 is not None,
            f"destination returned no digest for {name}; cannot prove the upload is intact",
        )
        _gate(
            remote.md5 == receipt.md5,
            f"checksum mismatch after upload: local md5 {receipt.md5}, destination digest {remote.md5}",
        )
        logger.info("styx: verified %s on the destination (%d bytes)", name, remote.size)

        # ── 6. prune, only now that a good backup is confirmed ───────────
        existing = drive.list_artifacts(config.host_label)
        keep, prune = plan_retention(
            existing,
            daily=config.retain_daily,
            weekly=config.retain_weekly,
            monthly=config.retain_monthly,
        )
        for stale in prune:
            drive.delete(stale.file_id)
            receipt.pruned.append(stale.name)
        receipt.retained = len(keep)
        logger.info("styx: retained %d artifacts, pruned %d", len(keep), len(prune))

        receipt.ok = True
        return receipt
    except Exception as exc:
        receipt.ok = False
        receipt.error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "styx: backup FAILED for %s: %s",
            config.host_label,
            receipt.error,
            exc_info=True,
        )
        raise
    finally:
        receipt.finished_at = datetime.now(timezone.utc).isoformat()
        _write_receipt(config, receipt)
        # The work directory holds the cleartext bundle. Remove it unless the
        # operator asked to keep it for forensics.
        if not keep_workdir and run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)


def _write_receipt(config: StyxConfig, receipt: BackupReceipt) -> Path | None:
    """Persist the run receipt, best-effort.

    Never raises: a receipt that cannot be written must not convert a
    successful backup into a failed one, nor mask the real exception on the
    failure path.
    """
    import json  # noqa: PLC0415 - local to keep the failure path dependency-free

    try:
        config.work_dir.mkdir(parents=True, exist_ok=True)
        path = config.work_dir / "styx-last-run.json"
        path.write_text(json.dumps(receipt.to_dict(), indent=2), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - best-effort observability, never fatal
        logger.warning("styx: could not write run receipt", exc_info=True)
        return None
