"""STYX destination abstraction — R2, rclone, and the selector factory.

Mirrors tests/test_styx_drive.py's idiom (hand-rolled fakes matching the
real client's method shape, not a mocking framework) so all three
destinations read the same way.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mnemos.tools.styx.config import DEST_GDRIVE, DEST_R2, DEST_RCLONE, StyxConfig
from mnemos.tools.styx.destination import (
    CloudflareR2Destination,
    RcloneDestination,
    _strip_etag,
    build_destination,
)
from mnemos.tools.styx.errors import StyxConfigError, StyxUploadError

RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3n0qmt"


# ── Cloudflare R2 ───────────────────────────────────────────────────────


class _FakeR2Client:
    """Stand-in for a boto3 S3 client, shaped like the real one's methods."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.fail_put = False
        self.fail_head = False
        self.fail_list = False

    def put_object(self, *, Bucket, Key, Body):
        if self.fail_put:
            raise RuntimeError("injected put failure")
        import hashlib

        digest = hashlib.md5(Body).hexdigest()
        self.objects[Key] = {"body": Body, "size": len(Body), "etag": digest}
        return {"ETag": f'"{digest}"'}

    def head_object(self, *, Bucket, Key):
        if self.fail_head:
            raise RuntimeError("injected head failure")
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        row = self.objects[Key]
        return {"ContentLength": row["size"], "ETag": f'"{row["etag"]}"'}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        if self.fail_list:
            raise RuntimeError("injected list failure")
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


def test_strip_etag_removes_quotes():
    assert _strip_etag('"abc123"') == "abc123"
    assert _strip_etag(None) is None


def test_r2_upload_returns_the_computed_etag_as_md5(tmp_path):
    path = tmp_path / "bundle.age"
    path.write_bytes(b"ciphertext-not-really")
    client = _FakeR2Client()
    dest = CloudflareR2Destination(client, "styx-backups")

    artifact = dest.upload(path, "mnemos-pythia-20260917T024000Z.mif.tar.gz.age")

    assert artifact.file_id == "mnemos-pythia-20260917T024000Z.mif.tar.gz.age"
    assert artifact.size == len(b"ciphertext-not-really")
    assert artifact.md5 is not None
    assert '"' not in artifact.md5


def test_r2_stat_reads_back_size_and_digest(tmp_path):
    path = tmp_path / "bundle.age"
    path.write_bytes(b"payload")
    client = _FakeR2Client()
    dest = CloudflareR2Destination(client, "styx-backups")
    uploaded = dest.upload(path, "mnemos-pythia-x.mif.tar.gz.age")

    remote = dest.stat(uploaded.file_id)

    assert remote.size == uploaded.size
    assert remote.md5 == uploaded.md5


def test_r2_upload_failure_is_a_styx_upload_error(tmp_path):
    path = tmp_path / "bundle.age"
    path.write_bytes(b"x")
    client = _FakeR2Client()
    client.fail_put = True
    dest = CloudflareR2Destination(client, "styx-backups")
    with pytest.raises(StyxUploadError, match="uploading .* to R2 bucket"):
        dest.upload(path, "mnemos-pythia-x.mif.tar.gz.age")


def test_r2_stat_of_missing_object_is_a_styx_upload_error():
    dest = CloudflareR2Destination(_FakeR2Client(), "styx-backups")
    with pytest.raises(StyxUploadError, match="reading back R2 object"):
        dest.stat("no-such-key")


def test_r2_list_artifacts_filters_by_host_and_respects_prefix(tmp_path):
    client = _FakeR2Client()
    dest = CloudflareR2Destination(client, "styx-backups", prefix="styx")
    for name in [
        "mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        "mnemos-otherhost-20260917T024000Z.mif.tar.gz.age",
    ]:
        dest.upload(tmp_path / "x", name) if False else None
        client.objects[f"styx/{name}"] = {"body": b"", "size": 10, "etag": "d" * 32}

    found = dest.list_artifacts("pythia")

    assert [a.name for a in found] == ["mnemos-pythia-20260917T024000Z.mif.tar.gz.age"]
    assert found[0].file_id == "styx/mnemos-pythia-20260917T024000Z.mif.tar.gz.age"


