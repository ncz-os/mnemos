"""Orchestration tests for poller + reconciler using an in-memory fake bucket.

Exercises the claim/execute/result/reconcile flow end-to-end without GCS or the
network, so the relay's control logic is covered in CI. The live GCS conditional
-claim primitive itself is validated by the provisioning smoke test (see
docs/SPARK_HIVE_BRIDGE.md) and is not re-mocked here.
"""

from __future__ import annotations

import os

import pytest

from spark_relay import relay_crypto, reconciler, spark_poller


class FakeRelay:
    """In-memory stand-in for RelayClient. Models per-prefix object stores +
    the exactly-once semantics of conditional-claim."""

    def __init__(self) -> None:
        self.pending: dict[str, bytes] = {}
        self.claimed: dict[str, str] = {}
        self.terminal: dict[str, bytes] = {}

    # enqueuer
    def put_pending(self, uuid: str, sealed: bytes) -> None:
        self.pending[uuid] = sealed

    # poller
    def list_pending(self) -> list[str]:
        return list(self.pending)

    def claim(self, uuid: str, owner: str, **_: object) -> bool:
        if uuid in self.claimed:
            return False
        self.claimed[uuid] = owner
        return True

    def get_pending(self, uuid: str) -> bytes:
        return self.pending[uuid]

    def put_terminal(self, uuid: str, sealed: bytes) -> bool:
        if uuid in self.terminal:
            return False  # create-only: first terminal write wins
        self.terminal[uuid] = sealed
        return True

    # reconciler
    def list_terminal(self) -> list[str]:
        return list(self.terminal)

    def get_terminal(self, uuid: str) -> bytes:
        return self.terminal[uuid]

    def purge(self, uuid: str) -> None:
        self.pending.pop(uuid, None)
        self.claimed.pop(uuid, None)
        self.terminal.pop(uuid, None)


class OkExecutor:
    def execute(self, job: dict) -> dict:
        return {"commit_sha": "deadbeef", "branch": "main", "metrics": {"ok": True}}


class BoomExecutor:
    def execute(self, job: dict) -> dict:
        raise RuntimeError("kaboom")


class RecordingHive:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._ok = ok

    def patch_status(self, job_id: str, status: str, *, result: dict | None = None) -> bool:
        self.calls.append((job_id, status, result or {}))
        return self._ok


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def _seed(relay: FakeRelay, key: bytes, uuid: str = "job-1") -> str:
    relay.put_pending(
        uuid,
        relay_crypto.seal({"job_id": uuid, "prompt": "hi"}, key, aad=relay_crypto.aad_for("pending", uuid)),
    )
    return uuid


def _open_terminal(relay: FakeRelay, key: bytes, uuid: str) -> dict:
    return relay_crypto.open_blob(relay.terminal[uuid], key, aad=relay_crypto.aad_for("terminal", uuid))


def test_poller_success_path(key: bytes) -> None:
    relay = FakeRelay()
    uuid = _seed(relay, key)
    assert spark_poller.run_once(relay, key, OkExecutor()) == 1
    result = _open_terminal(relay, key, uuid)
    assert result["status"] == "done"
    assert result["commit_sha"] == "deadbeef"


def test_poller_failure_path(key: bytes) -> None:
    relay = FakeRelay()
    uuid = _seed(relay, key)
    spark_poller.run_once(relay, key, BoomExecutor())
    failed = _open_terminal(relay, key, uuid)
    assert failed["status"] == "failed"
    assert "kaboom" in failed["error"]


def test_poller_skips_already_claimed(key: bytes) -> None:
    relay = FakeRelay()
    uuid = _seed(relay, key)
    relay.claim(uuid, "other-worker")  # someone else owns it
    assert spark_poller.run_once(relay, key, OkExecutor()) == 0
    assert uuid not in relay.terminal


def test_poller_quarantines_undecryptable(key: bytes) -> None:
    relay = FakeRelay()
    uuid = "bad-1"
    relay.put_pending(uuid, b"SHR1\x01" + b"\x00" * 40)  # garbage, won't decrypt
    spark_poller.run_once(relay, key, OkExecutor())
    term = _open_terminal(relay, key, uuid)  # durable terminal, claim not stranded
    assert term["status"] == "failed"
    assert "undecryptable" in term["error"]


def test_poller_quarantines_uuid_mismatch(key: bytes) -> None:
    relay = FakeRelay()
    uuid = "obj-1"
    relay.put_pending(
        uuid,
        relay_crypto.seal({"job_id": "DIFFERENT", "prompt": "x"}, key, aad=relay_crypto.aad_for("pending", uuid)),
    )
    spark_poller.run_once(relay, key, OkExecutor())
    assert "mismatch" in _open_terminal(relay, key, uuid)["error"]


def test_poller_keeps_first_terminal(key: bytes) -> None:
    """A pre-existing terminal object (e.g. lease takeover re-run) is not clobbered."""
    relay = FakeRelay()
    uuid = _seed(relay, key)
    relay.put_terminal(
        uuid,
        relay_crypto.seal({"status": "done", "commit_sha": "first"}, key, aad=relay_crypto.aad_for("terminal", uuid)),
    )
    spark_poller.run_once(relay, key, OkExecutor())
    assert _open_terminal(relay, key, uuid)["commit_sha"] == "first"


def test_reconciler_done(key: bytes) -> None:
    relay, hive = FakeRelay(), RecordingHive()
    uuid = "job-9"
    relay.put_terminal(
        uuid,
        relay_crypto.seal({"status": "done", "commit_sha": "abc123"}, key, aad=relay_crypto.aad_for("terminal", uuid)),
    )
    assert reconciler.run_once(hive, relay, key) == 1
    assert hive.calls[0][:2] == (uuid, "done")
    assert hive.calls[0][2]["commit_sha"] == "abc123"
    assert uuid not in relay.terminal  # purged


def test_reconciler_failed(key: bytes) -> None:
    relay, hive = FakeRelay(), RecordingHive()
    uuid = "job-x"
    relay.put_terminal(
        uuid,
        relay_crypto.seal({"status": "failed", "error": "oom"}, key, aad=relay_crypto.aad_for("terminal", uuid)),
    )
    reconciler.run_once(hive, relay, key)
    assert hive.calls[0][1] == "failed"
    assert hive.calls[0][2]["error"] == "oom"
    assert uuid not in relay.terminal


def test_reconciler_keeps_objects_when_patch_fails(key: bytes) -> None:
    relay, hive = FakeRelay(), RecordingHive(ok=False)
    uuid = "job-keep"
    relay.put_terminal(
        uuid,
        relay_crypto.seal({"status": "done", "commit_sha": "z"}, key, aad=relay_crypto.aad_for("terminal", uuid)),
    )
    assert reconciler.run_once(hive, relay, key) == 0
    assert uuid in relay.terminal  # NOT purged — hive never acked


def test_full_roundtrip(key: bytes) -> None:
    relay, hive = FakeRelay(), RecordingHive()
    _seed(relay, key, "rt-1")
    spark_poller.run_once(relay, key, OkExecutor())
    reconciler.run_once(hive, relay, key)
    assert hive.calls[0][:2] == ("rt-1", "done")
    assert not relay.pending and not relay.terminal
