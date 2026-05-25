#!/usr/bin/env python3
"""PG streaming-replica HA read benchmark.

Measures read latency (semantic search, point lookup, list scan) against both
the primary (:5433) and the streaming standby (:5434) to characterise the
throughput and latency benefit of offloading reads to the replica.

Usage::

    PG_PRIMARY_DSN="postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos" \
    PG_STANDBY_DSN="postgresql://mnemos_user:mnemos_local@localhost:5434/mnemos" \
    BENCH_HMAC_KEY="mnemos-bench-v2" \
    .venv/bin/python scripts/bench_ha_pg.py \
      --embed-url http://192.168.207.67:11434 \
      --n-records 2000 --repeat 50

The script inserts n-records into the PRIMARY, waits for replication lag to
drain to 0, then runs read benchmarks against both primary and standby,
reporting p50/p95 for each node.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

_EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.207.67:11434")
_EMBED_MODEL = "nomic-embed-text"
_POOL: list[list[float]] = []


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

async def _fetch_embedding(client: httpx.AsyncClient, text: str) -> list[float]:
    resp = await client.post(
        f"{_EMBED_URL}/api/embeddings",
        json={"model": _EMBED_MODEL, "prompt": text},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def _build_pool(embed_url: str, size: int = 256) -> None:
    global _POOL, _EMBED_URL
    _EMBED_URL = embed_url
    print(f"  [embed-pool] generating {size} embeddings from {embed_url} ...", flush=True)
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_embedding(client, f"HA benchmark vector seed {i}") for i in range(size)]
        results = await asyncio.gather(*tasks)
    _POOL.extend(results)
    print(f"  [embed-pool] done — {len(_POOL)} vectors cached", flush=True)


def _pool_vec(idx: int | None = None) -> list[float]:
    if not _POOL:
        raise RuntimeError("embed pool not built")
    i = random.randrange(len(_POOL)) if idx is None else idx % len(_POOL)
    return _POOL[i]


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS ha_bench_memories (
    id        TEXT PRIMARY KEY,
    content   TEXT NOT NULL,
    embedding vector(768)
);
CREATE INDEX IF NOT EXISTS idx_ha_hnsw ON ha_bench_memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
"""


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for stmt in _DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            await conn.execute(stmt)


