"""In-memory audit chain entry builder.

This module is DB-free. Given the previous entry (or None for first
write) plus the current memory state, it produces a fully-signed
``AuditEntry`` ready for backend insertion. Persistence-layer hooks
that fetch the latest entry and insert the new one are added in a
follow-up commit; this slice keeps the builder testable in isolation.

Design ref: docs/v6.2-nexus-pattern-adoption.md § 1.

Usage (from a memory route handler, in pseudo-code)::

    from mnemos.audit import build_entry, entry_hash

    prev = await backend.memories.get_latest_audit_entry(tx, memory_id)
    entry, signature = build_entry(
        op="create",
        memory_id=memory_id_bytes,
        prev_entry_id=prev["entry_id"] if prev else None,
        prev_entry_hash=prev["latest_hash"] if prev else None,
        payload_hash=canonical_payload_hash(...),
        writer_id=user.user_id,
        session_secret=settings.server.session_secret.encode(),
    )
    await backend.memories.insert_audit_entry(tx, entry, signature)
    # next entry's prev_entry_hash will be entry_hash(entry, signature)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from .crypto import (
    AuditEntry,
    derive_writer_keypair,
    entry_hash,
    sign_entry,
)

AuditOp = Literal["create", "update", "delete", "archive", "replicate"]


def _uuidv7_bytes() -> bytes:
    """UUIDv7 16-byte representation.

    Python stdlib has uuid.uuid7() in 3.14+. Fall back to UUIDv4 on
    older runtimes; the chain hash + sealer ordering use signed_at as
    the primary timeline anchor so version doesn't materially affect
    correctness (only forensics).
    """
    try:
        # Python 3.14+ provides uuid7 directly.
        return uuid.uuid7().bytes  # type: ignore[attr-defined]
    except AttributeError:
        return uuid.uuid4().bytes


def build_entry(
    *,
    op: AuditOp,
    memory_id: bytes,
    prev_entry_id: bytes | None,
    prev_entry_hash: bytes | None,
    payload_hash: bytes,
    writer_id: str,
    session_secret: bytes,
    signed_at: datetime | None = None,
) -> tuple[AuditEntry, bytes]:
    """Build + sign a new audit entry.

    Returns ``(entry, signature)`` — both required for the row insert
    and for computing the next entry's ``prev_entry_hash``.

    Validates byte lengths and chain consistency:
    - ``memory_id`` must be 16 bytes
    - ``payload_hash`` must be 32 bytes
    - ``prev_entry_id`` (when set) must be 16 bytes
    - ``prev_entry_hash`` (when set) must be 32 bytes
    - ``prev_entry_id`` and ``prev_entry_hash`` must both be set, or
      both be None (no half-state)
    """
    if len(memory_id) != 16:
        raise ValueError(f"memory_id must be 16 bytes (got {len(memory_id)})")
    if len(payload_hash) != 32:
        raise ValueError(f"payload_hash must be 32 bytes (got {len(payload_hash)})")
    if (prev_entry_id is None) != (prev_entry_hash is None):
        raise ValueError("prev_entry_id and prev_entry_hash must both be set or both be None")
    if prev_entry_id is not None and len(prev_entry_id) != 16:
        raise ValueError(f"prev_entry_id must be 16 bytes (got {len(prev_entry_id)})")
    if prev_entry_hash is not None and len(prev_entry_hash) != 32:
        raise ValueError(f"prev_entry_hash must be 32 bytes (got {len(prev_entry_hash)})")

    private_key, pubkey = derive_writer_keypair(session_secret, writer_id)
    ts = (signed_at or datetime.now(tz=timezone.utc)).isoformat()
    entry = AuditEntry(
        entry_id=_uuidv7_bytes(),
        memory_id=memory_id,
        prev_entry_id=prev_entry_id,
        prev_entry_hash=prev_entry_hash,
        op=op,
        payload_hash=payload_hash,
        writer_id=writer_id,
        writer_pubkey=pubkey,
        signed_at=ts,
    )
    signature = sign_entry(entry, private_key)
    return entry, signature


def latest_hash(entry: AuditEntry, signature: bytes) -> bytes:
    """Convenience re-export: SHA-256(canonical_bytes || signature).

    Persisted denormalized so the next builder call doesn't have to
    recompute it from the prior row's canonical_bytes (saves a JCS pass
    on the hot path). Backend ``insert_audit_entry`` stores this on
    the row alongside the signature.
    """
    return entry_hash(entry, signature)
