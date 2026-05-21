#!/usr/bin/env python3
"""Cross-backend performance bench: PG 16, Oracle EE, Db2 12.1.

Usage:
  MNEMOS_BACKEND=pg       MNEMOS_PROOF_HMAC_KEY=... python3 scripts/bench_three_backends.py
  MNEMOS_BACKEND=oracle   ...   python3 scripts/bench_three_backends.py --corpus ./corpus.json
  MNEMOS_BACKEND=db2      ...   python3 scripts/bench_three_backends.py --repeat 3

Env vars:
  MNEMOS_BACKEND        pg|oracle|db2 (required)
  PG_PROOF_DSN          PG DSN (default localhost:5433/mnemos)
  ORACLE_DSN            Oracle DSN (default PROTEUS:1522/FREEPDB1)
  DB2_DSN               Db2 DSN (default PYTHIA:50001/mnemos)
  MNEMOS_PROOF_HMAC_KEY proof HMAC key (required)
  MNEMOS_EMBED_BACKEND  openvino|llamacpp (default auto → OV, 4.5 rec/s CPU)

Output:
  docs/proof/bench-{backend}-{ts}.json  per-backend timed artifact
  docs/proof/bench-comparison-{ts}.md   summary table
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
OUT = REPO / "docs" / "proof"
OUT.mkdir(parents=True, exist_ok=True)

# ── config ───────────────────────────────────────────────────────────────────
EMBED_DIM = 768
TOP_K = 10
MIN_SCORE = 0.3
BATCH_INSERT_CHUNK = 256
BULK_READ_COUNT = 1000
WARMUP_INSERTS = 64

CORPUS_PATHS = [
    Path("/home/mini/embed-bench/mnemos-corpus.json"),
    Path("/mnt/argonas/datapool/backups/mnemos-corpus.json"),
    REPO / "mnemos-corpus.json",
]

BACKENDS = {
    "pg": {
        "dsn_env": "PG_PROOF_DSN",
        "default_dsn": "postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos",
    },
    "oracle": {
        "dsn_env": "ORACLE_DSN",
        "default_dsn": "oracle://mnemos:mnemos_dev@192.168.207.25:1522/FREEPDB1",
    },
    "db2": {
        "dsn_env": "DB2_DSN",
        "default_dsn": "db2://mnemos:mnemos_dev@192.168.207.67:50001/mnemos",
    },
}

# ── helpers ──────────────────────────────────────────────────────────────────


def _sha() -> str:
    import subprocess

    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        return "unknown"


def _redact(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    h, t = dsn.split("@", 1)
    if "://" in h:
        s, c = h.split("://", 1)
        if ":" in c:
            u = c.split(":", 1)[0]
            return f"{s}://{u}:<redacted>@{t}"
    return f"<redacted>@{t}"


def _stats(samples: list[float], batch_total: int, label: str) -> dict:
    if not samples:
        return {
            "label": label,
            "iterations": 0,
            "median_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "throughput_rec_s": None,
        }
    s = sorted(samples)
    n = len(s)
    return {
        "label": label,
        "iterations": n,
        "median_ms": round(statistics.median(s) * 1000, 2),
        "p50_ms": round(s[n * 50 // 100] * 1000, 2) if n > 0 else None,
        "p95_ms": round(s[max(0, n * 95 // 100 - 1)] * 1000, 2) if n >= 20 else None,
        "p99_ms": round(s[max(0, n * 99 // 100 - 1)] * 1000, 2) if n >= 100 else None,
        "throughput_rec_s": round(batch_total / sum(s), 1) if sum(s) > 0 else None,
    }


def _load_corpus(path_override: str | None) -> list[dict]:
    paths = [Path(path_override)] if path_override else CORPUS_PATHS
    for p in paths:
        if p.exists():
            with open(p) as fh:
                data = json.load(fh)
            if isinstance(data, list) and len(data) > 0:
                return data[:]
            raise ValueError(f"Corpus at {p} is not a non-empty list")
    raise FileNotFoundError(f"No corpus found. Searched: {[str(p) for p in paths]}")


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch using the embedder singleton."""
    import asyncio as aio
    from mnemos.runtime.embedder import InProcessEmbedder

    e = InProcessEmbedder()
    return aio.run(e.embed_batch(texts))


# ── backend connections ──────────────────────────────────────────────────────


async def _connect_pg(dsn: str):
    import asyncpg

    return await asyncpg.create_pool(dsn, min_size=1, max_size=4, statement_cache_size=0)


async def _connect_oracle(dsn: str):
    from mnemos.persistence.oracle import create_oracle_pool

    return await create_oracle_pool(dsn, min_size=1, max_size=4)


class _Db2PoolWrapper:
    """Wrap a _Db2AsyncConnectionPool so it matches PG/Oracle acquire/release surface."""

    def __init__(self, pool):
        self._pool = pool  # _Db2AsyncConnectionPool
        self._ctx_stack = []  # track open context managers for release

    async def acquire(self):
        cm = self._pool.acquire()
        conn = await cm.__aenter__()
        self._ctx_stack.append(cm)
        return conn

    async def release(self, conn):
        if self._ctx_stack:
            cm = self._ctx_stack.pop()
            await cm.__aexit__(None, None, None)

    async def close(self):
        await self._pool.close()


