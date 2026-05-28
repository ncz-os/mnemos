"""Route-handler bridge for v6.2 M-2.2.1 audit-chain writes.

Memory IDs in production are the string format ``mem_<timestamp>_<hex6>``
(per ``mnemos.core.ids.new_memory_id``), not 16-byte UUIDs. The audit
schema uses RAW(16) / BYTEA(16) / BLOB columns for `memory_id` — we
bridge by taking the first 16 bytes of ``SHA-256(memory_id_str)`` as
the canonical audit-side memory key. Hashing is deterministic so
lookups still work; the actual string mem_id stays in the memories
table for joinability.

Public surface::

    from mnemos.audit.route_helper import (
        memory_id_to_audit_bytes,
        write_audit_entry,
    )
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal

from .crypto import canonical_payload_hash
from .writer import build_entry, latest_hash
from mnemos.persistence.base import AuditPersistence

logger = logging.getLogger(__name__)

AuditOp = Literal["create", "update", "delete", "archive", "replicate"]


def memory_id_to_audit_bytes(memory_id_str: str) -> bytes:
    """Deterministic 16-byte audit key for a string memory_id."""
    if not memory_id_str:
        raise ValueError("memory_id_str is empty")
    return hashlib.sha256(memory_id_str.encode("utf-8")).digest()[:16]


async def write_audit_entry(
    backend: AuditPersistence,
    tx: Any,
    *,
    op: AuditOp,
    memory_id_str: str,
    content: str,
    category: str,
    subcategory: str | None,
    metadata: dict[str, Any] | None,
    embedding: bytes | None,
    writer_id: str,
    session_secret: bytes,
) -> None:
    """Build + insert one audit entry inside the caller's tx.

    Fetches the prior entry for this memory (via SHA-256 16-byte key)
    to populate ``prev_entry_id`` + ``prev_entry_hash``. Computes
    payload_hash, signs the new entry with the writer's HKDF-derived
    Ed25519 key, then INSERTs.

    Errors are LOGGED but not re-raised — the audit chain is a
    consistency-guarantee on top of the write, not a write
    prerequisite. We must never break a memory write if audit
    insertion fails (e.g. transient backend hiccup). Sealer's
    eventual replay over the unsealed window catches any genuine
    durability gaps.
    """
    if backend.audit_chain is None:
        return  # backend hasn't shipped audit_chain; silently no-op

    try:
        memory_id_bytes = memory_id_to_audit_bytes(memory_id_str)
        prev_row = await backend.audit_chain.get_latest_audit_entry(tx, memory_id_bytes)
        prev_entry_id: bytes | None = None
        prev_entry_hash: bytes | None = None
        if prev_row is not None:
            prev_entry_id = prev_row["entry_id"]
            # Reconstruct prev_entry_hash from the prior row's canonical
            # bytes + signature. We don't persist latest_hash as a
            # separate column today; recompute from the row.
            from .crypto import AuditEntry as _AE

            prev_ae = _AE(
                entry_id=prev_row["entry_id"],
                memory_id=prev_row["memory_id"],
                prev_entry_id=prev_row.get("prev_entry_id"),
                prev_entry_hash=prev_row.get("prev_entry_hash"),
                op=prev_row["op"],
                payload_hash=prev_row["payload_hash"],
                writer_id=prev_row["writer_id"],
                writer_pubkey=prev_row["writer_pubkey"],
                signed_at=_to_iso(prev_row["signed_at"]),
            )
            prev_entry_hash = latest_hash(prev_ae, prev_row["signature"])

        payload_hash = canonical_payload_hash(
            memory_id=memory_id_str,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata=metadata,
            embedding=embedding,
        )
        entry, signature = build_entry(
            op=op,
            memory_id=memory_id_bytes,
            prev_entry_id=prev_entry_id,
            prev_entry_hash=prev_entry_hash,
            payload_hash=payload_hash,
            writer_id=writer_id,
            session_secret=session_secret,
        )
        await backend.audit_chain.insert_audit_entry(
            tx,
            entry_id=entry.entry_id,
            memory_id=entry.memory_id,
            prev_entry_id=entry.prev_entry_id,
            prev_entry_hash=entry.prev_entry_hash,
            op=entry.op,
            payload_hash=entry.payload_hash,
            writer_id=entry.writer_id,
            writer_pubkey=entry.writer_pubkey,
            signature=signature,
            signed_at=entry.signed_at,
        )
        logger.debug(
            "[AUDIT] op=%s memory_id=%s entry_id=%s",
            op,
            memory_id_str,
            entry.entry_id.hex()[:16],
        )
    except Exception:  # noqa: BLE001 - audit must not block writes
        logger.exception(
            "[AUDIT] write_audit_entry failed for op=%s memory=%s",
            op,
            memory_id_str,
        )


def _to_iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)