def test_r2_list_artifacts_rejects_objects_without_the_age_suffix():
    """A bucket shared with other tooling can hold sidecars / debug logs /
    hand-uploaded files. ``list_objects_v2``'s ``Prefix`` is a substring
    match, so without the suffix check a ``mnemos-pythia-debug.txt`` would
    slip through and be counted toward retention. Drive already requires
    the suffix; R2 must too."""
    client = _FakeR2Client()
    dest = CloudflareR2Destination(client, "styx-backups")
    client.objects["mnemos-pythia-20260917T024000Z.mif.tar.gz.age"] = {
        "body": b"",
        "size": 10,
        "etag": "a" * 32,
    }
    # Prefix matches but the suffix does not — must be filtered out.
    client.objects["mnemos-pythia-debug.txt"] = {
        "body": b"",
        "size": 10,
        "etag": "b" * 32,
    }
    # Suffix matches but the prefix does not (a different host) — also filtered out.
    client.objects["mnemos-otherhost-20260917T024000Z.mif.tar.gz.age"] = {
        "body": b"",
        "size": 10,
        "etag": "c" * 32,
    }

    found = dest.list_artifacts("pythia")
    assert [a.name for a in found] == ["mnemos-pythia-20260917T024000Z.mif.tar.gz.age"]


def test_build_r2_client_sets_long_timeouts_comparable_to_rclone(monkeypatch):
    """boto3's default ``read_timeout`` is 60s; rclone's is 900s. A STYX
    bundle can sit on a slow link for minutes; without an explicit
    ``read_timeout`` the request would be aborted before the upload
    completed. The fix pins connect and read to 900s on the boto3
    ``Config`` so both transports share one ceiling on "how patient STYX
    is"."""
    import types

    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def _fake_boto3_client(*args, **kwargs):
        captured.update(kwargs)
        captured["_config"] = kwargs.get("config")
        return object()

    fake_boto3 = types.SimpleNamespace(client=_fake_boto3_client)
    fake_botocore_config = types.SimpleNamespace(Config=_FakeConfig)
    fake_botocore = types.ModuleType("botocore")
    fake_botocore.config = fake_botocore_config
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)
    monkeypatch.setitem(__import__("sys").modules, "botocore", fake_botocore)
    monkeypatch.setitem(__import__("sys").modules, "botocore.config", fake_botocore_config)

    from mnemos.tools.styx.destination import R2Credentials, build_r2_client

    creds = R2Credentials(account_id="acct", bucket="b", access_key_id="k", secret_access_key="s")
    build_r2_client(creds)

    config = captured["_config"]
    # 900s matches rclone's _RCLONE_TIMEOUT_S — the two transports have
    # one shared upper bound on "how patient STYX is".
    assert config.kwargs["connect_timeout"] == 900
    assert config.kwargs["read_timeout"] == 900
    # SigV4 is still required for R2.
    assert config.kwargs["signature_version"] == "s3v4"


def test_r2_delete_surfaces_errors_generically(monkeypatch):
    client = _FakeR2Client()

    def _boom(**kwargs):
        raise RuntimeError("permission denied")

    client.delete_object = _boom
    dest = CloudflareR2Destination(client, "styx-backups")
    with pytest.raises(StyxUploadError, match="deleting R2 object"):
        dest.delete("some-key")


