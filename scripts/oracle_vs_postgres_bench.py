"""Side-by-side perf comparison: Oracle 23ai (PROTEUS) vs Postgres+pgvector (PYTHIA).

Runs the same workload against both backends and emits a signed JSON
artifact under ``docs/proof/`` so the numbers are traceable to a
specific Oracle build, Postgres build, dataset size, and git commit.

Workload (per backend, N iterations each):

1. ``SELECT COUNT(*) FROM memories`` (warm-cache baseline)
2. ``fetch_by_id`` — random sampled id from the live set
3. ``list_page`` — recent 20 ordered by created DESC
4. ``fts_substring`` — substring scan for a fixed token
5. ``semantic_search`` — cosine over a 768-d random query vector
   (uses pgvector ``<=>`` on Postgres and ``VECTOR_DISTANCE(..., COSINE)``
   on Oracle)
6. ``insert+delete`` — round-trip on a synthetic ``bench-<uuid>`` row

Outputs p50/p95/p99 + min/mean/max in ms per operation per backend,
plus the dataset sizes seen on each side.

Run::

    .venv/bin/python scripts/oracle_vs_postgres_bench.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ORACLE_DSN = os.environ.get("ORACLE_BENCH_DSN", "oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1")
PG_HOST = os.environ.get("PG_BENCH_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PG_BENCH_PORT", "5433"))
PG_DB = os.environ.get("PG_BENCH_DB", "mnemos")
PG_USER = os.environ.get("PG_BENCH_USER", "mnemos_user")
PG_PASSWORD = os.environ.get("PG_BENCH_PASSWORD", "mnemos_secure_password")
HMAC_KEY = os.environ.get("ORACLE_PROOF_HMAC_KEY", "mnemos-oracle-proof-v1")
EMBED_DIM = int(os.environ.get("BENCH_EMBED_DIM", "768"))
N_ITER = int(os.environ.get("BENCH_N", "50"))


def _probe_host_specs(host: str, ssh_user: str = "jasonperlow") -> dict[str, Any]:
    """SSH out to ``host`` and capture CPU/RAM/storage specs.

    The fair-comparison story for the Oracle Free vs Postgres bench
    depends on knowing the underlying hardware asymmetry. We capture
    that into the perf artifact so the numbers can never be read out
    of context.
    """
    if not host:
        return {"reachable": False, "error": "empty host"}
    try:
        out = subprocess.check_output(
            [
                "ssh",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "BatchMode=yes",
                f"{ssh_user}@{host}",
                "echo MODEL:; cat /proc/cpuinfo | grep 'model name' | head -1; "
                "echo CPUS:; lscpu | grep -E '^CPU\\(s\\):|^Thread|^CPU max MHz' | head; "
                "echo MEM:; free -h | head -2; "
                "echo DISK:; lsblk -d -o NAME,SIZE,ROTA,MODEL,TRAN 2>/dev/null | head -10",
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=8,
        )
        return {"reachable": True, "raw": out}
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    samples_sorted = sorted(samples)
    return {
        "n": len(samples),
        "min_ms": round(samples_sorted[0], 3),
        "p50_ms": round(statistics.median(samples_sorted), 3),
        "p95_ms": round(samples_sorted[int(0.95 * (len(samples_sorted) - 1))], 3),
        "p99_ms": round(samples_sorted[int(0.99 * (len(samples_sorted) - 1))], 3),
        "max_ms": round(samples_sorted[-1], 3),
        "mean_ms": round(statistics.fmean(samples_sorted), 3),
    }


async def _time_async(coro_fn, n: int) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await coro_fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _random_vec(dim: int) -> list[float]:
    return [random.uniform(-1.0, 1.0) for _ in range(dim)]


# ---------------------------------------------------------------- Postgres


async def _bench_postgres(n: int, query_vec: list[float]) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASSWORD)
    try:
        total = await conn.fetchval("SELECT COUNT(*) FROM memories")
        # NOTE: PYTHIA Postgres predates the v1_multiuser deleted_at column.
        # We benchmark against the actual columns it carries.
        sample_ids = [r["id"] for r in await conn.fetch("SELECT id FROM memories ORDER BY random() LIMIT 200")]
        with_embed = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        vec_lit = "[" + ",".join(f"{v:.7f}" for v in query_vec) + "]"

        async def count():
            await conn.fetchval("SELECT COUNT(*) FROM memories")

        async def fetch_by_id():
            mid = random.choice(sample_ids)
            await conn.fetchrow("SELECT * FROM memories WHERE id = $1", mid)

        async def list_page():
            await conn.fetch(
                "SELECT id, category, owner_id, namespace, created FROM memories ORDER BY created DESC LIMIT 20"
            )

        async def fts_substring():
            await conn.fetch(
                "SELECT id FROM memories WHERE content ILIKE $1 LIMIT 20",
                "%mnemos%",
            )

        async def semantic_search():
            await conn.fetch(
                "SELECT id, 1 - (embedding <=> $1::vector) AS sim "
                "FROM memories WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> $1::vector LIMIT 10",
                vec_lit,
            )

        async def insert_delete():
            mid = f"bench-{uuid.uuid4().hex}"
            await conn.execute(
                "INSERT INTO memories (id, content, category, owner_id, namespace, "
                "created, updated) VALUES ($1, $2, 'bench', 'bench-owner', 'bench', "
                "NOW(), NOW())",
                mid,
                "bench content",
            )
            await conn.execute("DELETE FROM memories WHERE id = $1", mid)

        ops = {
            "count_star": await _time_async(count, n),
            "fetch_by_id": await _time_async(fetch_by_id, n),
            "list_page": await _time_async(list_page, n),
            "fts_substring": await _time_async(fts_substring, n),
            "semantic_search": await _time_async(semantic_search, n),
            "insert_delete": await _time_async(insert_delete, n),
        }
        return {
            "backend": "postgres",
            "total_memories": total,
            "memories_with_embedding": with_embed,
            "version_banner": (await conn.fetchval("SELECT version()")).splitlines()[0],
            "ops": {name: _stats(s) for name, s in ops.items()},
        }
    finally:
        await conn.close()


# ---------------------------------------------------------------- Oracle


async def _bench_oracle(n: int, query_vec: list[float]) -> dict[str, Any]:
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()
            await cur.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
            (total,) = await cur.fetchone()
            await cur.execute(
                "SELECT id FROM memories WHERE deleted_at IS NULL AND ROWNUM <= 200 ORDER BY ORA_HASH(id)"
            )
            sample_ids = [row[0] for row in await cur.fetchall()]
            await cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
            (with_embed,) = await cur.fetchone()
            await cur.execute("SELECT BANNER_FULL FROM v$version")
            (banner,) = await cur.fetchone()
            vec_lit = "[" + ",".join(f"{v:.7f}" for v in query_vec) + "]"
            cur.close()

            async def count():
                c = conn.cursor()
                await c.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
                await c.fetchone()
                c.close()

            async def fetch_by_id():
                c = conn.cursor()
                mid = random.choice(sample_ids)
                await c.execute(
                    "SELECT id, content, category, owner_id, namespace, created, updated "
                    "FROM memories WHERE id = :id AND deleted_at IS NULL",
                    {"id": mid},
                )
                await c.fetchone()
                c.close()

            async def list_page():
                c = conn.cursor()
                await c.execute(
                    "SELECT id, category, owner_id, namespace, created "
                    "FROM memories WHERE deleted_at IS NULL "
                    "ORDER BY created DESC FETCH FIRST 20 ROWS ONLY"
                )
                await c.fetchall()
                c.close()

            async def fts_substring():
                c = conn.cursor()
                await c.execute(
                    "SELECT id FROM memories WHERE deleted_at IS NULL "
                    "AND DBMS_LOB.INSTR(content, :q) > 0 "
                    "FETCH FIRST 20 ROWS ONLY",
                    {"q": "mnemos"},
                )
                await c.fetchall()
                c.close()

            async def semantic_search():
                c = conn.cursor()
                await c.execute(
                    "SELECT id, "
                    "VECTOR_DISTANCE(embedding, TO_VECTOR(:q), COSINE) AS d "
                    "FROM memories WHERE embedding IS NOT NULL "
                    "ORDER BY VECTOR_DISTANCE(embedding, TO_VECTOR(:q), COSINE) "
                    "FETCH FIRST 10 ROWS ONLY",
                    {"q": vec_lit},
                )
                await c.fetchall()
                c.close()

            async def insert_delete():
                c = conn.cursor()
                mid = f"bench-{uuid.uuid4().hex}"
                await c.execute(
                    "INSERT INTO memories (id, content, category, owner_id, "
                    "namespace, created, updated) "
                    "VALUES (:id, :content, 'bench', 'bench-owner', 'bench', "
                    "SYSTIMESTAMP, SYSTIMESTAMP)",
                    {"id": mid, "content": "bench content"},
                )
                await c.execute("DELETE FROM memories WHERE id = :id", {"id": mid})
                c.close()

            ops = {
                "count_star": await _time_async(count, n),
                "fetch_by_id": await _time_async(fetch_by_id, n),
                "list_page": await _time_async(list_page, n),
                "fts_substring": await _time_async(fts_substring, n),
                "semantic_search": await _time_async(semantic_search, n),
                "insert_delete": await _time_async(insert_delete, n),
            }
            await conn.commit()
            return {
                "backend": "oracle",
                "total_memories": total,
                "memories_with_embedding": with_embed,
                "version_banner": banner,
                "ops": {name: _stats(s) for name, s in ops.items()},
            }
    finally:
        await pool.close()


# ---------------------------------------------------------------- driver


def _sign(body: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    sig = hmac.new(HMAC_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {
        "evidence": body,
        "hmac_sha256": sig,
        "hmac_key_id": hashlib.sha256(HMAC_KEY.encode()).hexdigest()[:16],
    }


async def main_async(n: int) -> dict[str, Any]:
    random.seed(0)
    query_vec = _random_vec(EMBED_DIM)
    started = _now_iso()
    pg_result = await _bench_postgres(n, query_vec)
    or_result = await _bench_oracle(n, query_vec)
    finished = _now_iso()
    # Hardware metadata — fairness-critical context that should never
    # be stripped from a published perf number.
    pg_host = os.environ.get("PG_HW_HOST", "192.168.207.67")
    oracle_host = os.environ.get("ORACLE_HW_HOST", "192.168.207.25")
    return _sign(
        {
            "schema": "mnemos-oracle-perf/v1",
            "git_head_sha": _git_head(),
            "started_utc": started,
            "finished_utc": finished,
            "iterations_per_op": n,
            "embedding_dim": EMBED_DIM,
            "hardware": {
                "postgres_host": pg_host,
                "oracle_host": oracle_host,
                "postgres_specs": _probe_host_specs(pg_host),
                "oracle_specs": _probe_host_specs(oracle_host),
                "note": (
                    "Hardware is asymmetric — PYTHIA is modern CPU + NVMe, "
                    "PROTEUS is older Skylake i7 + SATA3 SSD RAID. Any "
                    "operation where Oracle Free still wins is doing so "
                    "across a substantial hardware gap."
                ),
            },
            "results": [pg_result, or_result],
        }
    )


def _print_summary(art: dict[str, Any]) -> None:
    ev = art["evidence"]
    print(f"\n{ev['schema']} — git={ev['git_head_sha'][:12]} n={ev['iterations_per_op']}")
    pg, ora = ev["results"]
    print(f"\n  Postgres: {pg['version_banner']}")
    print(f"    memories={pg['total_memories']} embedded={pg['memories_with_embedding']}")
    print(f"  Oracle:   {ora['version_banner'].splitlines()[0]}")
    print(f"    memories={ora['total_memories']} embedded={ora['memories_with_embedding']}")
    ops = sorted(set(pg["ops"]) | set(ora["ops"]))
    print(
        f"\n{'op':18s}  "
        f"{'pg p50':>10s} {'pg p95':>10s} {'pg p99':>10s}  "
        f"{'ora p50':>10s} {'ora p95':>10s} {'ora p99':>10s}  "
        f"{'pg/ora p50':>12s}"
    )
    for op in ops:
        p = pg["ops"].get(op, {})
        o = ora["ops"].get(op, {})
        if not p or not o:
            continue
        ratio = p["p50_ms"] / o["p50_ms"] if o["p50_ms"] else float("inf")
        print(
            f"{op:18s}  "
            f"{p['p50_ms']:10.3f} {p['p95_ms']:10.3f} {p['p99_ms']:10.3f}  "
            f"{o['p50_ms']:10.3f} {o['p95_ms']:10.3f} {o['p99_ms']:10.3f}  "
            f"{ratio:11.2f}x"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=N_ITER)
    ap.add_argument(
        "--out",
        default=str(
            REPO_ROOT
            / "docs"
            / "proof"
            / f"oracle-perf-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        ),
    )
    args = ap.parse_args()

    artifact = asyncio.run(main_async(args.n))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    _print_summary(artifact)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
