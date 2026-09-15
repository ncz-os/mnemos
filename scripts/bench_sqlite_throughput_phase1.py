#!/usr/bin/env python3
"""SQLite persistence-layer throughput / latency bench — Phase 1.

MNEMOS v7 roadmap Feature 5/5: benchmark execution, Phase 1 ONLY.

Scope (Phase 1):

- corpus sizes: 1,000 and 10,000 memories
- concurrency levels: 1, 5, 10
- backend: SQLite only (``mnemos.persistence.sqlite.SqliteBackend`` and
  ``SqliteMemoryRepository.insert_memory``, line ~927)
- vectors: MOCK, not real embeddings. We generate deterministic random
  float vectors of the configured embedding dim, mirroring the
  ``random.Random(0)`` seeding pattern already used in
  ``scripts/embed_throughput_bench.py``.
- runtime budget: ~30 minutes for the full 2 x 3 = 6 matrix.

What this bench measures (and what it does NOT measure):

- The SqliteBackend serializes every insert through one
  ``asyncio.Lock`` plus a single ``BEGIN IMMEDIATE`` connection
  (see ``SqliteBackend.transactional`` at line ~7401 of
  ``mnemos/persistence/sqlite.py``). That means concurrency > 1 does
  NOT give us parallel SQLite writes — it tells us how many tasks
  queue up at the lock. The bench is honest about this: we report
  the per-task latency distribution and aggregate wall-time, and the
  report explicitly calls out the serialized path so a reader cannot
  misread the throughput plateau as a hardware limit.
- We do NOT call any inference endpoint; mock vectors are deterministic
  per corpus size, so the JSON artifact is bit-for-bit reproducible
  per (corpus_size, concurrency) pair on the same hardware.

Phase 2 (explicitly NOT in this script):
- 100k / 1M corpus, concurrency 25 / 50 / 100
- multi-instance / packing-density testing
- blocked on an operator hardware-allocation decision.

The CLI flags here do accept larger values (--corpus-sizes, --concurrency
take comma-lists) — that's the natural extension point for Phase 2
later — but no Phase 2 logic is implemented.

Output:
- docs/proof/bench-sqlite-phase1-{ts}.json   provenance-tagged artifact
- docs/proof/bench-sqlite-phase1-{ts}.md     summary table

Usage:
    python scripts/bench_sqlite_throughput_phase1.py \\
        --corpus-sizes 1000,10000 \\
        --concurrency 1,5,10 \\
        --output-dir docs/proof
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ── helpers ──────────────────────────────────────────────────────────────────


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        return "unknown"


def _parse_int_list(raw: str, name: str) -> list[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise SystemExit(f"FATAL: --{name} must contain at least one integer")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError as e:
            raise SystemExit(f"FATAL: --{name} contains non-integer token {p!r}") from e
        if out[-1] < 1:
            raise SystemExit(f"FATAL: --{name} values must be >= 1, got {out[-1]}")
    return out


def _mock_embedding(rng: random.Random, dim: int) -> list[float]:
    """Deterministic random float vector, dim-D. Seeded from caller."""
    return [float(rng.random()) for _ in range(dim)]


def _percentile(sorted_samples: list[float], pct: float) -> float | None:
    if not sorted_samples:
        return None
    n = len(sorted_samples)
    idx = max(0, min(n - 1, int(pct * (n - 1))))
    return round(sorted_samples[idx] * 1000.0, 3)  # seconds -> ms


def _summarize(samples: list[float]) -> dict[str, Any]:
    """Stats for a list of per-insert latencies in seconds."""
    if not samples:
        return {
            "n": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
        }
    s = sorted(samples)
    n = len(s)
    return {
        "n": n,
        "p50_ms": round(statistics.median(s) * 1000.0, 3),
        "p95_ms": _percentile(s, 0.95),
        "p99_ms": _percentile(s, 0.99),
        "min_ms": round(s[0] * 1000.0, 3),
        "max_ms": round(s[-1] * 1000.0, 3),
        "mean_ms": round(statistics.fmean(s) * 1000.0, 3),
    }


# ── core bench ───────────────────────────────────────────────────────────────


async def _run_one(
    corpus_size: int,
    concurrency: int,
    *,
    embedding_dim: int,
    warmup: int,
    readback_count: int,
) -> dict[str, Any]:
    """Run a single (corpus_size, concurrency) cell against a fresh temp DB.

    Returns a per-cell dict with all measured stats. The DB file lives in a
    temp directory that is cleaned up before this function returns, so we
    cannot pollute any real data path.
    """
    from mnemos.persistence.sqlite import SqliteBackend

    # Settings object: just enough to satisfy SqliteBackend's
    # `_resolve_embedding_dim()` and the `_require_dim()` invariant
    # on insert_memory. We don't pass any other settings — the bench
    # doesn't need the full MnemosSettings graph.
    settings = type("S", (), {})()
    settings.database = type("DB", (), {"embedding_dim": embedding_dim})()

    workdir = Path(tempfile.mkdtemp(prefix=f"mnemos-bench-sqlite-p1-{corpus_size}-{concurrency}-"))
    db_path = workdir / "bench.db"
    rid = uuid.uuid4().hex[:12]
    backend = SqliteBackend(db_path, settings)
    await backend.open()

    # Deterministic per-corpus seed: corpus_size itself is enough to keep
    # the mock vector stream stable across runs of the same cell, mirroring
    # the random.Random(0) seeding convention already in this repo.
    rng = random.Random(corpus_size)
    records: list[dict[str, Any]] = []
    for i in range(corpus_size):
        records.append(
            {
                "memory_id": f"b_{rid}_{i:08d}",
                "content": f"bench memory {i} for corpus={corpus_size}",
                "category": "bench",
                "subcategory": "phase1",
                "embedding": _mock_embedding(rng, embedding_dim),
            }
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    insert_kwargs = {
        "category": "bench",
        "subcategory": "phase1",
        "metadata_json": json.dumps({"bench": "phase1", "corpus_size": corpus_size, "concurrency": concurrency}),
        "quality_rating": 50,
        "owner_id": "bench",
        "namespace": "phase1",
        "permission_mode": 1,
        "source_model": "mock-bench",
        "source_provider": "mock-bench",
        "source_session": rid,
        "source_agent": "bench_sqlite_throughput_phase1",
        "verbatim_content": None,
        "created": now,
        "updated": now,
    }

    # Warmup: small synchronous insertion under one transaction to flush any
    # one-time open() / migration / sqlite-vec costs. Insert warmup rows with
    # one task so they don't skew the concurrency measurement. Cap at
    # corpus_size so a tiny cell doesn't try to warm up with more rows than
    # it actually has.
    warmup_n = min(warmup, corpus_size)
    if warmup_n > 0:
        warm_records = [
            {**r, "memory_id": f"warm_{rid}_{i:08d}", "content": f"warmup {i}"}
            for i, r in enumerate(records[:warmup_n])
        ]
        async with backend.transactional() as tx:
            for r in warm_records:
                await backend.memories.insert_memory(tx, memory_id=r["memory_id"], content=r["content"], **insert_kwargs, embedding=r["embedding"])

    queue: asyncio.Queue[int] = asyncio.Queue()
    for idx in range(corpus_size):
        queue.put_nowait(idx)

    latencies: list[float] = []
    latencies_lock = asyncio.Lock()
    started_at = time.perf_counter()
    started_perf = started_at

    async def worker(task_id: int) -> None:
        while True:
            try:
                rec_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                r = records[rec_idx]
                t0 = time.perf_counter()
                async with backend.transactional() as tx:
                    await backend.memories.insert_memory(
                        tx,
                        memory_id=r["memory_id"],
                        content=r["content"],
                        **insert_kwargs,
                        embedding=r["embedding"],
                    )
                dt = time.perf_counter() - t0
            finally:
                queue.task_done()
            async with latencies_lock:
                latencies.append(dt)

    workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]
    await asyncio.gather(*workers)
    insert_wall = time.perf_counter() - started_perf

    # Optional small read-back pass: how long does a single gather_stats on
    # the freshly-loaded corpus take? This isn't the headline metric for
    # Phase 1 — the brief is insert throughput/latency — but it's cheap and
    # surfaces pathological cases (e.g. missing index) without costing the
    # 30-minute budget.
    readback_wall = 0.0
    readback_count_actual = 0
    if readback_count > 0:
        # gather_stats is the cheapest aggregate; it returns total/federated/etc.
        t0 = time.perf_counter()
        async with backend.transactional() as tx:
            stats = await backend.memories.gather_stats(tx)
        readback_wall = time.perf_counter() - t0
        readback_count_actual = int(stats.total_memories)

    total_wall = time.perf_counter() - started_at

    throughput_ops_s = round(corpus_size / insert_wall, 2) if insert_wall > 0 else None

    await backend.close()

    # Clean up the temp DB and its directory before returning so we cannot
    # pollute any real data path. Best-effort: a leftover temp dir is fine.
    try:
        db_path.unlink(missing_ok=True)
        workdir.rmdir()
    except OSError:
        pass

    summary = _summarize(latencies)
    return {
        "corpus_size": corpus_size,
        "concurrency": concurrency,
        "wall_seconds": round(total_wall, 4),
        "insert_wall_seconds": round(insert_wall, 4),
        "readback_wall_seconds": round(readback_wall, 4),
        "readback_total_memories": readback_count_actual,
        "throughput_ops_per_sec": throughput_ops_s,
        "latency": summary,
        "embedding_dim": embedding_dim,
        "warmup_inserts": warmup,
        "schema_note": (
            "SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE "
            "connection, so all writes serialize through one path. Concurrency > 1 measures "
            "how many tasks queue at the lock, not parallel SQLite writes."
        ),
    }


# ── artifact writers ─────────────────────────────────────────────────────────


def _write_json_artifact(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stable key ordering + JSON pretty-print, no HMAC here — Phase 1 is a
    # benchmark, not a proof artifact (cf. sqlite_proof_run.py which signs).
    # git SHA + python version + per-cell git provenance are sufficient for
    # reproducibility audit; the docs/proof/bench-four-*.json convention also
    # ships unsigned.
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    path.write_text(body + "\n", encoding="utf-8")


def _render_markdown(payload: dict[str, Any]) -> str:
    cells = payload["cells"]
    lines: list[str] = []
    lines.append("# MNEMOS SQLite throughput bench — Phase 1")
    lines.append("")
    lines.append(f"Run at: `{payload['run_utc']}`  ")
    lines.append(f"Git SHA: `{payload['git_sha']}`  ")
    lines.append(f"Python: `{payload['python_version']}`  ")
    lines.append(f"Platform: `{payload['platform']}`  ")
    lines.append(f"Backend: `{payload['backend']}` (mock vectors, embedding_dim={payload['embedding_dim']})  ")
    lines.append(f"Schema version: `{payload['schema']}`")
    lines.append("")
    lines.append("## Results — corpus_size x concurrency -> throughput + p50/p95/p99")
    lines.append("")
    lines.append(
        "| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cell in cells:
        lat = cell["latency"]
        lines.append(
            f"| {cell['corpus_size']} | {cell['concurrency']} | "
            f"{cell['throughput_ops_per_sec']} | "
            f"{lat['p50_ms']} | {lat['p95_ms']} | {lat['p99_ms']} | {lat['mean_ms']} | "
            f"{cell['insert_wall_seconds']} | {cell['wall_seconds']} |"
        )
    lines.append("")
    lines.append("## Per-cell schema note (repeated for offline readers)")
    lines.append("")
    for cell in cells:
        lines.append(
            f"- **corpus={cell['corpus_size']} concurrency={cell['concurrency']}** — {cell['schema_note']}"
        )
    lines.append("")
    lines.append("## Phase 2 (NOT done)")
    lines.append("")
    lines.append(
        "Phase 1 stops at corpus sizes {1k, 10k} x concurrency {1, 5, 10}. Phase 2 "
        "would extend to 100k / 1M corpus and concurrency 25 / 50 / 100, plus "
        "multi-instance packing-density testing. Blocked on an operator "
        "hardware-allocation decision. The CLI flags on this script "
        "(`--corpus-sizes`, `--concurrency`) accept larger values, so the natural "
        "extension point is to widen the matrix when Phase 2 is unblocked — no "
        "script rewrite needed."
    )
    lines.append("")
    return "\n".join(lines)


# ── main ─────────────────────────────────────────────────────────────────────


async def _async_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = (REPO / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"bench-sqlite-phase1-{ts}.json"
    md_path = out_dir / f"bench-sqlite-phase1-{ts}.md"

    print(f"[bench-sqlite-phase1] output_dir={out_dir}  ts={ts}")
    print(
        f"[bench-sqlite-phase1] matrix: corpus_sizes={args.corpus_sizes}  concurrency={args.concurrency}  "
        f"embedding_dim={args.embedding_dim}  warmup={args.warmup}"
    )

    cells: list[dict[str, Any]] = []
    total_started = time.perf_counter()
    for corpus_size in args.corpus_sizes:
        for concurrency in args.concurrency:
            cell_started = time.perf_counter()
            cell = await _run_one(
                corpus_size,
                concurrency,
                embedding_dim=args.embedding_dim,
                warmup=args.warmup,
                readback_count=args.readback_count,
            )
            cell_wall = time.perf_counter() - cell_started
            cell["phase1_cell_wall_seconds"] = round(cell_wall, 4)
            cells.append(cell)
            print(
                f"[bench-sqlite-phase1] corpus={corpus_size} concurrency={concurrency} "
                f"throughput={cell['throughput_ops_per_sec']}ops/s "
                f"p50={cell['latency']['p50_ms']}ms p95={cell['latency']['p95_ms']}ms "
                f"wall={cell['wall_seconds']}s"
            )
    total_wall = time.perf_counter() - total_started

    payload: dict[str, Any] = {
        "schema": "mnemos-sqlite-bench-phase1/v1",
        "run_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "python_version": sys.version,
        "platform": platform.platform(),
        "backend": "sqlite",
        "embedding_dim": args.embedding_dim,
        "warmup_inserts": args.warmup,
        "cells": cells,
        "total_wall_seconds": round(total_wall, 4),
        "phase": 1,
        "phase2_status": "blocked on operator hardware-allocation decision",
        "note": (
            "Mock vectors are deterministic per corpus_size (seeded random.Random(corpus_size)). "
            "Phase 1 only; Phase 2 (100k/1M corpus, concurrency 25/50/100, multi-instance) is out of scope."
        ),
    }

    _write_json_artifact(payload, json_path)
    md_path.write_text(_render_markdown(payload) + "\n", encoding="utf-8")

    print(f"[bench-sqlite-phase1] wrote {json_path}")
    print(f"[bench-sqlite-phase1] wrote {md_path}")
    print(f"[bench-sqlite-phase1] total wall: {total_wall:.2f}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="SQLite persistence-layer throughput bench — Phase 1 (v7 roadmap Feature 5/5).",
    )
    p.add_argument(
        "--corpus-sizes",
        default="1000,10000",
        help="Comma-separated corpus sizes. Phase 1 default: 1000,10000.",
    )
    p.add_argument(
        "--concurrency",
        default="1,5,10",
        help="Comma-separated concurrency levels. Phase 1 default: 1,5,10.",
    )
    p.add_argument(
        "--embedding-dim",
        type=int,
        default=768,
        help="Embedding dimension used for mock vectors (default 768 — matches MNEMOS default).",
    )
    p.add_argument(
        "--warmup",
        type=int,
        default=64,
        help="Number of warmup inserts before timed measurement (default 64).",
    )
    p.add_argument(
        "--readback-count",
        type=int,
        default=1,
        help="If >0, run one gather_stats read-back per cell to surface pathological cases (default 1 — on for Phase 1).",
    )
    p.add_argument(
        "--output-dir",
        default="docs/proof",
        help="Output directory for the JSON+md artifacts (default docs/proof).",
    )
    args = p.parse_args()

    args.corpus_sizes = _parse_int_list(args.corpus_sizes, "corpus-sizes")
    args.concurrency = _parse_int_list(args.concurrency, "concurrency")
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
