"""DB2-native compression-queue repository tests (severance slice 6).

Exercises Db2CompressionQueueRepository end-to-end against a live Db2 12.1.5
EAP: enqueue, concurrent SKIP-LOCKED dequeue (disjoint claims, no double
claim), mark done/failed, and stale sweep. Skipped unless DB2_DSN is set.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("ibm_db", reason="ibm_db driver not installed")

DB2_DSN = os.environ.get("DB2_DSN")
pytestmark = [
    pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live EAP probe skipped"),
    pytest.mark.asyncio,
]

IDS = ["tcq_a", "tcq_b", "tcq_c"]


def _dummy(col, typ):
    c = col.lower()
    if c == "id":
        return None
    if "VECTOR" in typ:
        return "VECTOR('[" + ",".join("0" for _ in range(768)) + "]', 768, FLOAT32)"
    if typ in ("INTEGER", "SMALLINT", "BIGINT", "DECIMAL", "DOUBLE", "REAL"):
        return "0"
    if typ in ("TIMESTAMP", "DATE"):
        return "CURRENT TIMESTAMP"
    return "'x'"


async def test_db2_native_compression_queue_lifecycle():
    from mnemos.persistence import db2 as m

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="native"))
    pool = await m.create_db2_native_pool(DB2_DSN, min_size=2, max_size=4)
    be = m.Db2BackendNative(pool, settings)
    await be.open()
    cq = be._compression_queue_repo
    assert type(cq).__name__ == "Db2CompressionQueueRepository"

    async with be.transactional() as tx:
        conn = m._conn_from_tx(tx)
        cur = await m._call(conn.cursor)
        await m._call(
            cur.execute,
            "select colname, typename from syscat.columns where tabschema=current schema "
            "and tabname='MEMORIES' and nulls='N' and default is null order by colno",
        )
        req = await m._call(cur.fetchall)
        await m._call(cur.close)
        names = [r[0] for r in req]
        for idv in IDS:
            cur = await m._call(conn.cursor)
            await m._call(cur.execute, f"DELETE FROM memory_compression_queue WHERE memory_id='{idv}'")
            await m._call(cur.execute, f"DELETE FROM memories WHERE id='{idv}'")
            vals = ", ".join(
                (f"'{idv}'" if r[0].lower() == "id" else _dummy(r[0], r[1])) for r in req
            )
            await m._call(cur.execute, f"INSERT INTO memories ({', '.join(names)}) VALUES ({vals})")
            await m._call(cur.close)

    try:
        async with be.transactional() as tx:
            enq = await cq.enqueue_compression(
                tx, memory_ids=IDS, reason="manual", priority=5, scoring_profile="balanced"
            )
        assert len(enq) == 3

        async def worker():
            async with be.transactional() as tx:
                rows = await cq.dequeue_compression(tx, limit=2)
                return {r["id"] for r in rows}

        a, b = await asyncio.gather(worker(), worker())
        assert not (a & b), f"double claim: {a & b}"  # SKIP LOCKED -> disjoint
        assert a or b

        async with be.transactional() as tx:
            conn = m._conn_from_tx(tx)
            cur = await m._call(conn.cursor)
            await m._call(
                cur.execute,
                "SELECT id FROM memory_compression_queue WHERE memory_id IN "
                "('tcq_a','tcq_b','tcq_c') ORDER BY memory_id",
            )
            allq = [r[0] for r in await m._call(cur.fetchall)]
            await m._call(cur.close)
        assert len(allq) == 3

        async with be.transactional() as tx:
            await cq.mark_compression_done(tx, queue_id=allq[0])
            await cq.mark_compression_failed(tx, queue_id=allq[1], error="test failure")

        async with be.transactional() as tx:
            conn = m._conn_from_tx(tx)
            cur = await m._call(conn.cursor)
            await m._call(
                cur.execute,
                "UPDATE memory_compression_queue SET status='running', "
                "started_at=CURRENT TIMESTAMP - 9999 SECONDS, attempts=0 WHERE id=?",
                (allq[2],),
            )
            await m._call(cur.close)
        async with be.transactional() as tx:
            swept = await cq.sweep_stale_compression(tx, stale_threshold_secs=60, max_attempts=3)
        assert swept >= 1
    finally:
        async with be.transactional() as tx:
            conn = m._conn_from_tx(tx)
            cur = await m._call(conn.cursor)
            for idv in IDS:
                await m._call(cur.execute, f"DELETE FROM memory_compression_queue WHERE memory_id='{idv}'")
                await m._call(cur.execute, f"DELETE FROM memories WHERE id='{idv}'")
            await m._call(cur.close)
        await be.close()
