"""v6.2 M-2.2.1 audit-chain sealer worker.

Periodic loop (default 60s cadence) that:

1. Claims unsealed entries via ``backend.audit_chain.claim_unsealed_window``
   (FOR UPDATE SKIP LOCKED on PG / Oracle / Db2; single-writer on SQLite).
2. Builds a Merkle tree over ``SHA-256(entry_id || signature)`` leaves,
   pad-to-power-of-two with zeros (see ``mnemos.audit.merkle_root``).
3. Signs the root with the per-instance Ed25519 root key from
   ``MNEMOS_AUDIT_ROOT_PRIVKEY``.
4. INSERTs the (global_root, signature, window) row into
   ``memory_audit_roots`` AND UPDATEs each claimed entry's
   ``global_root`` + ``global_seq`` — both writes in the same tx so
   the seal commits atomically.

Design ref: docs/v6.2-nexus-pattern-adoption.md § 1 "Global Merkle root".

The whole sealer is no-op when ``MNEMOS_AUDIT_CHAIN`` is unset or set
to something other than ``on`` — same opt-in switch the route handler
checks before calling ``build_entry``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from mnemos.audit import (
    load_root_keypair,
    merkle_leaf,
    merkle_root,
)
from mnemos.core.config import audit_chain_enabled_flag
from mnemos.persistence.base import AuditPersistence

logger = logging.getLogger(__name__)


DEFAULT_WINDOW_SECONDS = 60
DEFAULT_BATCH_SIZE = 1000
DEFAULT_POLL_INTERVAL = 60


def _coerce_iso8601(value: Any) -> str:
    """Normalize a backend-provided ``signed_at`` to ISO 8601 string.

    Postgres returns ``datetime``; Oracle / Db2 also return ``datetime``;
    SQLite returns ``str`` already. Format mask matches what
    ``OracleAuditChainRepository.insert_audit_entry`` accepts.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)


class AuditSealer:
    """Sealer worker bound to a single backend.

    Spawn one per backend. The claim path uses SKIP LOCKED so multiple
    sealer instances against the SAME backend coexist on PG / Oracle /
    Db2 (e.g. for high availability); single-writer SQLite would
    deadlock, so deploy one sealer per SQLite node.
    """

    def __init__(
        self,
        backend: AuditPersistence,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        if backend.audit_chain is None:
            raise ValueError(
                "backend has no audit_chain repository; cannot seal (check MNEMOS_AUDIT_CHAIN env + backend impl)"
            )
        self._backend = backend
        self._window_seconds = max(int(window_seconds), 1)
        self._batch_size = max(int(batch_size), 1)
        self._poll_interval = max(int(poll_interval), 1)
        self._root_priv, self._root_pub = load_root_keypair()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def root_pubkey(self) -> bytes:
        return self._root_pub

    async def run_once(self) -> int:
        """Seal one window. Returns number of entries sealed.

        Empty window returns 0 — sealer caller sleeps and retries.
        """
        async with self._backend.transactional() as tx:
            claimed = await self._backend.audit_chain.claim_unsealed_window(
                tx,
                max_window_seconds=self._window_seconds,
                limit=self._batch_size,
            )
            if not claimed:
                return 0

            # Build Merkle tree over leaf-hashed entries.
            leaves = [merkle_leaf(r["entry_id"], r["signature"]) for r in claimed]
            root = merkle_root(leaves)

            # Sign the root with the per-instance root key.
            root_signature = self._root_priv.sign(root)

            # Window timestamps: earliest claimed signed_at → now.
            signed_ats = sorted(_coerce_iso8601(r["signed_at"]) for r in claimed)
            window_start = signed_ats[0]
            sealed_at = datetime.now(tz=timezone.utc).isoformat()
            window_end = sealed_at

            entry_ids = [r["entry_id"] for r in claimed]
            await self._backend.audit_chain.stamp_window_with_root(
                tx,
                entry_ids=entry_ids,
                global_root=root,
                starting_seq=1,
            )
            await self._backend.audit_chain.insert_audit_root(
                tx,
                global_root=root,
                window_start=window_start,
                window_end=window_end,
                entry_count=len(claimed),
                root_signature=root_signature,
                signer_pubkey=self._root_pub,
                sealed_at=sealed_at,
            )

        logger.info(
            "[AUDIT_SEALER] sealed n=%d window=[%s..%s] root=%s",
            len(claimed),
            window_start,
            window_end,
            root.hex()[:16],
        )
        return len(claimed)

    async def run_forever(self) -> None:
        """Run the seal loop until ``stop()`` is called.

        Exceptions inside `run_once` are logged and swallowed — the
        next poll retries. Persistent failures show in logs but don't
        kill the worker.
        """
        logger.info(
            "[AUDIT_SEALER] starting: window=%ds batch=%d poll=%ds pubkey=%s",
            self._window_seconds,
            self._batch_size,
            self._poll_interval,
            self._root_pub.hex()[:16],
        )
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - log + continue is the spec
                logger.exception("[AUDIT_SEALER] run_once raised; continuing")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("[AUDIT_SEALER] stopped")

    def stop(self) -> None:
        self._stop.set()

    async def start_background(self) -> asyncio.Task:
        """Launch ``run_forever`` as a background task."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run_forever())
        return self._task


def audit_chain_enabled() -> bool:
    """Whether sealer + route-side audit writes are active.

    Honors ``MNEMOS_AUDIT_CHAIN``: ``on`` enables the chain; anything
    else disables it. Same opt-in semantics as in spec § 1
    "MNEMOS_AUDIT_CHAIN=on (default in v6.2)".
    """
    return audit_chain_enabled_flag()