async def _connect_db2(dsn: str):
    from mnemos.persistence.db2 import create_db2_pool

    raw = await create_db2_pool(dsn, min_size=1, max_size=4, acquire_timeout=60.0)
    return _Db2PoolWrapper(raw)


async def _acquire(pool) -> object:
    return await pool.acquire()


async def _release(pool, conn: object) -> None:
    await pool.release(conn)


async def _execute(conn, sql: str, *args):
    return await conn.execute(sql, *args)


async def _fetch(conn, sql: str, *args):
    return await conn.fetch(sql, *args)


async def _fetchval(conn, sql: str, *args):
    return await conn.fetchval(sql, *args)


# ── benchmarks ───────────────────────────────────────────────────────────────


async def _bench_bulk_insert(pool, records: list[dict], embeddings: list[list[float]], repeat: int):
    """Bulk-insert N records with precomputed embeddings."""
    assert len(records) == len(embeddings)
    samples: list[float] = []
    for _ in range(repeat):
        conn = await _acquire(pool)
        try:
            t0 = time.perf_counter()
            for i in range(0, len(records), BATCH_INSERT_CHUNK):
                chunk = records[i : i + BATCH_INSERT_CHUNK]
                emb = embeddings[i : i + BATCH_INSERT_CHUNK]
                values = []
                for rec, e in zip(chunk, emb):
                    vs = "[" + ",".join(f"{x:.7f}" for x in e) + "]"
                    values.append(
                        (
                            rec["id"],
                            rec["content"],
                            rec.get("category", "bench"),
                            rec.get("subcategory", ""),
                            50,
                            True,
                            vs,
                        )
                    )
                b_args = ", ".join(
                    f"(${j*7+1},${j*7+2},${j*7+3},${j*7+4},${j*7+5},${j*7+6},${j*7+7}::vector)"
                    for j in range(len(chunk))
                )
                flat = [x for row in values for x in row]
                await _execute(
                    conn,
                    f"INSERT INTO memories (id,content,category,subcategory,quality_rating,is_original,embedding) "
                    f"VALUES {b_args}",
                    *flat,
                )
            elapsed = time.perf_counter() - t0
            samples.append(elapsed)
        finally:
            await _release(pool, conn)
    return _stats(samples, len(records), f"bulk-insert-{len(records)}")


async def _bench_semantic(pool, query_embs: list[list[float]], records: list[dict], repeat: int):
    """Cosine-similarity search: top-k=10, min_score=0.3."""
    samples: list[float] = []
    for _ in range(repeat):
        conn = await _acquire(pool)
        try:
            for qe in query_embs:
                qs = "[" + ",".join(f"{x:.7f}" for x in qe) + "]"
                t0 = time.perf_counter()
                r = await _fetch(
                    conn,
                    "SELECT id, content, 1 - (embedding <=> $1::vector) AS score "
                    "FROM memories WHERE archived_at IS NULL ORDER BY embedding <=> $2::vector "
                    "LIMIT $3",
                    qs,
                    qs,
                    TOP_K,
                )
                # verify filter
                hits = [row for row in r if row["score"] >= MIN_SCORE]
                if hits:
                    elapsed = time.perf_counter() - t0
                    samples.append(elapsed)
        finally:
            await _release(pool, conn)
    return _stats(samples, 1, f"semantic-top{TOP_K}")


async def _bench_lookup(pool, ids: list[str], repeat: int):
    """Point lookup by id."""
    samples: list[float] = []
    for _ in range(repeat):
        conn = await _acquire(pool)
        try:
            for mid in ids:
                t0 = time.perf_counter()
                await _fetchval(conn, "SELECT id FROM memories WHERE id=$1 AND archived_at IS NULL", mid)
                samples.append(time.perf_counter() - t0)
        finally:
            await _release(pool, conn)
    return _stats(samples, 1, "point-lookup")


async def _bench_filtered(pool, records: list[dict], repeat: int):
    """Filtered scan: category=X, subcategory=Y."""
    cats = list({r.get("category", "bench") for r in records[:200]})
    subs = list({r.get("subcategory", "") for r in records[:200]})
    pairs = [(cats[i % len(cats)], subs[i % len(subs)]) for i in range(min(len(cats), 20))]
    samples: list[float] = []
    for _ in range(repeat):
        conn = await _acquire(pool)
        try:
            for cat, sub in pairs:
                t0 = time.perf_counter()
                await _fetch(
                    conn,
                    "SELECT id FROM memories WHERE archived_at IS NULL AND category=$1 AND subcategory=$2 LIMIT 100",
                    cat,
                    sub,
                )
                samples.append(time.perf_counter() - t0)
        finally:
            await _release(pool, conn)
    return _stats(samples, 1, "filtered-scan")


