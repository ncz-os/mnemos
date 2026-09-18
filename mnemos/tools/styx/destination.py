"""Pluggable upload destinations for STYX.

Mirrors :mod:`mnemos.tools.styx.source`'s one-class-per-backend shape: each
destination owns its own transport and its own verification, and
``runner.py`` drives whichever one it is handed through one duck-typed
interface (``upload``, ``stat``, ``list_artifacts``, ``delete`` — the same
four methods :class:`mnemos.tools.styx.drive.DriveBackupFolder` already
exposes). Nothing here changes that interface or the post-upload
size+digest re-read gate in ``runner.py`` — a destination that cannot
support it honestly is not a valid STYX destination.

Two additional destinations exist alongside the original GCP
service-account Google Drive path, added because that ceremony is real
overhead for a personal/home-fleet deployment that a shared, already-issued
credential removes entirely:

``r2``
    Cloudflare R2 (S3-compatible object storage) via ``boto3``. No OAuth
    dance at all — a scoped API token pasted into config is the whole
    setup. Verification uses R2's own ETag, which for a single-part PUT
    (STYX bundles are a few MB, well under any multipart threshold) is the
    object's MD5 hex digest — the same shape Drive's ``md5Checksum``
    already gave ``runner.py``, so no verification logic had to change.

``rclone``
    A named ``rclone`` remote, configured entirely outside this codebase
    (``rclone config``). This is deliberate: rclone owns 100% of the OAuth
    flow and token storage for a *personal* Google Drive account, so this
    process never sees, stores, or can leak a Drive credential — it only
    ever shells out to a binary that already has its own, out-of-band
    authorization. Verification reads back ``rclone``'s own computed MD5
    via ``rclone lsjson --hash``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mnemos.tools.styx.config import DEST_GDRIVE, DEST_R2, DEST_RCLONE, StyxConfig
from mnemos.tools.styx.errors import StyxConfigError, StyxUploadError
from mnemos.tools.styx.retention import ARTIFACT_SUFFIX, RemoteArtifact

_RCLONE_TIMEOUT_S = 900

#: boto3's default ``read_timeout`` is 60 seconds. STYX bundles are a few MB
#: but a slow link, a busy R2 edge, or a TLS handshake that needs a retry can
#: push a single PUT well past that — and the default aborts the request and
#: leaves the operator holding an empty object without an explanation. Pin
#: connect and read to the same 900s the rclone path uses so the two
#: transports have one shared upper bound on "how patient STYX is", and so a
#: logged timeout means a real timeout, not a default-fluke.
_BOTO3_TIMEOUT_S = 900


class Destination(Protocol):
    """The four operations ``runner.py`` performs against an upload target."""

    def upload(self, path: Path, name: str) -> RemoteArtifact: ...

    def stat(self, file_id: str) -> RemoteArtifact: ...

    def list_artifacts(self, host_label: str | None = None) -> list[RemoteArtifact]: ...

    def delete(self, file_id: str) -> None: ...


# ── Cloudflare R2 ────────────────────────────────────────────────────────


def _strip_etag(etag: str | None) -> str | None:
    """S3-family APIs quote the ETag header value; drop the quotes."""
    if etag is None:
        return None
    return etag.strip('"')


@dataclass(frozen=True)
class R2Credentials:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str = ""


def build_r2_client(creds: R2Credentials):
    """Build a boto3 S3 client pointed at this account's R2 endpoint."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - exercised by the import-guard test
        raise StyxConfigError(
            "STYX needs boto3 to reach Cloudflare R2. Install the extra: "
            "reinstall mnemos-core with its base dependencies"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=f"https://{creds.account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        # R2's docs specify the "auto" pseudo-region; a real AWS region name
        # is meaningless here and SigV4 still needs something non-empty.
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            # Match rclone's 900s ceiling (see ``_BOTO3_TIMEOUT_S``) so a
            # hung PUT fails with a clear timeout error instead of the boto3
            # default 60s, which would fire mid-upload for a slow link.
            connect_timeout=_BOTO3_TIMEOUT_S,
            read_timeout=_BOTO3_TIMEOUT_S,
        ),
    )


