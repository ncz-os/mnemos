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
import json
import math
import struct
from datetime import timezone
from typing import Any, Literal

from .crypto import AuditEntry, canonical_payload_hash, verify_entry
from .writer import build_entry, latest_hash
from mnemos.persistence.base import AuditPersistence

logger = logging.getLogger(__name__)

AuditOp = Literal["create", "update", "delete", "archive", "replicate"]


class AuditChainContinuityError(ValueError):
    """Raised when a caller requires a specific prior chain head."""


def normalize_embedding(value: Any) -> bytes | None:
    """Canonical little-endian float32 encoding across driver representations."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        value = json.loads(value)
    numbers = [float(v) for v in value]
    if not all(math.isfinite(v) for v in numbers):
        raise ValueError("audit embedding contains non-finite values")
    return struct.pack(f"<{len(numbers)}f", *numbers)


async def fetch_audit_snapshot(tx, memory_id_str):
    """Read and lock the fields signed by an imminent destructive mutation."""
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    ops = _Ops(tx, transaction_dialect(tx))
    return await ops.fetchone(
        "SELECT content, category, subcategory, metadata, embedding FROM memories WHERE id = ?"
        + ("" if ops.dialect == "sqlite" else " FOR UPDATE"),
        memory_id_str,
    )


async def write_transaction_audit(tx, *, op, memory_id_str, snapshot, writer_id):
    """Audit repository/lifecycle mutations without consulting global app state."""
    from mnemos.core.config import audit_chain_enabled_flag as audit_chain_enabled

    if not audit_chain_enabled():
        return
    from types import SimpleNamespace
    from mnemos.persistence.worker_lifecycle import transaction_dialect

    dialect = transaction_dialect(tx)
    if dialect == "sqlite":
        from mnemos.persistence.sqlite import SqliteAuditChainRepository

        repo = SqliteAuditChainRepository()
    elif dialect == "postgres":  # pragma: no cover - postgres dialect dispatch; this CI gate runs against SQLite only. The PostgresAuditChainRepository itself is exercised in tests/test_db2_dialect_parity.py / test_federation_journal.py against live Postgres in the test:integration job, not here.
        from mnemos.persistence.postgres import PostgresAuditChainRepository

        repo = PostgresAuditChainRepository()
    elif dialect == "oracle":  # pragma: no cover - oracle dialect dispatch; covered by test:oracle-smoke against a live Oracle (not this SQLite-only audit-coverage gate).
        from mnemos.persistence.oracle import OracleAuditChainRepository

        repo = OracleAuditChainRepository()
    elif dialect == "db2":  # pragma: no cover - db2 dialect dispatch; covered by tests/test_db2_*.py against live Db2 in test:integration (Db2 has no arm64 Linux wheel and is excluded from this gate by design).
        from mnemos.persistence.db2 import Db2AuditChainRepository

        repo = Db2AuditChainRepository()
    else:  # pragma: no cover - defensive default for unrecognized dialects; not reachable from any production backend (sqlite/postgres/oracle/db2/mysql/mariadb all map to a branch above). Kept so a future backend cannot silently fall through to repo=None.
        repo = None
    await write_configured_audit_entry(
        SimpleNamespace(audit_chain=repo),
        tx,
        op=op,
        memory_id_str=memory_id_str,
        snapshot=snapshot,
        writer_id=writer_id,
    )


async def write_configured_audit_entry(backend, tx, *, op, memory_id_str, snapshot, writer_id):
    """Apply deployment policy to a real mutation snapshot in the caller's tx.

    `on` retains best-effort compatibility. `required` propagates failures so
    the data write and any durable federation cursor roll back together.
    """
    from mnemos.core.config import audit_chain_required_flag, get_settings
    from mnemos.core.config import audit_chain_enabled_flag as audit_chain_enabled

    if not audit_chain_enabled():
        return
    required = audit_chain_required_flag()
    try:
        secret = (getattr(get_settings().server, "session_secret", "") or "").encode("utf-8")
        if not secret:
            raise AuditChainContinuityError("audit signing requires a session secret")
        if snapshot is None:
            raise AuditChainContinuityError("audit mutation snapshot is missing")
        values = dict(snapshot)
        from mnemos.persistence.worker_lifecycle import _await

        for name in ("content", "metadata", "embedding"):
            value = values.get(name)
            if hasattr(value, "read"):
                values[name] = await _await(value.read())
        metadata = values.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        await write_audit_entry(
            backend,
            tx,
            op=op,
            memory_id_str=memory_id_str,
            content=values.get("content") or "",
            category=values.get("category") or "",
            subcategory=values.get("subcategory"),
            metadata=metadata,
            embedding=values.get("embedding"),
            writer_id=writer_id,
            session_secret=secret,
            required=required,
        )
    except Exception as exc:
        if required:
            if isinstance(exc, AuditChainContinuityError):
                raise
            raise AuditChainContinuityError("required mutation audit failed") from exc
        logger.exception("[AUDIT] best-effort mutation audit failed op=%s", op)


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
    required: bool = False,
) -> None:
    """Build + insert one audit entry inside the caller's tx.

    Fetches the prior entry for this memory (via SHA-256 16-byte key)
    to populate ``prev_entry_id`` + ``prev_entry_hash``. Computes
    payload_hash, signs the new entry with the writer's HKDF-derived
    Ed25519 key, then INSERTs.

    Default behavior is BEST-EFFORT: errors are LOGGED but not
    re-raised. The audit chain is a consistency-guarantee on top of the
    write, not a write prerequisite; callers running inside their own
    ``async with backend.transactional()`` get the rollback safety from
    the outer transaction already.

    Callers that pass ``enforce_continuity=True`` opt into hard failure
    for continuity/insert errors (federation uses this for replica-chain
    audit writes).

    F16: callers that document audit coverage as REQUIRED (e.g.,
    archive + delete paths where the chain MUST capture every mutation)
    pass ``required=True``. With ``required=True``, a failed
    ``insert_audit_entry`` propagates out of the call so the surrounding
    transaction rolls back -- preventing the "memory row committed,
    audit row missed" silent drift documented at
    ``docs/AUDIT_CHAIN.md`` failure-mode table.
    """
    from mnemos.core.config import audit_chain_required_flag

    required = required or audit_chain_required_flag()
    if required and not session_secret:
        raise AuditChainContinuityError("required audit signing requires a session secret")
    if backend.audit_chain is None:
        if required:
            raise AuditChainContinuityError(
                f"required audit write for op={op} memory={memory_id_str!r} "
                "but backend has no audit_chain repo (MySQL/MariaDB "
                "deployment, or audit_chain not migrated yet)"
            )
        return  # backend hasn't shipped audit_chain; silently no-op

    savepoint_ops = None
    if not required and not enforce_continuity:
        from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

        try:
            dialect = transaction_dialect(tx)
        except TypeError:
            dialect = None
        if dialect is not None:
            savepoint_ops = _Ops(tx, dialect)
            await savepoint_ops.execute(
                "SAVEPOINT mnemos_audit_append" + (" ON ROLLBACK RETAIN CURSORS" if dialect == "db2" else "")
            )
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
                raise AuditChainContinuityError("expected prev head supplied but local audit chain has no predecessor")
            if override_prev != (prev_entry_id, prev_entry_hash):
                raise AuditChainContinuityError("expected prev head does not match local audit chain head")

        payload_hash = canonical_payload_hash(
            memory_id=memory_id_str,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata=metadata,
            embedding=normalize_embedding(embedding),
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
    except Exception as exc:  # noqa: BLE001 - audit must not block writes unless requested
        if savepoint_ops is not None:
            await savepoint_ops.execute(
                "ROLLBACK TO SAVEPOINT mnemos_audit_append"
                if savepoint_ops.dialect != "oracle"
                else "ROLLBACK TO mnemos_audit_append"
            )
        logger.exception(
            "[AUDIT] write_audit_entry failed for op=%s memory=%s required=%s",
            op,
            memory_id_str,
            required,
        )
        if enforce_continuity:
            raise
        if required:
            # F16: REQUIRED audit writes must propagate so the outer
            # transaction rolls back. Re-raise with the original
            # exception chained; the caller (route handler / repo)
            # typically sits inside ``async with backend.transactional()
            # as tx`` and the rollback handles the rest.
            raise AuditChainContinuityError(
                f"required audit write failed for op={op} memory={memory_id_str!r}"
            ) from exc
    finally:
        if savepoint_ops is not None and savepoint_ops.dialect != "oracle":
            await savepoint_ops.execute("RELEASE SAVEPOINT mnemos_audit_append")


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
        except Exception:  # pragma: no cover - defensive: an exotic tz-aware datetime whose astimezone(UTC) raises. No production datetime hits this; the catch exists so the candidate list still gets a usable format even on a bad datetime.
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
        raise AuditChainContinuityError("expected prev entry id/hash must both be supplied or both be empty")
    return expected_entry_id, expected_entry_hash


def _to_iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)
