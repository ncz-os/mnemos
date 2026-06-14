"""Tests for backend-neutral v6.2 memory audit-chain verification."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from mnemos.audit import build_entry, canonical_payload_hash
from mnemos.audit.verify import audit_entry_from_row, verify_memory_audit_chain


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    class _S:
        class database:
            embedding_dim = 1024

    backend = SqliteBackend(tmp_path / "audit-verify.db", _S())
    await backend.open()
    yield backend
    await backend.close()


def _row(entry, signature):
    return {
        "entry_id": entry.entry_id,
        "memory_id": entry.memory_id,
        "prev_entry_id": entry.prev_entry_id,
        "prev_entry_hash": entry.prev_entry_hash,
        "op": entry.op,
        "payload_hash": entry.payload_hash,
        "writer_id": entry.writer_id,
        "writer_pubkey": entry.writer_pubkey,
        "signature": signature,
        "signed_at": entry.signed_at,
        "global_root": None,
        "global_seq": None,
    }


def _chain(memory_id_str: str = "mem_1_abcdef"):
    from mnemos.audit.route_helper import memory_id_to_audit_bytes
    from mnemos.audit.writer import latest_hash

    memory_id = memory_id_to_audit_bytes(memory_id_str)
    ph1 = canonical_payload_hash(
        memory_id=memory_id_str,
        content="first",
        category="facts",
        subcategory=None,
        metadata={"a": 1},
        embedding=None,
    )
    e1, s1 = build_entry(
        op="create",
        memory_id=memory_id,
        prev_entry_id=None,
        prev_entry_hash=None,
        payload_hash=ph1,
        writer_id="alice",
        session_secret=b"x" * 32,
        signed_at=datetime(2026, 6, 14, 1, 0, tzinfo=timezone.utc),
    )
    ph2 = canonical_payload_hash(
        memory_id=memory_id_str,
        content="second",
        category="facts",
        subcategory="s",
        metadata={"b": 2},
        embedding=None,
    )
    e2, s2 = build_entry(
        op="update",
        memory_id=memory_id,
        prev_entry_id=e1.entry_id,
        prev_entry_hash=latest_hash(e1, s1),
        payload_hash=ph2,
        writer_id="alice",
        session_secret=b"x" * 32,
        signed_at=datetime(2026, 6, 14, 1, 1, tzinfo=timezone.utc),
    )
    current = {
        "id": memory_id_str,
        "content": "second",
        "category": "facts",
        "subcategory": "s",
        "metadata": {"b": 2},
    }
    return [_row(e1, s1), _row(e2, s2)], current


def test_verify_memory_audit_chain_valid() -> None:
    rows, current = _chain()

    out = verify_memory_audit_chain(rows, current_memory=current)

    assert out["valid"] is True
    assert out["entry_count"] == 2
    assert out["head_op"] == "update"
    assert out["head_payload_match"] is True
    assert out["issues"] == []


def test_verify_detects_signature_tamper() -> None:
    rows, current = _chain()
    tampered_entry = dataclasses.replace(audit_entry_from_row(rows[1]), op="delete")
    rows[1] = {**rows[1], "op": tampered_entry.op}

    out = verify_memory_audit_chain(rows, current_memory=current)

    assert out["valid"] is False
    assert any(i["code"] == "invalid_signature" for i in out["issues"])


def test_verify_detects_broken_hash_link() -> None:
    rows, current = _chain()
    rows[1] = {**rows[1], "prev_entry_hash": b"\x99" * 32}

    out = verify_memory_audit_chain(rows, current_memory=current)

    assert out["valid"] is False
    assert any(i["code"] == "prev_entry_hash_mismatch" for i in out["issues"])


def test_verify_detects_silent_memory_payload_modification() -> None:
    rows, current = _chain()
    current = {**current, "content": "poisoned"}

    out = verify_memory_audit_chain(rows, current_memory=current)

    assert out["valid"] is False
    assert out["head_payload_match"] is False
    assert any(i["code"] == "head_payload_hash_mismatch" for i in out["issues"])


def test_verify_detects_writer_pubkey_substitution() -> None:
    rows, current = _chain()
    rows[0] = {**rows[0], "writer_pubkey": b"\x01" * 32}

    out = verify_memory_audit_chain(rows, current_memory=current, session_secret=b"x" * 32)

    assert out["valid"] is False
    assert any(i["code"] in {"invalid_signature", "writer_pubkey_mismatch"} for i in out["issues"])


@pytest.mark.asyncio
async def test_sqlite_list_memory_entries_ordered(sqlite_backend):
    from mnemos.audit.route_helper import memory_id_to_audit_bytes
    from mnemos.audit.writer import latest_hash

    memory_id_str = "mem_sqlite_verify"
    memory_id = memory_id_to_audit_bytes(memory_id_str)
    ph1 = canonical_payload_hash(
        memory_id=memory_id_str,
        content="a",
        category="facts",
        subcategory=None,
        metadata=None,
        embedding=None,
    )
    e1, s1 = build_entry(
        op="create",
        memory_id=memory_id,
        prev_entry_id=None,
        prev_entry_hash=None,
        payload_hash=ph1,
        writer_id="alice",
        session_secret=b"x" * 32,
        signed_at=datetime.now(tz=timezone.utc),
    )
    ph2 = canonical_payload_hash(
        memory_id=memory_id_str,
        content="b",
        category="facts",
        subcategory=None,
        metadata=None,
        embedding=None,
    )
    e2, s2 = build_entry(
        op="update",
        memory_id=memory_id,
        prev_entry_id=e1.entry_id,
        prev_entry_hash=latest_hash(e1, s1),
        payload_hash=ph2,
        writer_id="alice",
        session_secret=b"x" * 32,
        signed_at=datetime.now(tz=timezone.utc) + timedelta(seconds=1),
    )

    async with sqlite_backend.transactional() as tx:
        for entry, sig in [(e1, s1), (e2, s2)]:
            await sqlite_backend.audit_chain.insert_audit_entry(
                tx,
                entry_id=entry.entry_id,
                memory_id=entry.memory_id,
                prev_entry_id=entry.prev_entry_id,
                prev_entry_hash=entry.prev_entry_hash,
                op=entry.op,
                payload_hash=entry.payload_hash,
                writer_id=entry.writer_id,
                writer_pubkey=entry.writer_pubkey,
                signature=sig,
                signed_at=entry.signed_at,
            )
        rows = await sqlite_backend.audit_chain.list_memory_entries(tx, memory_id)

    assert [r["entry_id"] for r in rows] == [e1.entry_id, e2.entry_id]
    assert verify_memory_audit_chain(rows, session_secret=b"x" * 32)["valid"] is True
