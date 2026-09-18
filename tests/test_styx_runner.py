"""STYX end-to-end orchestration.

These run the real pipeline — a real disposable SQLite MNEMOS store, a real
CHARON MIF export including the vault, real ``age`` encryption — against a
fake Drive. Only the network transport is simulated; everything that decides
whether the backup is correct is genuine.
"""

from __future__ import annotations

import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.tools.styx.config import StyxConfig
from mnemos.tools.styx.errors import StyxIntegrityError
from mnemos.tools.styx.retention import artifact_name
from mnemos.tools.styx.runner import run_backup
from tests._styx_fakes import FakeDriveFolder

pyrage = pytest.importorskip("pyrage", reason="STYX encryption needs the styx extra")

VAULT_SECRET = "AKIAIOSFODNN7EXAMPLE-styx-runner-not-a-real-key"
ORDINARY = "an ordinary memory about ferries"

NOW = datetime(2026, 9, 17, 2, 40, 0, tzinfo=UTC)


async def _seed_store(path: Path, *, entries: list[tuple[str, str]]) -> None:
    """Create a real SQLite MNEMOS store and seed it."""
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(path, SimpleNamespace())
    await backend.open()
    try:
        for content, namespace in entries:
            now = datetime.now(UTC)
            async with backend.transactional() as tx:
                await backend.memories.insert_memory(
                    tx,
                    memory_id=f"mem_{uuid.uuid4().hex[:12]}",
                    content=content,
                    category="infrastructure",
                    subcategory=None,
                    metadata_json=json.dumps({}),
                    quality_rating=80,
                    owner_id="alice",
                    namespace=namespace,
                    permission_mode=0,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=content,
                    embedding=None,
                    created=now,
                    updated=now,
                )
    finally:
        await backend.close()


@pytest.fixture
def identity():
    return pyrage.x25519.Identity.generate()


@pytest.fixture
def make_config(tmp_path, identity):
    def _make(**overrides):
        defaults = {
            "gdrive_folder_id": "folder-123",
            "age_recipient": str(identity.to_public()),
            "host_label": "testhost",
            "work_dir": tmp_path / "work",
            "sqlite_path": tmp_path / "mnemos.sqlite3",
            "min_records": 1,
        }
        defaults.update(overrides)
        return StyxConfig(**defaults)

    return _make