class CloudflareR2Destination:
    """The operations STYX performs against one R2 bucket (+ optional key prefix)."""

    def __init__(self, client, bucket: str, prefix: str = ""):
        self._client = client
        self._bucket = bucket
        # Bucket-relative "directory" STYX artifacts live under. Kept
        # separate from the bucket name itself so one bucket could in
        # principle host more than one thing without STYX's own listing
        # ever seeing a non-STYX object.
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def upload(self, path: Path, name: str) -> RemoteArtifact:
        """Upload ``path`` as ``name`` via a single, non-multipart PUT.

        Single-part is load-bearing, not incidental: R2's ETag equals the
        object's MD5 hex digest only when the object was written as one
        part. ``put_object`` with the full body in memory (STYX bundles are
        a few MB) guarantees that, where ``upload_file``'s automatic
        multipart threshold would not.
        """
        key = self._key(name)
        data = Path(path).read_bytes()
        try:
            response = self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        except Exception as exc:
            raise StyxUploadError(f"uploading {name} to R2 bucket {self._bucket!r} failed: {exc}") from exc
        etag = _strip_etag(response.get("ETag"))
        return RemoteArtifact(file_id=key, name=name, size=len(data), md5=etag)

    def stat(self, file_id: str) -> RemoteArtifact:
        """Re-read an object's server-side metadata via HEAD."""
        try:
            found = self._client.head_object(Bucket=self._bucket, Key=file_id)
        except Exception as exc:
            raise StyxUploadError(f"reading back R2 object {file_id} failed: {exc}") from exc
        name = file_id.removeprefix(self._prefix)
        return RemoteArtifact(
            file_id=file_id,
            name=name,
            size=int(found.get("ContentLength", 0)),
            md5=_strip_etag(found.get("ETag")),
        )

    def list_artifacts(self, host_label: str | None = None) -> list[RemoteArtifact]:
        """List STYX artifacts under this bucket's prefix, optionally for one host.

        Filters on BOTH ``mnemos-<host>-...`` and the ``.mif.tar.gz.age``
        suffix, exactly like Drive does. A bucket shared with other
        tooling (logs, sidecars from another process, anything an operator
        archived there once) is the default deployment shape; ``list_objects_v2``
        with ``Prefix`` is a substring match, so without the suffix check a
        ``logs/mnemos-pythia-debug.txt`` file would slip through and be
        counted toward this host's retention budget. The retention pass
        already requires both — keep the listing honest at the source.
        """
        name_prefix = f"mnemos-{host_label}-" if host_label else "mnemos-"
        list_prefix = f"{self._prefix}{name_prefix}"
        artifacts: list[RemoteArtifact] = []
        continuation: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": list_prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            try:
                response = self._client.list_objects_v2(**kwargs)
            except Exception as exc:
                raise StyxUploadError(f"listing R2 bucket {self._bucket!r} failed: {exc}") from exc
            for item in response.get("Contents", []):
                key = item["Key"]
                name = key.removeprefix(self._prefix)
                if not name.startswith(name_prefix) or not name.endswith(ARTIFACT_SUFFIX):
                    continue
                artifacts.append(
                    RemoteArtifact(
                        file_id=key,
                        name=name,
                        size=int(item.get("Size", 0)),
                        md5=_strip_etag(item.get("ETag")),
                    )
                )
            if not response.get("IsTruncated"):
                return artifacts
            continuation = response.get("NextContinuationToken")

    def delete(self, file_id: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=file_id)
        except Exception as exc:
            raise StyxUploadError(f"deleting R2 object {file_id} failed: {exc}") from exc


# ── rclone (generic — personal Google Drive, or any other rclone remote) ──


def _rclone_binary() -> str:
    found = shutil.which("rclone")
    if not found:
        raise StyxConfigError(
            "STYX is configured for the rclone destination but the 'rclone' binary "
            "is not on PATH. Install it (https://rclone.org/install/) and run "
            "'rclone config' to set up the remote before running STYX."
        )
    return found


