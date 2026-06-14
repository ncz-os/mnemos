"""Backend-neutral verifier for the v6.2 memory audit chain.

The verifier intentionally consumes repository rows rather than issuing SQL.
That keeps tamper-evidence independent of Postgres/Oracle/Db2/SQLite storage:
backends persist bytes; this module proves signatures, linear hash links, and
(optionally) that the latest signed payload still matches the live memory row.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .crypto import AuditEntry, canonical_payload_hash, derive_writer_keypair, entry_hash, verify_entry


def _bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    # Oracle/Db2 drivers may hand back RAW values as objects with a bytes()
    # conversion. Let bytes() try before falling back to a TypeError with
    # a useful repr for the verifier issue list.
    return bytes(value)


def _to_iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[attr-defined]
    return str(value)


def _metadata_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = _bytes(value).decode("utf-8")
    if isinstance(value, str):
        if not value.strip():
            return {}
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def audit_entry_from_row(row: Mapping[str, Any]) -> AuditEntry:
    """Rehydrate an ``AuditEntry`` from a repository row."""
    return AuditEntry(
        entry_id=_bytes(row["entry_id"]),
        memory_id=_bytes(row["memory_id"]),
        prev_entry_id=_bytes(row["prev_entry_id"]) if row.get("prev_entry_id") is not None else None,
        prev_entry_hash=_bytes(row["prev_entry_hash"]) if row.get("prev_entry_hash") is not None else None,
        op=str(row["op"]),
        payload_hash=_bytes(row["payload_hash"]),
        writer_id=str(row["writer_id"]),
        writer_pubkey=_bytes(row["writer_pubkey"]),
        signed_at=_to_iso(row["signed_at"]),
    )


def verify_memory_audit_chain(
    rows: list[Mapping[str, Any]],
    *,
    current_memory: Mapping[str, Any] | None = None,
    session_secret: bytes | None = None,
) -> dict[str, Any]:
    """Verify one memory's full audit chain.

    Checks performed:
    * every Ed25519 signature validates against its signed canonical bytes;
    * every row after the first points to the previous row's entry_id;
    * every row after the first stores the previous row's recomputed
      SHA-256(canonical_entry_bytes || signature) hash;
    * if ``current_memory`` is supplied and the head op is not ``delete``,
      the head payload_hash equals the canonical hash of the live memory row;
    * if ``session_secret`` is supplied, each stored writer_pubkey equals the
      deterministic HKDF-derived key for its writer_id, preventing an attacker
      who can rewrite both entry bytes and writer_pubkey from self-signing a
      forged history with an arbitrary key.

    Returns a structured result instead of raising so the HTTP endpoint can
    report all detected tamper indicators in one response.
    """
    issues: list[dict[str, Any]] = []
    entries: list[AuditEntry] = []
    signatures: list[bytes] = []

    previous_entry: AuditEntry | None = None
    previous_signature: bytes | None = None
    for index, row in enumerate(rows):
        try:
            entry = audit_entry_from_row(row)
            signature = _bytes(row["signature"])
        except Exception as exc:  # noqa: BLE001 - verifier reports malformed rows
            issues.append({"index": index, "code": "malformed_row", "detail": str(exc)})
            continue

        entries.append(entry)
        signatures.append(signature)

        if not verify_entry(entry, signature):
            issues.append(
                {
                    "index": index,
                    "entry_id": entry.entry_id.hex(),
                    "code": "invalid_signature",
                }
            )

        if session_secret is not None:
            try:
                _, expected_pubkey = derive_writer_keypair(session_secret, entry.writer_id)
                if entry.writer_pubkey != expected_pubkey:
                    issues.append(
                        {
                            "index": index,
                            "entry_id": entry.entry_id.hex(),
                            "code": "writer_pubkey_mismatch",
                            "writer_id": entry.writer_id,
                            "expected": expected_pubkey.hex(),
                            "actual": entry.writer_pubkey.hex(),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - report key-derivation issues
                issues.append(
                    {
                        "index": index,
                        "entry_id": entry.entry_id.hex(),
                        "code": "writer_pubkey_check_failed",
                        "detail": str(exc),
                    }
                )

        if previous_entry is None:
            if entry.prev_entry_id is not None or entry.prev_entry_hash is not None:
                issues.append(
                    {
                        "index": index,
                        "entry_id": entry.entry_id.hex(),
                        "code": "first_entry_has_previous_pointer",
                    }
                )
        else:
            assert previous_signature is not None
            expected_hash = entry_hash(previous_entry, previous_signature)
            if entry.prev_entry_id != previous_entry.entry_id:
                issues.append(
                    {
                        "index": index,
                        "entry_id": entry.entry_id.hex(),
                        "code": "prev_entry_id_mismatch",
                        "expected": previous_entry.entry_id.hex(),
                        "actual": entry.prev_entry_id.hex() if entry.prev_entry_id else None,
                    }
                )
            if entry.prev_entry_hash != expected_hash:
                issues.append(
                    {
                        "index": index,
                        "entry_id": entry.entry_id.hex(),
                        "code": "prev_entry_hash_mismatch",
                        "expected": expected_hash.hex(),
                        "actual": entry.prev_entry_hash.hex() if entry.prev_entry_hash else None,
                    }
                )

        previous_entry = entry
        previous_signature = signature

    head_payload_match: bool | None = None
    expected_head_payload_hash: str | None = None
    if entries and current_memory is not None and entries[-1].op != "delete":
        expected = canonical_payload_hash(
            memory_id=str(current_memory["id"]),
            content=str(current_memory.get("content") or ""),
            category=str(current_memory.get("category") or ""),
            subcategory=current_memory.get("subcategory"),
            metadata=_metadata_dict(current_memory.get("metadata")),
            embedding=None,
        )
        expected_head_payload_hash = expected.hex()
        head_payload_match = entries[-1].payload_hash == expected
        if not head_payload_match:
            issues.append(
                {
                    "index": len(entries) - 1,
                    "entry_id": entries[-1].entry_id.hex(),
                    "code": "head_payload_hash_mismatch",
                    "expected": expected.hex(),
                    "actual": entries[-1].payload_hash.hex(),
                }
            )

    return {
        "valid": not issues,
        "entry_count": len(entries),
        "head_entry_id": entries[-1].entry_id.hex() if entries else None,
        "head_op": entries[-1].op if entries else None,
        "head_payload_match": head_payload_match,
        "expected_head_payload_hash": expected_head_payload_hash,
        "issues": issues,
    }


__all__ = ["audit_entry_from_row", "verify_memory_audit_chain"]
