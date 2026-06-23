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
ACC_CORPUS = 300  # records in recall-accuracy corpus
ACC_QUERIES = 100  # query vectors for recall@TOP_K measurement

# Pre-generated embedding pool (populated at startup when --embed-url is set)
_EMBED_POOL: list[list[float]] = []
_EMBED_URL: str = ""
_EMBED_MODEL: str = "nomic-embed-text"

PG_DSN = os.environ.get("PG_DSN", "postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos")
ORACLE_DSN = os.environ.get("ORACLE_DSN", "")
DB2_DSN = os.environ.get("DB2_DSN", "")
MYSQL_DSN = os.environ.get("MYSQL_DSN", "")
MARIADB_DSN = os.environ.get("MARIADB_DSN", "")
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


def _pool_vec() -> list[float]:
    """Return a vector from the pre-generated pool, or a random one if no pool."""
    if _EMBED_POOL:
        return random.choice(_EMBED_POOL)
    return _rand_vec()


async def _fetch_embedding(text: str) -> list[float]:
    """Call Ollama /api/embeddings endpoint and return L2-normalised vector."""
    import urllib.request

    payload = json.dumps({"model": _EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{_EMBED_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=30).read())
    vec = json.loads(raw)["embedding"]
    mag = sum(x * x for x in vec) ** 0.5
    return [x / mag for x in vec] if mag > 0 else vec


async def _build_embed_pool(n: int) -> None:
    """Pre-generate n real embeddings from diverse synthetic phrases."""
    global _EMBED_POOL
    categories = [
        "infrastructure",
        "facts",
        "user",
        "project",
        "reasoning",
        "research",
        "memory",
        "context",
        "knowledge",
        "retrieval",
    ]
    verbs = [
        "stores",
        "retrieves",
        "indexes",
        "compresses",
        "searches",
        "records",
        "recalls",
        "encodes",
        "fetches",
        "persists",
    ]
    nouns = [
        "vector",
        "embedding",
        "memory",
        "document",
        "record",
        "context",
        "session",
        "knowledge",
        "concept",
        "information",
    ]
    print(f"  [embed-pool] generating {n} embeddings from {_EMBED_URL} ...", flush=True)
    texts = [
        f"System {categories[i % len(categories)]} {verbs[i % len(verbs)]} {nouns[i % len(nouns)]} item {i}"
        for i in range(n)
    ]
    pool = []
    batch = 32
    for start in range(0, len(texts), batch):
        chunk = texts[start : start + batch]
        vecs = await asyncio.gather(*[_fetch_embedding(t) for t in chunk])
        pool.extend(vecs)
        print(f"  [embed-pool] {len(pool)}/{n}", end="\r", flush=True)
    _EMBED_POOL = pool
    print(f"  [embed-pool] done — {len(pool)} vectors cached          ")


def _brute_force_topk(
    query: list[float],
    corpus: list[tuple[str, list[float]]],
    k: int,
) -> list[str]:
    """Return the top-k memory IDs by cosine similarity (brute force).

    All embeddings are L2-normalized so cosine similarity = dot product.
    For unit-norm vectors, minimising Euclidean distance is equivalent to
    maximising cosine similarity, so this ground truth is correct for both
    PG (cosine) and Db2 (EUCLIDEAN) backends.
    """
    scores = [(mid, sum(q * c for q, c in zip(query, emb))) for mid, emb in corpus]
    scores.sort(key=lambda x: -x[1])
    return [mid for mid, _ in scores[:k]]


def _recall_stats(recalls: list[float], k: int) -> dict[str, Any]:
    if not recalls:
        return {"label": f"recall@{k}", "n": 0, "mean": None, "min": None, "p5": None}
    s = sorted(recalls)
    n = len(s)
    return {
        "label": f"recall@{k}",
        "n": n,
        "mean": round(statistics.mean(s), 4),
        "min": round(s[0], 4),
        "p5": round(s[max(0, int(n * 0.05))], 4) if n >= 10 else None,
    }


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


async def _make_mariadb_backend(dsn: str):
    # MariaDB is wire-compatible with aiomysql, so it reuses the MySQL pool;
    # only the vector dialect differs (handled by MariadbBackend).
    from mnemos.persistence.mysql import create_mysql_pool
    from mnemos.persistence.mariadb import MariadbBackend

    pool = await create_mysql_pool(dsn, min_size=1, max_size=4)
    b = MariadbBackend(pool, SimpleNamespace())
    return b, pool


# ── per-backend bench primitives ──────────────────────────────────────────────


async def _insert_one(
    backend, tx, *, memory_id: str, content: str, category: str, subcategory: str, embedding: list[float]
) -> None:
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
        records.append(
            {
                "id": f"{prefix}{i:06d}",
                "content": f"benchmark memory record {i} for {name} bakeoff run",
                "category": ["infrastructure", "facts", "user", "project"][i % 4],
                "subcategory": ["performance", "vector", "search", "index"][i % 4],
            }
        )
        embeddings.append(_pool_vec())

    # warmup: insert WARMUP records, don't time these
    print(f"  [{name}] warmup {WARMUP} records ...", flush=True)
    async with backend.transactional() as tx:
        for i in range(WARMUP):
            await _insert_one(
                backend,
                tx,
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
                chunk_ids = rep_ids[j : j + BATCH_CHUNK]
                for k, rid in enumerate(chunk_ids):
                    idx = j + k
                    await _insert_one(
                        backend,
                        tx,
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
    print(f"  [{name}] semantic search x{repeat * 10} ...", flush=True)
    sem_samples: list[float] = []
    for _ in range(repeat):
        for _ in range(10):
            qvec = _pool_vec()
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
    print(f"  [{name}] point lookup x{repeat * 20} ...", flush=True)
    lookup_samples: list[float] = []
    sample_ids = [r["id"] for r in records[: min(20, len(records))]]
    for _ in range(repeat):
        for mid in sample_ids:
            t0 = time.perf_counter()
            async with backend.transactional() as tx:
                await backend.memories.get_memory(tx, mid, visibility=vis)
            lookup_samples.append(time.perf_counter() - t0)
    lookup_stat = _stats(lookup_samples, "point-lookup")

    # ── phase 4: list/scan ──
    print(f"  [{name}] list scan x{repeat * 5} ...", flush=True)
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

    # ── pre-recall: rebuild PG HNSW index ────────────────────────────────────
    # Bulk-insert phase deletes 4968 records × repeat reps without VACUUM.
    # HNSW retains dead graph links to deleted entries, causing recall < 0.5.
    # Rebuild before the recall test to get an honest ANN accuracy number.
    if name == "pg+pgvector" and hasattr(backend, "_pool"):
        async with backend._pool.acquire() as _conn:
            await _conn.execute("VACUUM ANALYZE memories")
            await _conn.execute("REINDEX INDEX idx_memories_hnsw")

    # ── phase 5: recall accuracy ──
    # Insert a dedicated corpus with fully known embeddings; compute
    # brute-force cosine top-K as ground truth; compare with ANN results.
    # Ground truth includes the warmup records already in the table.
    print(f"  [{name}] recall accuracy: corpus={ACC_CORPUS} queries={ACC_QUERIES} K={TOP_K} ...", flush=True)
    acc_prefix = f"acc_{uuid.uuid4().hex[:10]}_"
    acc_records_meta = []
    acc_embeddings_list: list[list[float]] = []
    for i in range(ACC_CORPUS):
        acc_records_meta.append(
            {
                "id": f"{acc_prefix}{i:06d}",
                "content": f"accuracy corpus record {i}",
                "category": "bench",
                "subcategory": "accuracy",
            }
        )
        acc_embeddings_list.append(_pool_vec())

    async with backend.transactional() as tx:
        for i, arec in enumerate(acc_records_meta):
            await _insert_one(
                backend,
                tx,
                memory_id=arec["id"],
                content=arec["content"],
                category=arec["category"],
                subcategory=arec["subcategory"],
                embedding=acc_embeddings_list[i],
            )

    # Ground truth corpus = warmup records + acc corpus (all in namespace="bench").
    # Warmup embeddings are the first WARMUP entries of `embeddings`.
    gt_corpus = [(records[i]["id"], embeddings[i]) for i in range(WARMUP)] + [
        (acc_records_meta[i]["id"], acc_embeddings_list[i]) for i in range(ACC_CORPUS)
    ]
    recalls: list[float] = []
    for _ in range(ACC_QUERIES):
        qvec = _pool_vec()
        gt_ids = set(_brute_force_topk(qvec, gt_corpus, TOP_K))
        async with backend.transactional() as tx:
            ann_rows = await backend.memories.semantic_search(
                tx,
                embedding=qvec,
                limit=TOP_K,
                visibility=vis,
            )
        ann_ids = {r["id"] for r in ann_rows}
        recalls.append(len(ann_ids & gt_ids) / len(gt_ids) if gt_ids else 0.0)

    recall_stat = _recall_stats(recalls, TOP_K)

    async with backend.transactional() as tx:
        for arec in acc_records_meta:
            try:
                await backend.memories.delete_memory(tx, arec["id"], visibility=vis)
            except Exception:
                pass

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
        "recall": recall_stat,
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
    # recall@K section
    print("-" * len(header))
    recall_row = f"{'recall@' + str(TOP_K):<28}"
    for res in results:
        rc = res.get("recall", {})
        val = f"{rc['mean']:.4f}" if rc.get("mean") is not None else "n/a"
        recall_row += f"{val:>{col}}"
    print(recall_row)
    min_row = f"  {'min':>26}"
    for res in results:
        rc = res.get("recall", {})
        val = f"{rc['min']:.4f}" if rc.get("min") is not None else ""
        min_row += f"{val:>{col}}"
    print(min_row)
    print("=" * len(header))


# ── main ──────────────────────────────────────────────────────────────────────


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-records", type=int, default=1000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--skip-pg", action="store_true")
    ap.add_argument(
        "--embed-url",
        default="",
        help="Ollama base URL for real embeddings, e.g. http://192.168.207.67:11434. "
        "If omitted, random unit vectors are used.",
    )
    ap.add_argument(
        "--embed-model",
        default="nomic-embed-text",
        help="Model name to pass to Ollama /api/embeddings (default: nomic-embed-text)",
    )
    ap.add_argument(
        "--embed-pool-size",
        type=int,
        default=512,
        help="Number of embeddings to pre-generate into the pool (default: 512)",
    )
    args = ap.parse_args()

    global _EMBED_URL, _EMBED_MODEL
    _EMBED_URL = args.embed_url.rstrip("/")
    _EMBED_MODEL = args.embed_model

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    embed_src = f"nomic-embed-text @ {_EMBED_URL}" if _EMBED_URL else "random unit vectors"
    print(f"[BAKEOFF] 4-backend bench n={args.n_records} repeat={args.repeat} @ {ts}")
    print(f"  git={_sha()}")
    print(f"  embeddings={embed_src}")

    if _EMBED_URL:
        await _build_embed_pool(args.embed_pool_size)

    factories = []
    if not args.skip_pg:
        factories.append(("pg+pgvector", _make_pg_backend, PG_DSN))
    if ORACLE_DSN:
        factories.append(("oracle-23ai", _make_oracle_backend, ORACLE_DSN))
    if DB2_DSN:
        factories.append(("db2-12.1.5", _make_db2_backend, DB2_DSN))
    if MYSQL_DSN:
        factories.append(("mysql-9.0", _make_mysql_backend, MYSQL_DSN))
    if MARIADB_DSN:
        factories.append(("mariadb-11.8", _make_mariadb_backend, MARIADB_DSN))

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
            rc = res.get("recall", {})
            print(f"  {rc.get('label', 'recall'):30} mean={rc.get('mean')}  min={rc.get('min')}")
        except Exception as exc:
            print(f"  ERROR {bname}: {exc}")
            import traceback

            traceback.print_exc()
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
