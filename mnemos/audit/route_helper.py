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
from datetime import timezone
from typing import Any, Literal

from .crypto import AuditEntry, canonical_payload_hash, verify_entry
from .writer import build_entry, latest_hash
from mnemos.persistence.base import AuditPersistence

logger = logging.getLogger(__name__)

AuditOp = Literal["create", "update", "delete", "archive", "replicate"]


class AuditChainContinuityError(ValueError):
    """Raised when a caller requires a specific prior chain head."""


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
    expected_prev_entry_id_hex: str | None = None,
    expected_prev_entry_hash_hex: str | None = None,
    enforce_continuity: bool = False,
) -> None:
    """Build + insert one audit entry inside the caller's tx.

    Fetches the prior entry for this memory (via SHA-256 16-byte key)
    to populate ``prev_entry_id`` + ``prev_entry_hash``. Computes
    payload_hash, signs the new entry with the writer's HKDF-derived
    Ed25519 key, then INSERTs.

    Errors are LOGGED but not re-raised by default — the audit chain
    is a consistency-guarantee on top of the write, not a write
    prerequisite. Callers that pass ``enforce_continuity=True`` opt
    into hard failure for continuity/insert errors (federation uses
    this for replica-chain audit writes).
    """
    if backend.audit_chain is None:
        return  # backend hasn't shipped audit_chain; silently no-op

    try:
        memory_id_bytes = memory_id_to_audit_bytes(memory_id_str)
        prev_row = await backend.audit_chain.get_latest_audit_entry(tx, memory_id_bytes)
        prev_entry_id, prev_entry_hash = _audit_prev_head(prev_row)
        override_prev = _decode_expected_prev_head(
            expected_prev_entry_id_hex=expected_prev_entry_id_hex,
            expected_prev_entry_hash_hex=expected_prev_entry_hash_hex,
        )
        if override_prev is not None:
            # Federation continuity is a claim about the predecessor this
            # replica is extending. Do not install a nonzero peer-supplied head
            # unless it exactly matches the local chain head for this memory.
            if prev_entry_id is None or prev_entry_hash is None:
                raise AuditChainContinuityError(
                    "expected prev head supplied but local audit chain has no predecessor"
                )
            if override_prev != (prev_entry_id, prev_entry_hash):
                raise AuditChainContinuityError(
                    "expected prev head does not match local audit chain head"
                )

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
    except Exception:  # noqa: BLE001 - audit must not block writes unless requested
        logger.exception(
            "[AUDIT] write_audit_entry failed for op=%s memory=%s",
            op,
            memory_id_str,
        )
        if enforce_continuity:
            raise


def _audit_prev_head(prev_row: Any | None) -> tuple[bytes | None, bytes | None]:
    if prev_row is None:
        return None, None
    signature = prev_row["signature"]
    for signed_at in _signed_at_candidates(prev_row["signed_at"]):
        prev_ae = _audit_entry_from_row(prev_row, signed_at=signed_at)
        if verify_entry(prev_ae, signature):
            return prev_row["entry_id"], latest_hash(prev_ae, signature)
    raise AuditChainContinuityError("local audit chain latest entry signature is invalid")


def _audit_entry_from_row(prev_row: Any, *, signed_at: str) -> AuditEntry:
    return AuditEntry(
        entry_id=prev_row["entry_id"],
        memory_id=prev_row["memory_id"],
        prev_entry_id=prev_row.get("prev_entry_id"),
        prev_entry_hash=prev_row.get("prev_entry_hash"),
        op=prev_row["op"],
        payload_hash=prev_row["payload_hash"],
        writer_id=prev_row["writer_id"],
        writer_pubkey=prev_row["writer_pubkey"],
        signed_at=signed_at,
    )


def _signed_at_candidates(value: Any) -> tuple[str, ...]:
    candidates: list[str] = []

    def add(candidate: str | None) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    add(_to_iso(value))
    if hasattr(value, "isoformat"):
        try:
            if getattr(value, "tzinfo", None) is None:
                add(value.replace(tzinfo=timezone.utc).isoformat())
            else:
                add(value.astimezone(timezone.utc).isoformat())
        except Exception:
            pass
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            add(text[:-1] + "+00:00")
        if " " in text:
            add(text.replace(" ", "T"))
        if "+" not in text and not text.endswith("Z"):
            add(text + "+00:00")
    return tuple(candidates)


def _decode_expected_hex(value: str | None, *, label: str, length: int) -> bytes | None:
    if value in (None, ""):
        return None
    try:
        out = bytes.fromhex(value)
    except ValueError as exc:
        raise AuditChainContinuityError(f"{label} is not valid hex") from exc
    if len(out) != length:
        raise AuditChainContinuityError(f"{label} must decode to {length} bytes")
    if not any(out):
        raise AuditChainContinuityError(f"{label} must not be all-zero bytes")
    return out


def _decode_expected_prev_head(
    *,
    expected_prev_entry_id_hex: str | None,
    expected_prev_entry_hash_hex: str | None,
) -> tuple[bytes, bytes] | None:
    expected_entry_id = _decode_expected_hex(
        expected_prev_entry_id_hex,
        label="expected_prev_entry_id_hex",
        length=16,
    )
    expected_entry_hash = _decode_expected_hex(
        expected_prev_entry_hash_hex,
        label="expected_prev_entry_hash_hex",
        length=32,
    )
    if expected_entry_id is None and expected_entry_hash is None:
        return None
    if expected_entry_id is None or expected_entry_hash is None:
        raise AuditChainContinuityError(
            "expected prev entry id/hash must both be supplied or both be empty"
        )
    return expected_entry_id, expected_entry_hash


def _to_iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)