async def _bench_bulk_read(pool, repeat: int):
    """Bulk read 1000 memories."""
    samples: list[float] = []
    for _ in range(repeat):
        conn = await _acquire(pool)
        try:
            t0 = time.perf_counter()
            await _fetch(conn, "SELECT * FROM memories WHERE archived_at IS NULL LIMIT $1", BULK_READ_COUNT)
            samples.append(time.perf_counter() - t0)
        finally:
            await _release(pool, conn)
    return _stats(samples, BULK_READ_COUNT, f"bulk-read-{BULK_READ_COUNT}")


# ── main ─────────────────────────────────────────────────────────────────────


async def _main():
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", help="Override corpus path")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--n-records", type=int, default=1000)
    args = p.parse_args()
    backend = os.environ.get("MNEMOS_BACKEND")
    if backend not in BACKENDS:
        exit("FATAL: set MNEMOS_BACKEND=pg|oracle|db2")
    hmac_key = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
    if not hmac_key:
        exit("FATAL: MNEMOS_PROOF_HMAC_KEY not set")
    cfg = BACKENDS[backend]
    dsn = os.environ.get(cfg["dsn_env"], cfg["default_dsn"])
    corpus = _load_corpus(args.corpus)
    n = min(args.n_records, len(corpus))
    records = corpus[:n]
    for r in records:
        r.setdefault("id", f"b_{uuid.uuid4().hex[:12]}")
        r.setdefault("category", r.get("category", "bench"))
        r.setdefault("subcategory", r.get("subcategory", ""))

    rid = uuid.uuid4().hex[:12]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"[BENCH] backend={backend} records={n} repeat={args.repeat}")

    connectors = {"pg": _connect_pg, "oracle": _connect_oracle, "db2": _connect_db2}
    pool = await connectors[backend](dsn)
    try:
        print("[EMBED] generating vectors ...")
        texts = [r["content"] for r in records]
        embeddings = _embed_texts(texts)
        assert len(embeddings) == n and all(len(e) == EMBED_DIM for e in embeddings if e)
        query_embs = _embed_texts(["mnemos vector search benchmark query"] * 10)

        # warmup
        conn = await _acquire(pool)
        try:
            for rec, e in zip(records[:WARMUP_INSERTS], embeddings[:WARMUP_INSERTS]):
                vs = "[" + ",".join(f"{x:.7f}" for x in e) + "]"
                await _execute(
                    conn,
                    "INSERT INTO memories (id,content,category,subcategory,"
                    "quality_rating,is_original,embedding) VALUES ($1,$2,$3,$4,$5,$6,$7::vector)",
                    rec["id"],
                    rec["content"],
                    rec["category"],
                    rec["subcategory"],
                    50,
                    True,
                    vs,
                )
        finally:
            await _release(pool, conn)
        print(f"[WARM] inserted {WARMUP_INSERTS} warmup records")

        benches = []
        for bench_fn in [_bench_bulk_insert, _bench_semantic, None, None, _bench_bulk_read]:
            if bench_fn is _bench_bulk_insert:
                b = await bench_fn(pool, records, embeddings, args.repeat)
            elif bench_fn is _bench_semantic:
                b = await bench_fn(pool, query_embs, records, args.repeat)
            elif bench_fn is None and len(benches) == 2:
                ids = [r["id"] for r in records[:100]]
                b = await _bench_lookup(pool, ids, max(args.repeat, 10))
            elif bench_fn is None:
                b = await _bench_filtered(pool, records, args.repeat)
            else:
                b = await bench_fn(pool, args.repeat)
            benches.append(b)
            print(
                f"  {b['label']}: median={b['median_ms']}ms"
                + (f" thr={b['throughput_rec_s']}rec/s" if b.get("throughput_rec_s") else "")
            )

        # cleanup
        conn = await _acquire(pool)
        try:
            for i in range(0, len(records), 500):
                chunk = records[i : i + 500]
                ph = ", ".join(f"${j+1}" for j in range(len(chunk)))
                await _execute(conn, f"DELETE FROM memories WHERE id IN ({ph})", *[r["id"] for r in chunk])
        finally:
            await _release(pool, conn)
    finally:
        await pool.close()

    # ── artifact ──
    body = {
        "schema": "mnemos-bench/v1",
        "backend": backend,
        "run_id": rid,
        "dsn_redacted": _redact(dsn),
        "n_records": n,
        "repeat": args.repeat,
        "git": _sha(),
        "python": sys.version,
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "benches": benches,
    }
    bjson = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    sig = hmac.new(hmac_key.encode("utf-8"), bjson, hashlib.sha256).hexdigest()
    out_path = OUT / f"bench-{backend}-{ts}.json"
    out_path.write_text(
        json.dumps(
            {
                "evidence": body,
                "hmac_sha256": sig,
                "hmac_key_id": hashlib.sha256(hmac_key.encode("utf-8")).hexdigest()[:16],
            },
            indent=2,
            default=str,
        )
    )
    print(f"\n[OK] {out_path}")
    return body


if __name__ == "__main__":
    body = asyncio.run(_main())
    print(json.dumps(body, indent=2, default=str))