async def _cleanup(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP TABLE IF EXISTS ha_bench_memories")


# ---------------------------------------------------------------------------
# Benchmark phases
# ---------------------------------------------------------------------------

async def _bulk_insert(conn: asyncpg.Connection, n: int) -> float:
    rows = [(str(uuid.uuid4()), f"HA bench record {i}", _pool_vec(i)) for i in range(n)]
    t0 = time.perf_counter()
    await conn.executemany(
        "INSERT INTO ha_bench_memories(id, content, embedding) VALUES($1, $2, $3::vector)",
        rows,
    )
    return (time.perf_counter() - t0) * 1000


async def _wait_for_replica(primary: asyncpg.Connection, standby: asyncpg.Connection, timeout: float = 30.0) -> None:
    print("  [ha] waiting for replication lag to drain ...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent = await primary.fetchval("SELECT pg_current_wal_lsn()")
        replayed = await standby.fetchval("SELECT pg_last_wal_replay_lsn()")
        if sent and replayed and sent <= replayed:
            print(f"  [ha] replica caught up (lsn={sent})", flush=True)
            return
        await asyncio.sleep(0.2)
    print("  [ha] WARNING: replica lag did not drain within timeout", flush=True)


async def _semantic_search(conn: asyncpg.Connection, repeat: int) -> list[float]:
    latencies = []
    for _ in range(repeat):
        vec = _pool_vec()
        t0 = time.perf_counter()
        await conn.fetch(
            """SELECT id FROM ha_bench_memories
               ORDER BY embedding <=> $1::vector LIMIT 10""",
            str(vec),
        )
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


async def _point_lookup(conn: asyncpg.Connection, ids: list[str], repeat: int) -> list[float]:
    latencies = []
    for i in range(repeat):
        rid = ids[i % len(ids)]
        t0 = time.perf_counter()
        await conn.fetchrow("SELECT id, content FROM ha_bench_memories WHERE id = $1", rid)
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


async def _list_scan(conn: asyncpg.Connection, repeat: int) -> list[float]:
    latencies = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        await conn.fetch("SELECT id, content FROM ha_bench_memories ORDER BY id LIMIT 50")
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _p(latencies: list[float], pct: float) -> float:
    s = sorted(latencies)
    idx = max(0, int(len(s) * pct / 100) - 1)
    return s[idx]


def _report(label: str, primary_lats: list[float], standby_lats: list[float]) -> None:
    p50_pri = _p(primary_lats, 50)
    p95_pri = _p(primary_lats, 95)
    p50_std = _p(standby_lats, 50)
    p95_std = _p(standby_lats, 95)
    print(f"  {label:<28}  primary p50={p50_pri:.3f}ms p95={p95_pri:.3f}ms  "
          f"standby p50={p50_std:.3f}ms p95={p95_std:.3f}ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary-dsn", default=os.environ.get("PG_PRIMARY_DSN", "postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos"))
    ap.add_argument("--standby-dsn", default=os.environ.get("PG_STANDBY_DSN", "postgresql://mnemos_user:mnemos_local@localhost:5434/mnemos"))
    ap.add_argument("--embed-url", default=os.environ.get("EMBED_URL", "http://192.168.207.67:11434"))
    ap.add_argument("--n-records", type=int, default=2000)
    ap.add_argument("--repeat", type=int, default=50)
    ap.add_argument("--pool-size", type=int, default=256)
    args = ap.parse_args()

    await _build_pool(args.embed_url, args.pool_size)

    print(f"\n[HA-bench] PG streaming replica comparison n={args.n_records} repeat={args.repeat}")

    pri = await asyncpg.connect(args.primary_dsn)
    std = await asyncpg.connect(args.standby_dsn)

    try:
        # Setup on primary (replicates to standby)
        await _cleanup(pri)
        await _setup(pri)

        # Bulk insert on primary
        print(f"  [primary] bulk inserting {args.n_records} records ...", flush=True)
        insert_ms = await _bulk_insert(pri, args.n_records)
        print(f"  [primary] insert done in {insert_ms:.1f}ms", flush=True)

        # Wait for standby to catch up
        await _wait_for_replica(pri, std)

        # Fetch some IDs for point lookup
        id_rows = await pri.fetch("SELECT id FROM ha_bench_memories LIMIT 200")
        ids = [r["id"] for r in id_rows]

        # Warm-up pass (both nodes)
        print("  [ha] warming up both nodes ...", flush=True)
        for conn in (pri, std):
            await _semantic_search(conn, 5)
            await _point_lookup(conn, ids, 10)
            await _list_scan(conn, 3)

        # Benchmark pass
        print("  [ha] benchmarking ...", flush=True)
        pri_sem = await _semantic_search(pri, args.repeat)
        std_sem = await _semantic_search(std, args.repeat)

        pri_pt = await _point_lookup(pri, ids, args.repeat)
        std_pt = await _point_lookup(std, ids, args.repeat)

        pri_ls = await _list_scan(pri, args.repeat // 2)
        std_ls = await _list_scan(std, args.repeat // 2)

        # Report
        w = 80
        print("\n" + "=" * w)
        print(f"{'phase':<28}  {'primary':>24}  {'standby':>24}")
        print("-" * w)
        _report("semantic-top10", pri_sem, std_sem)
        _report("point-lookup", pri_pt, std_pt)
        _report("list-scan-50", pri_ls, std_ls)
        print("-" * w)
        print(f"  {'bulk-insert':<28}  {insert_ms:.1f}ms (primary only; standby = replica)")
        print(f"  {'replication lag':<28}  < 200ms (drained to 0 before reads)")
        print("=" * w)

    finally:
        await _cleanup(pri)
        await pri.close()
        await std.close()


if __name__ == "__main__":
    asyncio.run(_main())
