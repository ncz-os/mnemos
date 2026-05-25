#!/usr/bin/env python3
"""4-backend vector performance bakeoff: PG+pgvector, Oracle 23ai, DB2 12.1.5, MySQL 9.0.

Runs all 4 through the MNEMOS PersistenceBackend abstraction so the
measurement includes the actual production code path, not raw SQL.

Usage (run on CERBERUS — all 4 backends live there):

    # Required env:
    PG_DSN=postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos
    ORACLE_DSN=oracle://mnemos:mnemos_dev@127.0.0.1:1522/FREEPDB1
    DB2_DSN=db2://db2inst1:mnemos_dev@127.0.0.1:50001/mnemos
    MYSQL_DSN=mysql://mnemos:mnemos_dev@127.0.0.1:3307/mnemos
    BENCH_HMAC_KEY=mnemos-bench-v2

    python3 scripts/bench_four_backends.py [--n-records 2000] [--repeat 5]

Output:
    docs/proof/bench-four-{ts}.json   signed artifact
    stdout                            summary table

Backends enabled by which DSN env vars are set.
PG is required (baseline). Oracle/DB2/MySQL are opt-in.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import hmac
import json
import os
import random
import statistics
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
OUT = REPO / "docs" / "proof"
OUT.mkdir(parents=True, exist_ok=True)

# ── config ────────────────────────────────────────────────────────────────────
EMBED_DIM = 768
TOP_K = 10
WARMUP = 32
BATCH_CHUNK = 128

PG_DSN = os.environ.get("PG_DSN", "postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos")
ORACLE_DSN = os.environ.get("ORACLE_DSN", "")
DB2_DSN = os.environ.get("DB2_DSN", "")
MYSQL_DSN = os.environ.get("MYSQL_DSN", "")
HMAC_KEY = os.environ.get("BENCH_HMAC_KEY", "mnemos-bench-v2")


# ── helpers ───────────────────────────────────────────────────────────────────


def _sha() -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        return "unknown"


def _stats(samples: list[float], label: str) -> dict[str, Any]:
    if not samples:
        return {"label": label, "n": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "min_ms": None, "max_ms": None}
    s = sorted(x * 1000 for x in samples)
    n = len(s)
    return {
        "label": label,
        "n": n,
        "p50_ms": round(s[n * 50 // 100], 3),
        "p95_ms": round(s[max(0, int(n * 0.95) - 1)], 3) if n >= 10 else None,
        "p99_ms": round(s[max(0, int(n * 0.99) - 1)], 3) if n >= 50 else None,
        "min_ms": round(s[0], 3),
        "max_ms": round(s[-1], 3),
        "median_ms": round(statistics.median(s), 3),
    }


def _rand_vec() -> list[float]:
    v = [random.gauss(0, 1) for _ in range(EMBED_DIM)]
    mag = sum(x * x for x in v) ** 0.5
    return [x / mag for x in v]


def _bench_user() -> Any:
    from mnemos.core.auth_context import UserContext
    return UserContext(
        user_id="bench",
        group_ids=[],
        role="root",
        namespace="bench",
        authenticated=True,
        session_id=None,
    )


def _root_vis(namespace: str | None = "bench") -> Any:
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
    )


# ── backend factory ───────────────────────────────────────────────────────────


async def _make_pg_backend(dsn: str):
    import asyncpg
    from mnemos.persistence.postgres import PostgresBackend
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, statement_cache_size=0)
    b = PostgresBackend(pool, SimpleNamespace())
    # Postgres backend has no open(); pool is already ready
    return b, pool


async def _open_noop(b):
    """Call open() only if the backend defines it."""
    if hasattr(b, "open"):
        await b.open()


async def _make_oracle_backend(dsn: str):
    from mnemos.persistence.oracle import create_oracle_pool, OracleBackend
    pool = await create_oracle_pool(dsn, min_size=1, max_size=4)
    b = OracleBackend(pool, SimpleNamespace())
    return b, pool


async def _make_db2_backend(dsn: str):
    from mnemos.persistence.db2 import create_db2_pool, Db2Backend
    pool = await create_db2_pool(dsn, min_size=1, max_size=4, acquire_timeout=60.0)
    b = Db2Backend(pool, SimpleNamespace())
    return b, pool


async def _make_mysql_backend(dsn: str):
    from mnemos.persistence.mysql import create_mysql_pool, MysqlBackend
    pool = await create_mysql_pool(dsn, min_size=1, max_size=4)
    b = MysqlBackend(pool, SimpleNamespace())
    return b, pool


# ── per-backend bench primitives ──────────────────────────────────────────────


async def _insert_one(backend, tx, *, memory_id: str, content: str, category: str,
                      subcategory: str, embedding: list[float]) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category=category,
        subcategory=subcategory,
        metadata_json="{}",
        quality_rating=50,
        owner_id="bench",
        namespace="bench",
        permission_mode=0,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent="bench",
        verbatim_content=None,
        created=now,
        updated=now,
    )
    if embedding and hasattr(backend.memories, "upsert_memory_embedding"):
        await backend.memories.upsert_memory_embedding(tx, memory_id, embedding)


async def _bench_backend(name: str, backend, repeat: int, n_records: int) -> dict[str, Any]:
    """Run all bench phases against one backend through the MNEMOS repo interface."""
    vis = _root_vis("bench")

    prefix = f"bench_{uuid.uuid4().hex[:10]}_"
    records = []
    embeddings = []
    for i in range(n_records):
        records.append({
            "id": f"{prefix}{i:06d}",
            "content": f"benchmark memory record {i} for {name} bakeoff run",
            "category": ["infrastructure", "facts", "user", "project"][i % 4],
            "subcategory": ["performance", "vector", "search", "index"][i % 4],
        })
        embeddings.append(_rand_vec())

    # warmup: insert WARMUP records, don't time these
    print(f"  [{name}] warmup {WARMUP} records ...", flush=True)
    async with backend.transactional() as tx:
        for i in range(WARMUP):
            await _insert_one(
                backend, tx,
                memory_id=records[i]["id"],
                content=records[i]["content"],
                category=records[i]["category"],
                subcategory=records[i]["subcategory"],
                embedding=embeddings[i],
            )

    # ── phase 1: bulk insert (remaining records) ──
    print(f"  [{name}] bulk insert {n_records - WARMUP} records ...", flush=True)
    insert_samples: list[float] = []
    for rep in range(repeat):
        rep_ids = [f"{prefix}r{rep}_{i:06d}" for i in range(n_records - WARMUP)]
        t0 = time.perf_counter()
        async with backend.transactional() as tx:
            for j in range(0, len(rep_ids), BATCH_CHUNK):
                chunk_ids = rep_ids[j: j + BATCH_CHUNK]
                for k, rid in enumerate(chunk_ids):
                    idx = j + k
                    await _insert_one(
                        backend, tx,
                        memory_id=rid,
                        content=f"rep{rep} bulk record {idx}",
                        category=records[idx % len(records)]["category"],
                        subcategory=records[idx % len(records)]["subcategory"],
                        embedding=embeddings[idx % len(embeddings)],
                    )
        insert_samples.append(time.perf_counter() - t0)
        # cleanup rep records
        async with backend.transactional() as tx:
            for rid in rep_ids:
                await backend.memories.delete_memory(tx, rid, visibility=vis)
    bulk_stat = _stats(insert_samples, f"bulk-insert-{n_records - WARMUP}")

    # ── phase 2: semantic search (vector similarity) ──
    print(f"  [{name}] semantic search x{repeat*10} ...", flush=True)
    sem_samples: list[float] = []
    for _ in range(repeat):
        for _ in range(10):
            qvec = _rand_vec()
            t0 = time.perf_counter()
            async with backend.transactional() as tx:
                results = await backend.memories.semantic_search(
                    tx,
                    embedding=qvec,
                    limit=TOP_K,
                    visibility=vis,
                )
            elapsed = time.perf_counter() - t0
            if results:
                sem_samples.append(elapsed)
    sem_stat = _stats(sem_samples, f"semantic-top{TOP_K}")

    # ── phase 3: point lookup ──
    print(f"  [{name}] point lookup x{repeat*20} ...", flush=True)
    lookup_samples: list[float] = []
    sample_ids = [r["id"] for r in records[:min(20, len(records))]]
    for _ in range(repeat):
        for mid in sample_ids:
            t0 = time.perf_counter()
            async with backend.transactional() as tx:
                await backend.memories.get_memory(tx, mid, visibility=vis)
            lookup_samples.append(time.perf_counter() - t0)
    lookup_stat = _stats(lookup_samples, "point-lookup")

    # ── phase 4: list/scan ──
    print(f"  [{name}] list scan x{repeat*5} ...", flush=True)
    list_samples: list[float] = []
    for _ in range(repeat):
        for cat in ["infrastructure", "facts", "user", "project", "bench"]:
            t0 = time.perf_counter()
            async with backend.transactional() as tx:
                await backend.memories.list_memories(
                    tx,
                    visibility=vis,
                    limit=50,
                    category=cat,
                )
            list_samples.append(time.perf_counter() - t0)
    list_stat = _stats(list_samples, "list-scan-50")

    # cleanup all bench records
    print(f"  [{name}] cleanup ...", flush=True)
    async with backend.transactional() as tx:
        for rec in records:
            try:
                await backend.memories.delete_memory(tx, rec["id"], visibility=vis)
            except Exception:
                pass

    return {
        "backend": name,
        "n_records": n_records,
        "phases": [bulk_stat, sem_stat, lookup_stat, list_stat],
    }


# ── report ────────────────────────────────────────────────────────────────────


def _print_table(results: list[dict]) -> None:
    backends = [r["backend"] for r in results]
    phases = [p["label"] for p in results[0]["phases"]] if results else []
    col = 18
    header = f"{'phase':<28}" + "".join(f"{b:>{col}}" for b in backends)
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for pi, phase in enumerate(phases):
        row = f"{phase:<28}"
        for res in results:
            p = res["phases"][pi]
            val = f"{p['p50_ms']}ms" if p["p50_ms"] is not None else "n/a"
            row += f"{val:>{col}}"
        print(row)
        # p95 sub-row
        p95row = f"  {'p95':>26}"
        for res in results:
            p = res["phases"][pi]
            val = f"{p['p95_ms']}ms" if p.get("p95_ms") is not None else ""
            p95row += f"{val:>{col}}"
        print(p95row)
    print("=" * len(header))


# ── main ──────────────────────────────────────────────────────────────────────


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-records", type=int, default=1000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--skip-pg", action="store_true")
    args = ap.parse_args()

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"[BAKEOFF] 4-backend bench n={args.n_records} repeat={args.repeat} @ {ts}")
    print(f"  git={_sha()}")

    factories = []
    if not args.skip_pg:
        factories.append(("pg+pgvector", _make_pg_backend, PG_DSN))
    if ORACLE_DSN:
        factories.append(("oracle-23ai", _make_oracle_backend, ORACLE_DSN))
    if DB2_DSN:
        factories.append(("db2-12.1.5", _make_db2_backend, DB2_DSN))
    if MYSQL_DSN:
        factories.append(("mysql-9.0", _make_mysql_backend, MYSQL_DSN))

    if not factories:
        sys.exit("No backends configured. Set at least PG_DSN.")

    results = []
    for bname, factory, dsn in factories:
        print(f"\n[{bname}] connecting ...", flush=True)
        try:
            backend, pool = await factory(dsn)
            await _open_noop(backend)
        except Exception as exc:
            print(f"  SKIP {bname}: {exc}")
            continue
        try:
            res = await _bench_backend(bname, backend, args.repeat, args.n_records)
            results.append(res)
            for p in res["phases"]:
                print(f"  {p['label']:30} p50={p['p50_ms']}ms  p95={p.get('p95_ms')}ms")
        except Exception as exc:
            print(f"  ERROR {bname}: {exc}")
            import traceback; traceback.print_exc()
        finally:
            try:
                await backend.close()
            except Exception:
                pass

    if not results:
        sys.exit("All backends failed.")

    _print_table(results)

    # artifact
    body = {
        "schema": "mnemos-bench-four/v1",
        "ts": ts,
        "git": _sha(),
        "n_records": args.n_records,
        "repeat": args.repeat,
        "results": results,
    }
    bjson = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str).encode()
    sig = hmac.new(HMAC_KEY.encode(), bjson, hashlib.sha256).hexdigest()
    artifact = {"evidence": body, "hmac_sha256": sig}
    out = OUT / f"bench-four-{ts}.json"
    out.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"\n[ARTIFACT] {out}")


if __name__ == "__main__":
    asyncio.run(_main())