def test_build_r2_client_without_boto3_raises_config_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_boto3(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("no boto3 here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    from mnemos.tools.styx.destination import R2Credentials, build_r2_client

    creds = R2Credentials(account_id="acct", bucket="b", access_key_id="k", secret_access_key="s")
    with pytest.raises(StyxConfigError, match="boto3"):
        build_r2_client(creds)


# ── rclone ───────────────────────────────────────────────────────────────


class _FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_rclone_binary(monkeypatch):
    monkeypatch.setattr("mnemos.tools.styx.destination.shutil.which", lambda _: "/usr/bin/rclone")


def test_rclone_missing_binary_is_a_config_error(monkeypatch):
    monkeypatch.setattr("mnemos.tools.styx.destination.shutil.which", lambda _: None)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")
    with pytest.raises(StyxConfigError, match="rclone"):
        dest.list_artifacts("pythia")


def test_rclone_upload_verifies_via_a_fresh_lsjson(monkeypatch, tmp_path):
    _patch_rclone_binary(monkeypatch)
    path = tmp_path / "bundle.age"
    path.write_bytes(b"ciphertext")
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "copyto":
            return _FakeCompletedProcess()
        if argv[1] == "lsjson":
            payload = [
                {
                    "Name": "mnemos-pythia-x.mif.tar.gz.age",
                    "Size": 10,
                    "IsDir": False,
                    "Hashes": {"md5": "abc123"},
                }
            ]
            return _FakeCompletedProcess(stdout=json.dumps(payload))
        raise AssertionError(f"unexpected rclone subcommand {argv}")

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")

    artifact = dest.upload(path, "mnemos-pythia-x.mif.tar.gz.age")

    assert artifact.md5 == "abc123"
    assert artifact.size == 10
    # copyto happened before the verifying lsjson re-read.
    assert calls[0][1] == "copyto"
    assert calls[1][1] == "lsjson"
    # Never invoked through a shell string — always argv[0] is the binary itself.
    assert calls[0][0] == "/usr/bin/rclone"


def test_rclone_stat_of_missing_object_is_a_styx_upload_error(monkeypatch):
    _patch_rclone_binary(monkeypatch)

    def _fake_run(argv, **kwargs):
        return _FakeCompletedProcess(stdout="[]")

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")
    with pytest.raises(StyxUploadError, match="no such object"):
        dest.stat("gdrive-personal:mnemos-backups/nope.mif.tar.gz.age")


def test_rclone_list_artifacts_filters_dirs_and_host(monkeypatch):
    _patch_rclone_binary(monkeypatch)
    payload = [
        {
            "Name": "mnemos-pythia-a.mif.tar.gz.age",
            "Size": 5,
            "IsDir": False,
            "Hashes": {"md5": "1"},
        },
        {
            "Name": "mnemos-other-a.mif.tar.gz.age",
            "Size": 5,
            "IsDir": False,
            "Hashes": {"md5": "2"},
        },
        {"Name": "subdir", "Size": 0, "IsDir": True},
    ]

    def _fake_run(argv, **kwargs):
        return _FakeCompletedProcess(stdout=json.dumps(payload))

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")

    found = dest.list_artifacts("pythia")

    assert [a.name for a in found] == ["mnemos-pythia-a.mif.tar.gz.age"]


def test_rclone_list_artifacts_rejects_objects_without_the_age_suffix(monkeypatch):
    """Same tightening as Drive and R2: the prefix is a substring match,
    so a sidecar or hand-uploaded file with the right name fragment would
    be counted toward retention. Require the suffix too."""
    _patch_rclone_binary(monkeypatch)
    payload = [
        {
            "Name": "mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
            "Size": 5,
            "IsDir": False,
            "Hashes": {"md5": "1"},
        },
        # Right host, wrong suffix — must be filtered out.
        {
            "Name": "mnemos-pythia-debug.txt",
            "Size": 5,
            "IsDir": False,
            "Hashes": {"md5": "2"},
        },
        # Right suffix, wrong host — must also be filtered out.
        {
            "Name": "mnemos-otherhost-20260917T024000Z.mif.tar.gz.age",
            "Size": 5,
            "IsDir": False,
            "Hashes": {"md5": "3"},
        },
    ]

    def _fake_run(argv, **kwargs):
        return _FakeCompletedProcess(stdout=json.dumps(payload))

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")

    found = dest.list_artifacts("pythia")
    assert [a.name for a in found] == ["mnemos-pythia-20260917T024000Z.mif.tar.gz.age"]


def test_rclone_nonzero_exit_is_a_styx_upload_error(monkeypatch):
    _patch_rclone_binary(monkeypatch)

    def _fake_run(argv, **kwargs):
        return _FakeCompletedProcess(stderr="remote error: quota exceeded", returncode=1)

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")
    with pytest.raises(StyxUploadError, match="quota exceeded"):
        dest.list_artifacts("pythia")


def test_rclone_timeout_is_a_styx_upload_error(monkeypatch):
    _patch_rclone_binary(monkeypatch)

    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=900)

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    dest = RcloneDestination("gdrive-personal:mnemos-backups")
    with pytest.raises(StyxUploadError, match="timed out"):
        dest.list_artifacts("pythia")


def test_rclone_never_shells_out_with_shell_true(monkeypatch, tmp_path):
    """Defense-in-depth: even if a name were hostile, there is no shell to inject into."""
    _patch_rclone_binary(monkeypatch)
    seen_kwargs = {}

    def _fake_run(argv, **kwargs):
        seen_kwargs.update(kwargs)
        return _FakeCompletedProcess(stdout="[]")

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    RcloneDestination("gdrive-personal:mnemos-backups").list_artifacts("pythia")

    assert seen_kwargs.get("shell", False) is False


def test_rclone_delete_calls_deletefile(monkeypatch):
    _patch_rclone_binary(monkeypatch)
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeCompletedProcess()

    monkeypatch.setattr("mnemos.tools.styx.destination.subprocess.run", _fake_run)
    RcloneDestination("gdrive-personal:mnemos-backups").delete(
        "gdrive-personal:mnemos-backups/mnemos-pythia-x.mif.tar.gz.age"
    )

    assert calls[0][1:] == [
        "deletefile",
        "gdrive-personal:mnemos-backups/mnemos-pythia-x.mif.tar.gz.age",
    ]


# ── selector factory + config validation ──────────────────────────────────


def _config(**overrides) -> StyxConfig:
    base = {
        "age_recipient": RECIPIENT,
        "sqlite_path": Path("/var/lib/mnemos/mnemos.sqlite3"),
        "host_label": "pythia",
    }
    base.update(overrides)
    return StyxConfig(**base)


def test_gdrive_is_the_default_destination_kind():
    config = _config(gdrive_folder_id="folder-1")
    assert config.destination_kind == DEST_GDRIVE


def test_unknown_destination_kind_is_refused():
    with pytest.raises(StyxConfigError, match="MNEMOS_STYX_DESTINATION"):
        _config(destination_kind="dropbox")


def test_r2_destination_requires_all_four_credentials():
    with pytest.raises(StyxConfigError, match="r2.*requires"):
        _config(destination_kind=DEST_R2, r2_bucket="b")


def test_r2_destination_accepts_complete_config():
    config = _config(
        destination_kind=DEST_R2,
        r2_account_id="acct",
        r2_bucket="styx-backups",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
    )
    assert config.destination_kind == DEST_R2


def test_rclone_destination_requires_a_remote():
    with pytest.raises(StyxConfigError, match="rclone.*requires"):
        _config(destination_kind=DEST_RCLONE)


def test_rclone_destination_accepts_a_remote():
    config = _config(destination_kind=DEST_RCLONE, rclone_remote="gdrive-personal:backups")
    assert config.rclone_remote == "gdrive-personal:backups"


def test_build_destination_dispatches_to_r2(monkeypatch):
    config = _config(
        destination_kind=DEST_R2,
        r2_account_id="acct",
        r2_bucket="styx-backups",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
    )
    monkeypatch.setattr("mnemos.tools.styx.destination.build_r2_client", lambda creds: _FakeR2Client())
    dest = build_destination(config)
    assert isinstance(dest, CloudflareR2Destination)


def test_build_destination_dispatches_to_rclone():
    config = _config(destination_kind=DEST_RCLONE, rclone_remote="gdrive-personal:backups")
    dest = build_destination(config)
    assert isinstance(dest, RcloneDestination)


def test_build_destination_dispatches_to_gdrive(monkeypatch):
    config = _config(gdrive_folder_id="folder-1")
    monkeypatch.setattr("mnemos.tools.styx.drive.build_drive_service", lambda creds: object())
    from mnemos.tools.styx.drive import DriveBackupFolder

    dest = build_destination(config)
    assert isinstance(dest, DriveBackupFolder)