def _run_rclone(args: list[str]) -> str:
    """Run rclone with a fixed argument LIST (never a shell string).

    Every argument STYX passes here is either a fixed flag or a value this
    process itself constructed (the remote spec from config, or an artifact
    name STYX generated) -- never anything from an untrusted source, and
    never concatenated into a shell command, so there is no injection
    surface even in principle.
    """
    binary = _rclone_binary()
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_RCLONE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StyxUploadError(f"rclone {args[0]} timed out after {_RCLONE_TIMEOUT_S}s") from exc
    if result.returncode != 0:
        # rclone's own stderr does not echo the remote's stored credentials
        # (those live only in its config file, never on argv or in output),
        # so surfacing it verbatim here does not leak a secret.
        raise StyxUploadError(f"rclone {args[0]} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


class RcloneDestination:
    """The operations STYX performs against one ``remote:path`` rclone target.

    ``remote_spec`` is exactly what an operator would type after ``rclone
    copy <src>`` -- a remote name and, usually, a subdirectory
    (``gdrive-personal:mnemos-backups``). rclone resolves everything about
    *how* to reach that remote (credentials, endpoint, retries) from its own
    config; this class never reads or holds that config itself.
    """

    def __init__(self, remote_spec: str):
        self._remote = remote_spec.rstrip("/")

    def _path(self, name: str) -> str:
        return f"{self._remote}/{name}"

    def upload(self, path: Path, name: str) -> RemoteArtifact:
        _run_rclone(["copyto", str(path), self._path(name)])
        return self.stat(self._path(name))

    def stat(self, file_id: str) -> RemoteArtifact:
        """Re-read one object's rclone-computed size+MD5 by relisting its parent.

        rclone has no single-object ``stat`` verb that reliably returns a
        hash across every backend, so the same ``lsjson --hash`` listing
        used for :meth:`list_artifacts` is filtered to the one entry this
        call cares about -- a real read-back of what the remote holds now,
        not a cached claim from the upload response.
        """
        remote_dir, _, name = file_id.rpartition("/")
        for artifact in self._list(remote_dir or self._remote):
            if artifact.name == name:
                return artifact
        raise StyxUploadError(f"rclone stat: no such object {file_id!r} on the remote")

    def list_artifacts(self, host_label: str | None = None) -> list[RemoteArtifact]:
        # Match Drive and R2: require BOTH the ``mnemos-<host>-`` prefix AND
        # the ``.mif.tar.gz.age`` suffix, so a sidecar or a stray file
        # someone hand-uploaded to the same rclone path can never be
        # mistaken for a backup artifact and counted toward retention.
        prefix = f"mnemos-{host_label}-" if host_label else "mnemos-"
        return [a for a in self._list(self._remote) if a.name.startswith(prefix) and a.name.endswith(ARTIFACT_SUFFIX)]

    def _list(self, remote_dir: str) -> list[RemoteArtifact]:
        raw = _run_rclone(["lsjson", "--hash", remote_dir])
        try:
            entries = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise StyxUploadError(f"rclone lsjson returned unparseable output: {exc}") from exc
        artifacts = []
        for entry in entries:
            if entry.get("IsDir"):
                continue
            name = entry.get("Name", "")
            md5 = (entry.get("Hashes") or {}).get("md5")
            artifacts.append(
                RemoteArtifact(
                    file_id=f"{remote_dir}/{name}",
                    name=name,
                    size=int(entry.get("Size", 0)),
                    md5=md5,
                )
            )
        return artifacts

    def delete(self, file_id: str) -> None:
        _run_rclone(["deletefile", file_id])


# ── factory ─────────────────────────────────────────────────────────────


def build_destination(config: StyxConfig):
    """Build the live destination ``config.destination_kind`` selects.

    Kept separate from :func:`mnemos.tools.styx.runner.run_backup` so the
    orchestration logic stays testable against a fake with no real
    credential of any kind -- this is the one place per destination that
    needs the real client library or the real ``rclone`` binary, and it is
    not imported until called.
    """
    if config.destination_kind == DEST_GDRIVE:
        from mnemos.tools.styx.drive import (
            DriveBackupFolder,
            build_drive_service,
        )

        service = build_drive_service(config.gdrive_credentials)
        return DriveBackupFolder(service, config.gdrive_folder_id)

    if config.destination_kind == DEST_R2:
        creds = R2Credentials(
            account_id=config.r2_account_id or "",
            bucket=config.r2_bucket or "",
            access_key_id=config.r2_access_key_id or "",
            secret_access_key=config.r2_secret_access_key or "",
            prefix=config.r2_prefix or "",
        )
        client = build_r2_client(creds)
        return CloudflareR2Destination(client, creds.bucket, creds.prefix)

    if config.destination_kind == DEST_RCLONE:
        return RcloneDestination(config.rclone_remote or "")

    raise StyxConfigError(f"unknown destination kind {config.destination_kind!r}")
