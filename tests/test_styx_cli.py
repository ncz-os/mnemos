"""STYX command line.

The keygen path earned its own test the hard way: it shipped calling a
pyrage method that does not exist, and every other test passed because none
of them ever invoked the CLI. A backup tool whose key ceremony crashes is
useless no matter how good the rest of it is.

The ``verify`` path earned its own test for the opposite reason: it shipped
hard-coding the Drive ``folder_id`` field in its output even when STYX was
configured to talk to Cloudflare R2 or an rclone remote, so an operator
running ``mnemos.styx verify`` got a JSON payload that silently named a
folder STYX was never going to write to.
"""

from __future__ import annotations

import json

import pytest

from mnemos.tools.styx.__main__ import main
from mnemos.tools.styx.config import (
    DEST_R2,
    DEST_RCLONE,
)
from mnemos.tools.styx.retention import RemoteArtifact

pyrage = pytest.importorskip("pyrage", reason="STYX encryption needs the styx extra")


RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3n0qmt"


class _FakeDestination:
    """Minimal stand-in matching the four-method destination protocol."""

    def __init__(self, artifacts: list[RemoteArtifact] | None = None):
        self._artifacts = artifacts or []
        self.constructed_with = "called"

    def upload(self, path, name):  # pragma: no cover - verify path does not upload
        raise NotImplementedError

    def stat(self, file_id):  # pragma: no cover - verify path does not stat
        raise NotImplementedError

    def list_artifacts(self, host_label=None):
        return list(self._artifacts)

    def delete(self, file_id):  # pragma: no cover - verify path does not delete
        raise NotImplementedError


@pytest.fixture
def gdrive_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_STYX_AGE_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("MNEMOS_STYX_GDRIVE_FOLDER_ID", "folder-abc")
    monkeypatch.setenv("MNEMOS_STYX_SQLITE_PATH", "/var/lib/mnemos/mnemos.sqlite3")
    monkeypatch.setenv("MNEMOS_STYX_HOST_LABEL", "pythia")
    monkeypatch.delenv("MNEMOS_STYX_DESTINATION", raising=False)
    return monkeypatch