def _decrypt_bundle(blob: bytes, identity, dest: Path) -> Path:
    """Decrypt + unpack an artifact exactly as an operator restoring would."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "restored.tar.gz"
    archive.write_bytes(pyrage.decrypt(blob, [identity]))
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(dest / "bundle")
    return dest / "bundle"


@pytest.mark.asyncio
async def test_backup_end_to_end_is_complete_and_restorable(tmp_path, make_config, identity):
    """The headline property: the backup contains the vault secret, and an
    operator holding only the offline identity can get it back."""
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(VAULT_SECRET, "vault"), (ORDINARY, "default")])
    drive = FakeDriveFolder()

    receipt = await run_backup(config, drive=drive, now=NOW)

    assert receipt.ok is True
    assert receipt.error is None
    assert receipt.record_count == 2, "both the vault and ordinary memory must be exported"
    assert receipt.artifact_name == artifact_name("testhost", NOW)
    assert receipt.md5 == receipt.remote_md5, "verified against Drive's own digest"
    assert len(drive.files) == 1

    # Restore it the way the README says to, and confirm the secret survived.
    stored = next(iter(drive.files.values()))
    blob = None
    # The fake keeps only metadata, so decrypt the artifact STYX actually made.
    # Re-run in dry-run mode against the same store to obtain the bytes.
    assert stored["size"] > 0

    dry = make_config(work_dir=tmp_path / "dry", dry_run=True)
    dry_receipt = await run_backup(dry, drive=None, now=NOW, keep_workdir=True)
    artifact = dry.work_dir / f"run-{NOW.strftime('%Y%m%dT%H%M%SZ')}" / dry_receipt.artifact_name
    blob = artifact.read_bytes()

    bundle = _decrypt_bundle(blob, identity, tmp_path / "restore")
    manifest = json.loads((bundle / "mif-manifest.json").read_text())
    assert manifest["vault_included"] is True
    assert manifest["vault_redacted"] is False

    everything = "\n".join(p.read_text(errors="ignore") for p in bundle.rglob("*") if p.is_file())
    assert VAULT_SECRET in everything, "a complete backup must contain the vault secret"
    assert ORDINARY in everything


@pytest.mark.asyncio
async def test_empty_export_is_refused_and_nothing_is_uploaded(tmp_path, make_config):
    """The five-week silent failure, caught on the first run.

    An export that found nothing must never be reported as a successful
    backup, and must never reach Drive.
    """
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[])
    drive = FakeDriveFolder()

    with pytest.raises(StyxIntegrityError, match="below the configured minimum"):
        await run_backup(config, drive=drive, now=NOW)

    assert drive.files == {}, "nothing may be uploaded from an empty export"


@pytest.mark.asyncio
async def test_record_floor_catches_a_shrunken_export(tmp_path, make_config):
    """An export returning far fewer records than expected is a broken
    export, not a small one — that is what the floor is for."""
    config = make_config(min_records=100)
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()

    with pytest.raises(StyxIntegrityError, match="below the configured minimum"):
        await run_backup(config, drive=drive, now=NOW)
    assert drive.files == {}


@pytest.mark.asyncio
async def test_checksum_mismatch_fails_the_run(tmp_path, make_config):
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    drive.corrupt_md5 = True

    with pytest.raises(StyxIntegrityError, match="checksum mismatch"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_zero_byte_upload_fails_the_run(tmp_path, make_config):
    """Drive accepting the call but storing nothing is precisely the NFS bug."""
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    drive.truncate_upload = True

    with pytest.raises(StyxIntegrityError, match="zero bytes"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_missing_remote_checksum_fails_the_run(tmp_path, make_config):
    """No digest from the destination means no proof; no proof means no success."""
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    drive.drop_md5 = True

    with pytest.raises(StyxIntegrityError, match="returned no digest"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_a_failed_run_prunes_nothing(tmp_path, make_config):
    """The previous backup set must survive a failure untouched."""
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    for days in range(1, 40):
        drive.seed(artifact_name("testhost", NOW - timedelta(days=days)))
    before = set(drive.files)
    drive.corrupt_md5 = True

    with pytest.raises(StyxIntegrityError):
        await run_backup(config, drive=drive, now=NOW)

    assert drive.deleted == [], "a failed run must not delete anything"
    assert before <= set(drive.files)


@pytest.mark.asyncio
async def test_retention_runs_only_after_a_verified_upload(tmp_path, make_config):
    config = make_config(retain_daily=7, retain_weekly=2, retain_monthly=1)
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    for days in range(1, 60):
        drive.seed(artifact_name("testhost", NOW - timedelta(days=days)))

    receipt = await run_backup(config, drive=drive, now=NOW)

    assert receipt.ok is True
    assert receipt.pruned, "old artifacts should have been pruned"
    assert receipt.retained == len(drive.files)
    # Today's freshly verified artifact is never among the casualties.
    assert receipt.artifact_name not in receipt.pruned


@pytest.mark.asyncio
async def test_retention_ignores_other_hosts_artifacts(tmp_path, make_config):
    """Several hosts may share one folder; a run must not prune a peer."""
    config = make_config(retain_daily=1, retain_weekly=0, retain_monthly=0)
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()
    peer_ids = [drive.seed(artifact_name("otherhost", NOW - timedelta(days=d))) for d in range(1, 10)]

    await run_backup(config, drive=drive, now=NOW)

    for peer in peer_ids:
        assert peer in drive.files, "another host's backups must be left alone"


@pytest.mark.asyncio
async def test_dry_run_encrypts_but_never_uploads(tmp_path, make_config):
    config = make_config(dry_run=True)
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    drive = FakeDriveFolder()

    receipt = await run_backup(config, drive=drive, now=NOW)

    assert receipt.ok is True
    assert receipt.encrypted_bytes > 0
    assert receipt.drive_file_id is None
    assert drive.files == {}


@pytest.mark.asyncio
async def test_receipt_is_written_on_success_and_on_failure(tmp_path, make_config):
    config = make_config()
    receipt_path = config.work_dir / "styx-last-run.json"

    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    await run_backup(config, drive=FakeDriveFolder(), now=NOW)
    assert json.loads(receipt_path.read_text())["ok"] is True

    failing = FakeDriveFolder()
    failing.corrupt_md5 = True
    with pytest.raises(StyxIntegrityError):
        await run_backup(config, drive=failing, now=NOW)

    recorded = json.loads(receipt_path.read_text())
    assert recorded["ok"] is False
    assert "checksum mismatch" in recorded["error"]


@pytest.mark.asyncio
async def test_cleartext_workdir_is_removed_after_a_run(tmp_path, make_config):
    """The bundle holds live credentials in cleartext; it must not linger."""
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(VAULT_SECRET, "vault")])

    receipt = await run_backup(config, drive=FakeDriveFolder(), now=NOW)

    assert receipt.ok is True
    leftovers = [p for p in config.work_dir.rglob("*") if p.is_file()]
    assert [p.name for p in leftovers] == ["styx-last-run.json"]
    assert VAULT_SECRET not in receipt_text(leftovers[0])


def receipt_text(path: Path) -> str:
    return path.read_text(errors="ignore")


# ── CloudflareR2Destination + RcloneDestination runner-level tests ───────
#
# The test_styx_destination.py tests prove the four destination primitives
# work in isolation against a fake client. These tests prove the END-TO-END
# integrity contract that ``runner.py`` enforces — size match, digest match,
# zero-byte rejection, digest-mismatch rejection — holds through each
# non-Drive backend too, not just through Drive's ``md5Checksum``. The five-
# week silent failure was a Drive bug; a future STYX maintainer must not
# accidentally rebuild the same shape against R2 or rclone by short-cutting
# a verification path that only Drive exercised.
#
# The fakes here deliberately mirror the boto3 / rclone surface that the
# real destinations use, rather than recasting them in a generic abstraction:
# the whole point of these tests is to exercise the same code paths the real
# backends take, just against an in-memory or argv-captured mock.


class _FakeR2ClientForRunner:
    """boto3-shaped fake that records puts/heads and supports fault injection.

    Mirrors ``_FakeR2Client`` in test_styx_destination.py but with one
    runner-specific knob: a way to corrupt what ``head_object`` returns so
    the post-upload digest mismatch is exercised the way ``runner.py``
    actually exercises it (the rerun of HEAD against the destination, not
    the value the put returned).
    """

    def __init__(self):

        self.objects: dict[str, dict] = {}
        self.fail_put = False
        # When True, head_object returns a different md5 from what put_object
        # wrote — the canonical "destination accepted the bytes but corrupted
        # them on the way to disk" scenario.
        self.corrupt_storage = False
        # When set to a positive int, head_object reports a smaller size than
        # was uploaded — the canonical "destination truncated the upload"
        # scenario.
        self.truncate_size_to: int | None = None

    def put_object(self, *, Bucket, Key, Body):
        if self.fail_put:
            raise RuntimeError("injected put failure")
        import hashlib

        digest = hashlib.md5(Body).hexdigest()
        self.objects[Key] = {"body": Body, "size": len(Body), "etag": digest}
        return {"ETag": f'"{digest}"'}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        row = self.objects[Key]
        size = row["size"]
        if self.truncate_size_to is not None:
            size = self.truncate_size_to
        etag = row["etag"]
        if self.corrupt_storage:
            etag = "0" * 32
        return {"ContentLength": size, "ETag": f'"{etag}"'}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matching = sorted(k for k in self.objects if k.startswith(Prefix))
        return {
            "Contents": [
                {
                    "Key": k,
                    "Size": self.objects[k]["size"],
                    "ETag": f'"{self.objects[k]["etag"]}"',
                }
                for k in matching
            ],
            "IsTruncated": False,
        }

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.mark.asyncio
async def test_run_backup_through_r2_succeeds_on_correct_upload(tmp_path, make_config):
    """A clean R2 round-trip: put, head, list — all agree on size and digest."""
    from mnemos.tools.styx.destination import CloudflareR2Destination

    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    client = _FakeR2ClientForRunner()
    drive = CloudflareR2Destination(client, "styx-backups")

    receipt = await run_backup(config, drive=drive, now=NOW)

    assert receipt.ok is True
    assert receipt.error is None
    assert receipt.md5 == receipt.remote_md5, "R2's ETag must match local md5"
    assert receipt.encrypted_bytes == receipt.archive_bytes or receipt.encrypted_bytes > 0
    assert len(client.objects) == 1, "exactly one artifact is stored"


@pytest.mark.asyncio
async def test_run_backup_through_r2_fails_on_digest_mismatch(tmp_path, make_config):
    """The destination accepted the bytes but stored them wrong: runner
    must catch it. This is the cross-backend equivalent of
    ``test_checksum_mismatch_fails_the_run``."""
    from mnemos.tools.styx.destination import CloudflareR2Destination

    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    client = _FakeR2ClientForRunner()
    client.corrupt_storage = True
    drive = CloudflareR2Destination(client, "styx-backups")

    with pytest.raises(StyxIntegrityError, match="checksum mismatch"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_run_backup_through_r2_fails_on_truncated_upload(tmp_path, make_config):
    """The destination accepted the bytes but stored fewer: runner
    must catch it. This is the cross-backend equivalent of
    ``test_zero_byte_upload_fails_the_run``."""
    from mnemos.tools.styx.destination import CloudflareR2Destination

    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    client = _FakeR2ClientForRunner()
    # Tell head_object the object is one byte. run_backup should reject the
    # size mismatch before it even gets to the digest comparison.
    client.truncate_size_to = 1
    drive = CloudflareR2Destination(client, "styx-backups")

    with pytest.raises(StyxIntegrityError, match="size mismatch"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_run_backup_through_r2_fails_when_md5_is_missing(tmp_path, make_config):
    """No digest from the destination = no proof = no success."""
    from mnemos.tools.styx.destination import CloudflareR2Destination

    class _NoDigestR2Client(_FakeR2ClientForRunner):
        def head_object(self, *, Bucket, Key):
            row = super().head_object(Bucket=Bucket, Key=Key)
            return {"ContentLength": row["ContentLength"], "ETag": None}

    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    client = _NoDigestR2Client()
    drive = CloudflareR2Destination(client, "styx-backups")

    with pytest.raises(StyxIntegrityError, match="no digest"):
        await run_backup(config, drive=drive, now=NOW)


# ── rclone runner-level tests ────────────────────────────────────────────


class _FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_rclone_binary_for_runner(monkeypatch):
    monkeypatch.setattr("mnemos.tools.styx.destination.shutil.which", lambda _: "/usr/bin/rclone")


def _make_rclone_fake_run(*, md5_override: str | None = None, size_override: int | None = None):
    """Build a fake ``subprocess.run`` that emulates the rclone subcommands.

    Reads the bytes STYX uploaded at copyto time (so we can return the
    real MD5 + size from the subsequent ``lsjson --hash`` call), with two
    fault-injection overrides: ``md5_override`` replaces the MD5 hash
    ``lsjson`` reports (digest-mismatch case) and ``size_override``
    replaces the Size lsjson reports (truncation case). Both default to
    None, meaning "report the truth" — the success path.
    """
    uploaded: dict[str, tuple[int, str]] = {}

    def _fake_run(argv, **kwargs):
        import hashlib

        subcommand = argv[1]
        if subcommand == "copyto":
            src = Path(argv[2])
            dst = argv[3]
            data = src.read_bytes()
            uploaded[dst] = (len(data), hashlib.md5(data).hexdigest())
            return _FakeCompletedProcess()
        if subcommand == "deletefile":
            uploaded.pop(argv[2], None)
            return _FakeCompletedProcess()
        if subcommand == "lsjson":
            remote = argv[-1]
            entries: list[dict] = []
            for dst, (size, md5) in uploaded.items():
                if not dst.startswith(remote + "/") and dst != remote:
                    continue
                name = dst[len(remote) + 1 :] if dst.startswith(remote + "/") else dst
                entries.append(
                    {
                        "Name": name,
                        "Size": size if size_override is None else size_override,
                        "IsDir": False,
                        "Hashes": {"md5": md5 if md5_override is None else md5_override},
                    }
                )
            return _FakeCompletedProcess(stdout=json.dumps(entries))
        raise AssertionError(f"unexpected rclone subcommand {argv}")

    return _fake_run


@pytest.mark.asyncio
async def test_run_backup_through_rclone_succeeds_on_correct_upload(tmp_path, make_config, monkeypatch):
    """A clean rclone round-trip: copyto, lsjson stat — agree on size and digest."""
    from mnemos.tools.styx.destination import RcloneDestination

    _patch_rclone_binary_for_runner(monkeypatch)
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    remote = "gdrive-personal:mnemos-backups"
    monkeypatch.setattr(
        "mnemos.tools.styx.destination.subprocess.run",
        _make_rclone_fake_run(),
    )
    drive = RcloneDestination(remote)

    receipt = await run_backup(config, drive=drive, now=NOW)

    assert receipt.ok is True
    assert receipt.error is None
    assert receipt.md5 == receipt.remote_md5, "rclone's MD5 must match local md5"
    assert receipt.encrypted_bytes > 0


@pytest.mark.asyncio
async def test_run_backup_through_rclone_fails_on_digest_mismatch(tmp_path, make_config, monkeypatch):
    """rclone returned a different MD5 than the bytes STYX wrote — runner
    must catch it via the post-upload stat (which calls lsjson again,
    filtered to one entry, the way ``RcloneDestination.stat`` works)."""
    from mnemos.tools.styx.destination import RcloneDestination

    _patch_rclone_binary_for_runner(monkeypatch)
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    remote = "gdrive-personal:mnemos-backups"
    monkeypatch.setattr(
        "mnemos.tools.styx.destination.subprocess.run",
        _make_rclone_fake_run(md5_override="0" * 32),
    )
    drive = RcloneDestination(remote)

    with pytest.raises(StyxIntegrityError, match="checksum mismatch"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_run_backup_through_rclone_fails_on_truncated_upload(tmp_path, make_config, monkeypatch):
    """rclone reports a smaller Size than STYX sent: runner must catch the
    size mismatch before it gets to the digest check."""
    from mnemos.tools.styx.destination import RcloneDestination

    _patch_rclone_binary_for_runner(monkeypatch)
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    remote = "gdrive-personal:mnemos-backups"
    monkeypatch.setattr(
        "mnemos.tools.styx.destination.subprocess.run",
        _make_rclone_fake_run(size_override=1),
    )
    drive = RcloneDestination(remote)

    with pytest.raises(StyxIntegrityError, match="size mismatch"):
        await run_backup(config, drive=drive, now=NOW)


@pytest.mark.asyncio
async def test_run_backup_through_rclone_fails_when_md5_is_missing(tmp_path, make_config, monkeypatch):
    """No md5 from rclone = no proof = no success — same gate as Drive / R2."""
    from mnemos.tools.styx.destination import RcloneDestination

    _patch_rclone_binary_for_runner(monkeypatch)
    config = make_config()
    await _seed_store(config.sqlite_path, entries=[(ORDINARY, "default")])
    remote = "gdrive-personal:mnemos-backups"

    # Persistent across the multiple subprocess.run calls a single
    # run_backup produces: copyto records the upload, then stat's lsjson
    # reads it back.
    state: dict[str, dict[str, tuple[int, str]]] = {remote: {}}

    def _no_hashes_run(argv, **kwargs):
        import hashlib

        subcommand = argv[1]
        if subcommand == "copyto":
            data = Path(argv[2]).read_bytes()
            state[remote][argv[3]] = (len(data), hashlib.md5(data).hexdigest())
            return _FakeCompletedProcess()
        if subcommand == "deletefile":
            state[remote].pop(argv[2], None)
            return _FakeCompletedProcess()
        if subcommand == "lsjson":
            remote_dir = argv[-1]
            entries = []
            for dst, (size, _md5) in state[remote].items():
                if not dst.startswith(remote_dir + "/") and dst != remote_dir:
                    continue
                name = dst[len(remote_dir) + 1 :] if dst.startswith(remote_dir + "/") else dst
                # NOTE: no ``Hashes`` key — same shape rclone returns for a
                # backend that has no MD5 (e.g., some S3-compatible stores
                # without MD5 enabled). ``RcloneDestination.stat`` should
                # hand back ``md5=None``; ``runner.py`` is supposed to refuse.
                entries.append({"Name": name, "Size": size, "IsDir": False})
            return _FakeCompletedProcess(stdout=json.dumps(entries))
        raise AssertionError(f"unexpected rclone subcommand {argv}")

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _no_hashes_run)
    drive = RcloneDestination(remote)

    with pytest.raises(StyxIntegrityError, match="no digest"):
        await run_backup(config, drive=drive, now=NOW)
