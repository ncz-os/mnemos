"""DB2-native audit-chain tests (severance slice 7).

Proves cross-dialect HASH EQUIVALENCE on a live Db2 12.1.5 EAP: an audit entry
hashed before insert recomputes to the identical entry_hash after native insert
+ read + reconstruction (the signed_at ISO string round-trips byte-identically
despite Db2 returning naive datetimes). Also exercises claim_unsealed_window
concurrency (disjoint, no double-claim), stamp/root/list/stats. Skipped unless
DB2_DSN is set.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("ibm_db", reason="ibm_db driver not installed")

DB2_DSN = os.environ.get("DB2_DSN")
pytestmark = [
    pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live EAP probe skipped"),
    pytest.mark.asyncio,
]

from mnemos.persistence import db2 as m
from mnemos.audit.crypto import AuditEntry, entry_hash
from mnemos.audit.route_helper import _to_iso

def mk_entry(suffix, signed_at_iso):
    return AuditEntry(
        entry_id=(b"E" + suffix).ljust(16, b"\x00")[:16],
        memory_id=(b"M" + suffix).ljust(16, b"\x00")[:16],
        prev_entry_id=None,
        prev_entry_hash=None,
        op="create",
        payload_hash=(b"P" + suffix).ljust(32, b"\x00")[:32],
        writer_id="tester",
        writer_pubkey=(b"K" + suffix).ljust(32, b"\x00")[:32],
        signed_at=signed_at_iso,
    )


async def test_db2_native_audit_chain_hash_equivalence():
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="native"))
    pool = await m.create_db2_native_pool(DB2_DSN, min_size=2, max_size=4)
    be = m.Db2BackendNative(pool, settings)
    await be.open()
    ac = be._audit_chain_repo
    print("repo:", type(ac).__name__)

    # clean
    async with be.transactional() as tx:
        conn = m._conn_from_tx(tx); cur = await m._call(conn.cursor)
        await m._call(cur.execute, "DELETE FROM memory_audit_chain WHERE writer_id='tester'")
        await m._call(cur.execute, "DELETE FROM memory_audit_roots WHERE entry_count=4242")
        await m._call(cur.close)

    # ---- HASH-EQUIVALENCE: signed_at with µs (offset) must round-trip ----
    iso = datetime.now(tz=timezone.utc).isoformat()
    e = mk_entry(b"1", iso)
    sig = (b"S1").ljust(64, b"\x00")[:64]
    h_before = entry_hash(e, sig)
    async with be.transactional() as tx:
        await ac.insert_audit_entry(
            tx, entry_id=e.entry_id, memory_id=e.memory_id, prev_entry_id=None,
            prev_entry_hash=None, op=e.op, payload_hash=e.payload_hash,
            writer_id=e.writer_id, writer_pubkey=e.writer_pubkey, signature=sig, signed_at=iso)
    async with be.transactional() as tx:
        row = await ac.get_audit_entry_by_id(tx, e.entry_id)
    assert row is not None, "entry not found after insert"
    # binary round-trip
    assert bytes(row["entry_id"]) == e.entry_id, "entry_id bytes mismatch"
    assert bytes(row["payload_hash"]) == e.payload_hash, "payload_hash bytes mismatch"
    assert bytes(row["signature"]) == sig, "signature bytes mismatch"
    # reconstruct via the REAL shared reconstruction + recompute hash
    e2 = mk_entry(b"1", _to_iso(row["signed_at"]))
    h_after = entry_hash(e2, bytes(row["signature"]))
    print(f"signed_at orig={iso!r}")
    print(f"signed_at recon={_to_iso(row['signed_at'])!r}")
    print(f"hash_before={h_before.hex()[:16]} hash_after={h_after.hex()[:16]} EQUAL={h_before==h_after}")
    assert h_before == h_after, "CROSS-DIALECT HASH MISMATCH (signed_at round-trip broke verification)"

    # get_latest
    async with be.transactional() as tx:
        latest = await ac.get_latest_audit_entry(tx, e.memory_id)
    assert latest is not None and bytes(latest["entry_id"]) == e.entry_id
    print("get_latest OK")

    # seed a few unsealed entries for claim/stamp/list
    isos = []
    async with be.transactional() as tx:
        for i in range(2, 5):
            ii = datetime.now(tz=timezone.utc).isoformat()
            isos.append(ii)
            ent = mk_entry(str(i).encode(), ii)
            await ac.insert_audit_entry(
                tx, entry_id=ent.entry_id, memory_id=ent.memory_id, prev_entry_id=None,
                prev_entry_hash=None, op=ent.op, payload_hash=ent.payload_hash,
                writer_id=ent.writer_id, writer_pubkey=ent.writer_pubkey,
                signature=(b"S" + str(i).encode()).ljust(64, b"\x00")[:64], signed_at=ii)

    # claim_unsealed_window concurrency: two workers, disjoint, no double-claim
    async def claimer():
        async with be.transactional() as tx:
            rows = await ac.claim_unsealed_window(tx, max_window_seconds=0, limit=2)
            return {bytes(r["entry_id"]) for r in rows}
    ca, cb = await asyncio.gather(claimer(), claimer())
    assert not (ca & cb), f"DOUBLE CLAIM: {ca & cb}"
    print(f"claim concurrency OK (A={len(ca)} B={len(cb)} disjoint)")

    # stamp a window + insert root + list
    groot = b"ROOT".ljust(32, b"\x00")
    async with be.transactional() as tx:
        all_ids = list(ca | cb)
        if all_ids:
            await ac.stamp_window_with_root(tx, entry_ids=all_ids, global_root=groot, starting_seq=1)
            await ac.insert_audit_root(
                tx, global_root=groot, window_start=isos[0], window_end=isos[-1],
                entry_count=4242, root_signature=b"RS".ljust(64, b"\x00"),
                signer_pubkey=b"SP".ljust(32, b"\x00"), sealed_at=datetime.now(tz=timezone.utc).isoformat())
    async with be.transactional() as tx:
        win = await ac.list_window_entries(tx, groot)
    print(f"list_window_entries: {len(win)} (expect {len(all_ids)})")
    assert len(win) == len(all_ids)

    # stats
    async with be.transactional() as tx:
        stats = await ac.get_chain_stats(tx)
    print("chain_stats:", {k: stats[k] for k in ("total_entries", "unsealed_count", "sealed_root_count")})
    assert stats["total_entries"] >= 4 and stats["sealed_root_count"] >= 1

    # cleanup
    async with be.transactional() as tx:
        conn = m._conn_from_tx(tx); cur = await m._call(conn.cursor)
        await m._call(cur.execute, "DELETE FROM memory_audit_chain WHERE writer_id='tester'")
        await m._call(cur.execute, "DELETE FROM memory_audit_roots WHERE entry_count=4242")
        await m._call(cur.close)
    await be.close()
    print("SLICE7_VALIDATE_OK")



async def test_db2_native_audit_chain_tamper_detection():
    """GRAEAE-C tamper-detection: a mutation to a stored audit field must change
    the recomputed entry_hash (so the chain link no longer verifies). Proves the
    Db2-native round-trip preserves tamper-evidence, not just equivalence."""
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="native"))
    pool = await m.create_db2_native_pool(DB2_DSN, min_size=1, max_size=2)
    be = m.Db2BackendNative(pool, settings)
    await be.open()
    ac = be._audit_chain_repo
    iso = datetime.now(tz=timezone.utc).isoformat()
    e = mk_entry(b"T", iso)
    sig = b"ST".ljust(64, b"\x00")[:64]
    h_clean = entry_hash(e, sig)
    async with be.transactional() as tx:
        conn = m._conn_from_tx(tx); cur = await m._call(conn.cursor)
        await m._call(cur.execute, "DELETE FROM memory_audit_chain WHERE writer_id='tester'")
        await m._call(cur.close)
    try:
        async with be.transactional() as tx:
            await ac.insert_audit_entry(
                tx, entry_id=e.entry_id, memory_id=e.memory_id, prev_entry_id=None,
                prev_entry_hash=None, op=e.op, payload_hash=e.payload_hash,
                writer_id=e.writer_id, writer_pubkey=e.writer_pubkey, signature=sig, signed_at=iso)
        # untampered read -> hash matches
        async with be.transactional() as tx:
            row = await ac.get_audit_entry_by_id(tx, e.entry_id)
        e_clean = mk_entry(b"T", _to_iso(row["signed_at"]))
        assert entry_hash(e_clean, bytes(row["signature"])) == h_clean
        # TAMPER: mutate the stored payload_hash directly
        async with be.transactional() as tx:
            conn = m._conn_from_tx(tx); cur = await m._call(conn.cursor)
            await m._call(cur.execute,
                "UPDATE memory_audit_chain SET payload_hash = ? WHERE entry_id = ?",
                (b"X".ljust(32, b"\x00"), e.entry_id))
            await m._call(cur.close)
        async with be.transactional() as tx:
            trow = await ac.get_audit_entry_by_id(tx, e.entry_id)
        e_tampered = AuditEntry(
            entry_id=bytes(trow["entry_id"]), memory_id=bytes(trow["memory_id"]),
            prev_entry_id=None, prev_entry_hash=None, op=trow["op"],
            payload_hash=bytes(trow["payload_hash"]), writer_id=trow["writer_id"],
            writer_pubkey=bytes(trow["writer_pubkey"]), signed_at=_to_iso(trow["signed_at"]))
        h_tampered = entry_hash(e_tampered, bytes(trow["signature"]))
        assert h_tampered != h_clean, "tamper NOT detected — recomputed hash unchanged after mutation"
    finally:
        async with be.transactional() as tx:
            conn = m._conn_from_tx(tx); cur = await m._call(conn.cursor)
            await m._call(cur.execute, "DELETE FROM memory_audit_chain WHERE writer_id='tester'")
            await m._call(cur.close)
        await be.close()
