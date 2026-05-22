"""EE feature #4 — HNSW VECTOR index benchmark.

Seeds N synthetic 384-dim float32 embeddings on the EE PDB then runs
top-K cosine-similarity scans both with and without an HNSW index.
Emits a JSON artifact under <archived bench artifact>
"""

from __future__ import annotations

import argparse
import array
import json
import os
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracledb  # noqa: E402

DEFAULT_DSN = "192.168.207.25:1521/ORCLPDB1"
DEFAULT_USER = "MNEMOS"
DEFAULT_PWD = "mnemos_dev"
DIM = 384
N_ROWS = 1000
K = 10
QUERIES = 50
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY or HMAC_KEY == "mnemos-oracle-proof-v1":
    print("ERROR: MNEMOS_PROOF_HMAC_KEY env var required (fail-closed).", file=sys.stderr)
    sys.exit(1)
HMAC_KEY = HMAC_KEY.encode("utf-8")


def _connect(dsn: str, user: str, pwd: str) -> oracledb.Connection:
    return oracledb.connect(user=user, password=pwd, dsn=dsn)


def _to_vec(vec: list[float]) -> array.array:
    return array.array("f", vec)


def _seed(conn: oracledb.Connection, n: int) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM memories WHERE owner_id = 'ee-bench'")
    rng = random.Random(42)
    rows = []
    for i in range(n):
        v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
        # normalize
        norm = sum(x * x for x in v) ** 0.5
        v = [x / norm for x in v]
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "owner_id": "ee-bench",
                "namespace": "bench",
                "content": f"synthetic-{i}",
                "embedding": _to_vec(v),
            }
        )
    cur.executemany(
        """
        INSERT INTO memories (id, owner_id, namespace, content, embedding, created_at, updated_at)
        VALUES (:id, :owner_id, :namespace, :content, :embedding, SYSTIMESTAMP, SYSTIMESTAMP)
        """,
        rows,
    )
    conn.commit()
    return n


def _make_query_vector() -> array.array:
    rng = random.Random(7)
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = sum(x * x for x in v) ** 0.5
    return _to_vec([x / norm for x in v])


def _bench_query(conn: oracledb.Connection, q: array.array, queries: int, k: int) -> dict:
    cur = conn.cursor()
    sql = """
        SELECT id, VECTOR_DISTANCE(embedding, :q, COSINE) d
        FROM memories
        WHERE owner_id = 'ee-bench'
        ORDER BY d
        FETCH FIRST :k ROWS ONLY
    """
    timings = []
    for _ in range(queries):
        t0 = time.perf_counter()
        cur.execute(sql, q=q, k=k)
        cur.fetchall()
        timings.append((time.perf_counter() - t0) * 1000.0)
    return {
        "queries": queries,
        "k": k,
        "min_ms": min(timings),
        "p50_ms": statistics.median(timings),
        "p95_ms": sorted(timings)[int(0.95 * (queries - 1))],
        "max_ms": max(timings),
        "mean_ms": statistics.mean(timings),
    }


def _index_exists(conn: oracledb.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM user_indexes WHERE index_name = :n",
        n=name.upper(),
    )
    return cur.fetchone()[0] > 0


def _create_hnsw(conn: oracledb.Connection) -> dict:
    cur = conn.cursor()
    t0 = time.perf_counter()
    cur.execute(
        """
        CREATE VECTOR INDEX idx_memories_embed_hnsw ON memories (embedding)
        ORGANIZATION INMEMORY NEIGHBOR GRAPH
        WITH DISTANCE COSINE
        PARAMETERS (TYPE HNSW, NEIGHBORS 32, EFCONSTRUCTION 200)
        """
    )
    return {"create_ms": (time.perf_counter() - t0) * 1000.0}


def _hmac(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac_new(HMAC_KEY, canon, sha256).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PWD)
    ap.add_argument("--rows", type=int, default=N_ROWS)
    ap.add_argument("--queries", type=int, default=QUERIES)
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    conn = _connect(args.dsn, args.user, args.password)

    # Cleanup prior index if rerun
    cur = conn.cursor()
    if _index_exists(conn, "idx_memories_embed_hnsw"):
        cur.execute("DROP INDEX idx_memories_embed_hnsw")
        conn.commit()

    print(f"[seed] inserting {args.rows} rows with {DIM}-dim embeddings...", flush=True)
    seeded = _seed(conn, args.rows)
    print(f"[seed] done seeded={seeded}", flush=True)

    q = _make_query_vector()

    print("[bench] no-index baseline...", flush=True)
    no_idx = _bench_query(conn, q, args.queries, K)
    print(f"[bench] no_idx p50={no_idx['p50_ms']:.2f}ms p95={no_idx['p95_ms']:.2f}ms", flush=True)

    print("[index] CREATE VECTOR INDEX ... HNSW INMEMORY ...", flush=True)
    idx_meta = _create_hnsw(conn)
    print(f"[index] created in {idx_meta['create_ms']:.0f}ms", flush=True)

    print("[bench] with-index ...", flush=True)
    with_idx = _bench_query(conn, q, args.queries, K)
    print(f"[bench] with_idx p50={with_idx['p50_ms']:.2f}ms p95={with_idx['p95_ms']:.2f}ms", flush=True)

    # Pull DB version + index metadata
    cur.execute("SELECT BANNER FROM v$version FETCH FIRST 1 ROWS ONLY")
    db_version = cur.fetchone()[0]
    cur.execute(
        "SELECT index_name, index_type, parameters FROM user_indexes WHERE index_name='IDX_MEMORIES_EMBED_HNSW'"
    )
    idx_row = cur.fetchone()

    finished = datetime.now(timezone.utc).isoformat()

    evidence = {
        "schema": "mnemos-oracle-ee-hnsw-bench/v1",
        "run_id": uuid.uuid4().hex[:12],
        "started_utc": started,
        "finished_utc": finished,
        "db_version": db_version,
        "dsn_redacted": f"oracle://{args.user}:<redacted>@{args.dsn}",
        "dim": DIM,
        "rows": args.rows,
        "queries_per_bench": args.queries,
        "k": K,
        "no_index": no_idx,
        "with_index": with_idx,
        "speedup_p50": no_idx["p50_ms"] / with_idx["p50_ms"] if with_idx["p50_ms"] else None,
        "speedup_p95": no_idx["p95_ms"] / with_idx["p95_ms"] if with_idx["p95_ms"] else None,
        "index_create_ms": idx_meta["create_ms"],
        "index_meta": {
            "name": idx_row[0] if idx_row else None,
            "type": idx_row[1] if idx_row else None,
            "parameters": str(idx_row[2]) if idx_row else None,
        },
        "python_oracledb": oracledb.__version__,
    }

    artifact = {
        "evidence": evidence,
        "hmac_key_id": sha256(HMAC_KEY).hexdigest()[:16],
        "hmac_sha256": _hmac(evidence),
    }

    out = REPO_ROOT / "docs" / "proof" / f"oracle-ee-hnsw-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"\nwrote {out}")
    print(f"speedup p50: {evidence['speedup_p50']:.2f}x  p95: {evidence['speedup_p95']:.2f}x")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