@pytest.fixture
def r2_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_STYX_AGE_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("MNEMOS_STYX_DESTINATION", DEST_R2)
    monkeypatch.setenv("MNEMOS_STYX_R2_ACCOUNT_ID", "acct-xyz")
    monkeypatch.setenv("MNEMOS_STYX_R2_BUCKET", "styx-backups")
    monkeypatch.setenv("MNEMOS_STYX_R2_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("MNEMOS_STYX_R2_SECRET_ACCESS_KEY", "secret-fake")
    monkeypatch.setenv("MNEMOS_STYX_R2_PREFIX", "host-a/encrypted")
    monkeypatch.setenv("MNEMOS_STYX_SQLITE_PATH", "/var/lib/mnemos/mnemos.sqlite3")
    monkeypatch.setenv("MNEMOS_STYX_HOST_LABEL", "pythia")
    return monkeypatch


@pytest.fixture
def rclone_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_STYX_AGE_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("MNEMOS_STYX_DESTINATION", DEST_RCLONE)
    monkeypatch.setenv("MNEMOS_STYX_RCLONE_REMOTE", "gdrive-personal:mnemos-backups")
    monkeypatch.setenv("MNEMOS_STYX_SQLITE_PATH", "/var/lib/mnemos/mnemos.sqlite3")
    monkeypatch.setenv("MNEMOS_STYX_HOST_LABEL", "pythia")
    return monkeypatch


def test_keygen_emits_a_usable_keypair(capsys):
    assert main(["keygen"]) == 0
    out = capsys.readouterr().out

    identity_line = next(ln for ln in out.splitlines() if "AGE-SECRET-KEY-1" in ln)
    recipient_line = next(ln for ln in out.splitlines() if "MNEMOS_STYX_AGE_RECIPIENT=" in ln)

    identity = pyrage.x25519.Identity.from_str(identity_line.strip())
    recipient = recipient_line.split("=", 1)[1].strip()
    assert recipient.startswith("age1")

    # The pair it printed must actually work together.
    blob = pyrage.encrypt(b"canary", [pyrage.x25519.Recipient.from_str(recipient)])
    assert pyrage.decrypt(blob, [identity]) == b"canary"


def test_keygen_warns_the_private_key_must_stay_off_the_fleet(capsys):
    main(["keygen"])
    out = capsys.readouterr().out.lower()
    assert "never" in out
    assert "decrypt" in out


def test_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        main(["not-a-command"])


# ── verify is destination-aware ──────────────────────────────────────────
#
# ``verify`` answers "where are my backups going?". For a tool whose entire
# reason for being is that the previous backup job was reporting success
# while writing nothing, the output MUST name the actual destination — for
# every destination kind — so an operator reading it cannot be fooled into
# thinking the run is going where it is not.


def test_verify_for_gdrive_reports_folder_id(gdrive_env, monkeypatch, capsys):
    artifact = RemoteArtifact(
        file_id="file-1",
        name="mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        size=10,
        md5="abc",
    )
    monkeypatch.setattr(
        "mnemos.tools.styx.__main__.build_destination",
        lambda _cfg: _FakeDestination([artifact]),
    )

    rc = main(["verify"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["destination"] == {
        "kind": "gdrive-service-account",
        "folder_id": "folder-abc",
    }
    # The legacy top-level ``folder_id`` field is preserved for existing tooling.
    assert payload["folder_id"] == "folder-abc"
    assert payload["total"] == 1


def test_verify_for_r2_reports_bucket_and_prefix(r2_env, monkeypatch, capsys):
    artifact = RemoteArtifact(
        file_id="host-a/encrypted/mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        name="mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        size=10,
        md5="abc",
    )
    monkeypatch.setattr(
        "mnemos.tools.styx.__main__.build_destination",
        lambda _cfg: _FakeDestination([artifact]),
    )

    rc = main(["verify"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    dest = payload["destination"]
    assert dest["kind"] == "r2"
    assert dest["bucket"] == "styx-backups"
    assert dest["prefix"] == "host-a/encrypted"
    # The locator format is the one an operator can read at a glance and
    # recognize as "this is my R2 bucket, this is my prefix".
    assert dest["locator"] == "r2://styx-backups/host-a/encrypted"
    # Verify does NOT echo the legacy Drive field for non-Drive destinations.
    assert "folder_id" not in payload


def test_verify_for_rclone_reports_remote(rclone_env, monkeypatch, capsys):
    artifact = RemoteArtifact(
        file_id="gdrive-personal:mnemos-backups/mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        name="mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        size=10,
        md5="abc",
    )
    monkeypatch.setattr(
        "mnemos.tools.styx.__main__.build_destination",
        lambda _cfg: _FakeDestination([artifact]),
    )

    rc = main(["verify"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["destination"] == {
        "kind": "rclone",
        "remote": "gdrive-personal:mnemos-backups",
    }
    assert "folder_id" not in payload


def test_verify_with_no_artifacts_exits_nonzero(gdrive_env, monkeypatch, capsys):
    monkeypatch.setattr(
        "mnemos.tools.styx.__main__.build_destination",
        lambda _cfg: _FakeDestination([]),
    )

    rc = main(["verify"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no STYX artifacts found" in err


def test_verify_r2_locator_handles_a_blank_prefix(r2_env, monkeypatch, capsys):
    """A bucket with no prefix is a valid deployment; the locator must
    still render cleanly without a trailing slash."""
    monkeypatch.setenv("MNEMOS_STYX_R2_PREFIX", "")
    artifact = RemoteArtifact(
        file_id="mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        name="mnemos-pythia-20260917T024000Z.mif.tar.gz.age",
        size=10,
        md5="abc",
    )
    monkeypatch.setattr(
        "mnemos.tools.styx.__main__.build_destination",
        lambda _cfg: _FakeDestination([artifact]),
    )

    rc = main(["verify"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    dest = payload["destination"]
    assert dest["prefix"] == ""
    assert dest["locator"] == "r2://styx-backups"
