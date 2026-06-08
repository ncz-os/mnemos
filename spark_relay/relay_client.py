"""GCS transport for the Spark<->PYTHIA relay.

Bucket layout (every ``*.json.enc`` value is a sealed blob from
:mod:`relay_crypto`; ``claimed/<uuid>`` holds a small JSON lease marker)::

    pending/<uuid>.json.enc     enqueuer (PYTHIA) writes; Spark consumes
    claimed/<uuid>              Spark conditional-creates = lease lock (exactly-once)
    terminal/<uuid>.json.enc    Spark writes done OR failed (status in payload);
                                reconciler (PYTHIA) consumes

The atomic primitive is :meth:`RelayClient.claim`, a conditional create with
``if_generation_match=0`` ("create only if absent"). Two pollers racing the same
job → exactly one wins; the loser gets ``PreconditionFailed`` and backs off.
No external lock service is involved.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

_PENDING = "pending/"
_CLAIMED = "claimed/"
# Single terminal prefix (not split done/failed): one create-only object per job
# is the exactly-once gate, so two workers can never record conflicting terminal
# states for the same uuid. The done-vs-failed distinction lives in the sealed
# payload's "status" field.
_TERMINAL = "terminal/"
# Out-of-band status objects (e.g. GPU telemetry). Overwrite-allowed (latest
# wins), unlike the create-only terminal/claimed objects.
_STATUS = "status/"
_SUFFIX = ".json.enc"

# A claim older than this (seconds) is considered abandoned (worker died
# mid-job) and may be taken over. Must exceed the longest expected NGC job.
DEFAULT_LEASE_SECONDS = 7200.0


@dataclass(frozen=True)
class RelayConfig:
    bucket: str
    sa_key_path: str

    @classmethod
    def from_env(cls) -> "RelayConfig":
        bucket = os.environ.get("SPARK_HIVE_RELAY_BUCKET")
        sa_path = os.environ.get("SPARK_HIVE_RELAY_GCS_SA")
        if not bucket:
            raise RuntimeError("set $SPARK_HIVE_RELAY_BUCKET")
        if not sa_path:
            raise RuntimeError("set $SPARK_HIVE_RELAY_GCS_SA (path to SA key json)")
        return cls(bucket=bucket, sa_key_path=sa_path)


class RelayClient:
    """Thin GCS wrapper exposing the relay's queue operations."""

    def __init__(self, config: RelayConfig | None = None) -> None:
        # Lazy import: the module stays importable (tests, CI, PYTHIA-side type
        # checks) without google-cloud-storage installed; only constructing a
        # live client requires it.
        from google.api_core.exceptions import NotFound, PreconditionFailed
        from google.cloud import storage

        self._NotFound = NotFound
        self._PreconditionFailed = PreconditionFailed
        self._cfg = config or RelayConfig.from_env()
        self._client = storage.Client.from_service_account_json(self._cfg.sa_key_path)
        self._bucket = self._client.bucket(self._cfg.bucket)

    # ---- enqueuer (PYTHIA) ----------------------------------------------
    def put_pending(self, uuid: str, sealed: bytes) -> None:
        self._bucket.blob(f"{_PENDING}{uuid}{_SUFFIX}").upload_from_string(
            sealed, content_type="application/octet-stream"
        )

    # ---- Spark poller ---------------------------------------------------
    def list_pending(self) -> list[str]:
        return [
            b.name[len(_PENDING) : -len(_SUFFIX)]
            for b in self._client.list_blobs(self._bucket, prefix=_PENDING)
            if b.name.endswith(_SUFFIX)
        ]

    def claim(
        self,
        uuid: str,
        owner: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Atomically claim a job with a lease. True = this caller owns it now.

        Fresh claim = conditional create on ``claimed/<uuid>`` (exactly-once).
        If a claim already exists, it is taken over only when older than
        ``lease_seconds`` (the prior worker is presumed dead), via a
        compare-and-swap on the object generation so two reclaimers can't both
        win. A live claim returns False.
        """
        now = time.time() if now is None else now
        blob = self._bucket.blob(f"{_CLAIMED}{uuid}")
        body = json.dumps({"owner": owner, "claimed_at": now})
        try:
            blob.upload_from_string(body, if_generation_match=0)
            return True
        except self._PreconditionFailed:
            pass
        # Claim exists — load its metadata (generation) for a safe compare-and-
        # swap takeover. get_blob() populates generation; a bare bucket.blob()
        # handle would leave it None and make if_generation_match unconditional.
        existing = self._bucket.get_blob(f"{_CLAIMED}{uuid}")
        if existing is None or existing.generation is None:
            return False  # vanished or no usable generation — don't risk a blind overwrite
        try:
            claimed_at = float(json.loads(existing.download_as_bytes()).get("claimed_at", 0))
        except (self._NotFound, ValueError, TypeError):
            claimed_at = 0.0  # unparseable/empty marker => treat as expired, take over
        if now - claimed_at < lease_seconds:
            return False  # lease still live
        try:
            blob.upload_from_string(body, if_generation_match=existing.generation)
            return True
        except self._PreconditionFailed:
            return False  # another reclaimer took it first

    def get_pending(self, uuid: str) -> bytes:
        return self._bucket.blob(f"{_PENDING}{uuid}{_SUFFIX}").download_as_bytes()

    def put_terminal(self, uuid: str, sealed: bytes) -> bool:
        """Write the single terminal object for a job, create-only.

        False = a terminal object already exists (idempotent: the first writer
        wins; duplicate/late executions are dropped, and done/failed can never
        both be recorded for one uuid)."""
        try:
            self._bucket.blob(f"{_TERMINAL}{uuid}{_SUFFIX}").upload_from_string(
                sealed,
                if_generation_match=0,
                content_type="application/octet-stream",
            )
            return True
        except self._PreconditionFailed:
            return False

    # ---- reconciler (PYTHIA) --------------------------------------------
    def list_terminal(self) -> list[str]:
        return [
            b.name[len(_TERMINAL) : -len(_SUFFIX)]
            for b in self._client.list_blobs(self._bucket, prefix=_TERMINAL)
            if b.name.endswith(_SUFFIX)
        ]

    def get_terminal(self, uuid: str) -> bytes:
        return self._bucket.blob(f"{_TERMINAL}{uuid}{_SUFFIX}").download_as_bytes()

    # ---- out-of-band status (GPU telemetry etc.) ------------------------
    def put_status(self, name: str, sealed: bytes) -> None:
        """Write/overwrite a status object (latest wins)."""
        self._bucket.blob(f"{_STATUS}{name}{_SUFFIX}").upload_from_string(
            sealed, content_type="application/octet-stream"
        )

    def get_status(self, name: str) -> bytes | None:
        """Read a status object, or None if absent."""
        try:
            return self._bucket.blob(f"{_STATUS}{name}{_SUFFIX}").download_as_bytes()
        except self._NotFound:
            return None

    def purge(self, uuid: str) -> None:
        """Delete every object for a job across all prefixes. Idempotent."""
        for name in (
            f"{_PENDING}{uuid}{_SUFFIX}",
            f"{_CLAIMED}{uuid}",
            f"{_TERMINAL}{uuid}{_SUFFIX}",
        ):
            try:
                self._bucket.blob(name).delete()
            except self._NotFound:
                pass
