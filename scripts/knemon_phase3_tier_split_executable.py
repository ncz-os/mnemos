#!/usr/bin/env python3
"""KNEMON Phase 3 — Tier-split executable (gate cleared).

Reads the Phase 1 baseline table ``mnemos.knemon_phase1_baseline_2026_05_28``
from ORCLPDB1, computes B1/B2/C1/C2 tier assignments from throughput
(events/day) × latency (p95 ms), and writes ``mnemos.knemon_tier_assignments``.

Tier definitions:
  B1 = high-throughput-fast   (events/day > 1000, p95 < 5000 ms)
  B2 = high-throughput-slow   (events/day > 1000, p95 >= 5000 ms)
  C1 = low-throughput-fast    (events/day <= 1000, p95 < 5000 ms)
  C2 = low-throughput-slow    (events/day <= 1000, p95 >= 5000 ms)

Directive 7 — Iterate-in-place:
  After the initial threshold-based assignment, the script iterates:
    1. Recompute per-tier centroids (median events/day, median p95).
    2. Reassign each task_kind to the nearest centroid tier.
    3. Repeat until no row changes tier (convergence) or max 20 iterations.

Usage::

    .venv/bin/python scripts/knemon_phase3_tier_split_executable.py \\
        --dsn oracle://mnemos:mnemos_dev@192.168.207.25:1521/ORCLPDB1

Or set ORACLE_PROOF_DSN in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get(
    "ORACLE_PROOF_DSN",
    "oracle://mnemos:mnemos_dev@192.168.207.25:1521/ORCLPDB1",
)

# ── Tier boundaries (directive 7 initial split) ──────────────────────────
THROUGHPUT_THRESHOLD = 1000.0   # events/day: > 1000 = high (B), <= 1000 = low (C)
LATENCY_THRESHOLD_MS = 5000.0   # p95 ms:    < 5000 = fast (1), >= 5000 = slow (2)
MAX_ITERATIONS = 20
CONVERGENCE_EPSILON = 0.001     # fraction of rows that may still shift


def _assign_tier(events_per_day: float, p95_latency_ms: float) -> str:
    """Assign tier label from two continuous dimensions."""
    if events_per_day > THROUGHPUT_THRESHOLD:
        prefix = "B"
    else:
        prefix = "C"
    if p95_latency_ms < LATENCY_THRESHOLD_MS:
        suffix = "1"
    else:
        suffix = "2"
    return prefix + suffix


def _tier_centroid(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Median events/day and median p95 for a cluster of rows."""
    if not rows:
        return {"events_per_day": 0.0, "p95_latency_ms": 0.0}
    eps = sorted(r["events_per_day"] for r in rows)
    lats = sorted(r["p95_latency_ms"] for r in rows)
    n = len(eps)
    return {
        "events_per_day": eps[n // 2],
        "p95_latency_ms": lats[n // 2],
    }


def _nearest_tier(
    events_per_day: float,
    p95_latency_ms: float,
    centroids: dict[str, dict[str, float]],
) -> str:
    """Return the tier label whose centroid is nearest in log-scaled space."""
    best_tier = "C2"
    best_dist = float("inf")
    # log-scale to make events/day and latency comparable
    import math

    le = math.log(max(events_per_day, 1.0))
    ll = math.log(max(p95_latency_ms, 1.0))
    for tier, c in centroids.items():
        ce = math.log(max(c["events_per_day"], 1.0))
        cl = math.log(max(c["p95_latency_ms"], 1.0))
        d = (le - ce) ** 2 + (ll - cl) ** 2
        if d < best_dist:
            best_dist = d
            best_tier = tier
    return best_tier


async def _run(dsn: str) -> int:
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()

            # ── 0. Ensure target table exists ──────────────────────────
            migration_sql = (
                REPO_ROOT
                / "mnemos"
                / "db_migrations"
                / "migrations_oracle"
                / "0041_knemon_tier_assignments.sql"
            )
            if migration_sql.exists():
                print(f"[phase3] Applying migration {migration_sql.name} …")
                _apply_migration(cur, migration_sql.read_text())
                await conn.commit()
            else:
                print("[phase3] WARNING: migration 0041 not found; table may not exist")

            # ── 1. Read baseline ───────────────────────────────────────
            print("[phase3] Reading knemon_phase1_baseline_2026_05_28 …")
            try:
                await cur.execute(
                    "SELECT * FROM mnemos.knemon_phase1_baseline_2026_05_28"
                )
            except Exception:
                # Try without schema prefix
                await cur.execute(
                    "SELECT * FROM knemon_phase1_baseline_2026_05_28"
                )

            col_info = [(d[0].lower(), i) for i, d in enumerate(cur.description)]
            col_names = [c[0] for c in col_info]
            print(f"[phase3]   columns: {col_names}")

            baseline_rows: list[dict[str, Any]] = []
            async for row in cur:
                d = {name: row[idx] for name, idx in col_info}
                baseline_rows.append(d)

            if not baseline_rows:
                print("[phase3] ERROR: baseline table is empty", file=sys.stderr)
                return 1

            print(
                f"[phase3]   {len(baseline_rows)} rows loaded "
                f"({len(set(r.get('task_kind','') for r in baseline_rows))} unique task_kinds)"
            )

            # ── 2. Normalise columns ───────────────────────────────────
            # The baseline may use different column names; map to canonical.
            def _col(row: dict, *candidates: str) -> float:
                for c in candidates:
                    v = row.get(c) or row.get(c.upper())
                    if v is not None:
                        return float(v)
                return 0.0

            def _str_col(row: dict, *candidates: str) -> str:
                for c in candidates:
                    v = row.get(c) or row.get(c.upper())
                    if v is not None:
                        return str(v)
                return ""

            parsed: list[dict[str, Any]] = []
            for r in baseline_rows:
                task_kind = _str_col(r, "task_kind", "kind", "task_kind_name")
                if not task_kind:
                    continue
                events_total = int(
                    _col(r, "events_total", "total_events", "event_count", "events")
                )
                sessions_total = int(
                    _col(r, "sessions_total", "total_sessions", "session_count", "sessions")
                )
                events_per_day = _col(
                    r,
                    "events_per_day",
                    "avg_events_per_day",
                    "daily_events",
                )
                p95_latency_ms = _col(
                    r,
                    "p95_latency_ms",
                    "p95_latency",
                    "latency_p95_ms",
                    "p95_ms",
                )
                avg_latency_ms = _col(
                    r,
                    "avg_latency_ms",
                    "avg_latency",
                    "latency_avg_ms",
                    "avg_ms",
                )

                # If events_per_day not precomputed, derive from total
                if events_per_day == 0 and events_total > 0:
                    # Assume baseline covers ~28 days (May 2026 snap)
                    events_per_day = events_total / 28.0

                if p95_latency_ms == 0:
                    # Try avg as fallback with 1.5x multiplier heuristic
                    p95_latency_ms = avg_latency_ms * 1.5

                parsed.append(
                    {
                        "task_kind": task_kind,
                        "events_total": events_total,
                        "sessions_total": sessions_total,
                        "events_per_day": events_per_day,
                        "p95_latency_ms": p95_latency_ms,
                        "avg_latency_ms": avg_latency_ms,
                    }
                )

            print(f"[phase3]   {len(parsed)} task_kinds after column normalisation")

            # ── 3. Initial tier assignment (thresholds) ─────────────────
            for p in parsed:
                p["tier"] = _assign_tier(p["events_per_day"], p["p95_latency_ms"])

            # ── 4. Truncate target and write initial assignments ───────
            print("[phase3] Writing initial tier assignments …")
            await cur.execute("DELETE FROM mnemos.knemon_tier_assignments")
            for p in parsed:
                await cur.execute(
                    """
                    INSERT INTO mnemos.knemon_tier_assignments
                      (task_kind, tier, events_total, sessions_total,
                       events_per_day, p95_latency_ms, avg_latency_ms, iteration)
                    VALUES (:1, :2, :3, :4, :5, :6, :7, 0)
                    """,
                    (
                        p["task_kind"],
                        p["tier"],
                        p["events_total"],
                        p["sessions_total"],
                        p["events_per_day"],
                        p["p95_latency_ms"],
                        p["avg_latency_ms"],
                    ),
                )
            await conn.commit()
            print(f"[phase3]   {len(parsed)} initial rows written (iteration 0)")

            # ── 5. Iterate-in-place (directive 7) ──────────────────────
            print("[phase3] Starting iterate-in-place (directive 7) …")
            for iteration in range(1, MAX_ITERATIONS + 1):
                # 5a. Read current state
                await cur.execute(
                    """
                    SELECT task_kind, tier, events_per_day, p95_latency_ms
                    FROM mnemos.knemon_tier_assignments
                    ORDER BY task_kind
                    """
                )
                current: dict[str, dict[str, Any]] = {}
                async for row in cur:
                    current[row[0]] = {
                        "tier": row[1],
                        "events_per_day": float(row[2] or 0),
                        "p95_latency_ms": float(row[3] or 0),
                    }

                # 5b. Group by tier and compute centroids
                tier_rows: dict[str, list[dict[str, Any]]] = {
                    "B1": [], "B2": [], "C1": [], "C2": []
                }
                for tk, d in current.items():
                    tier_rows[d["tier"]].append(
                        {
                            "task_kind": tk,
                            "events_per_day": d["events_per_day"],
                            "p95_latency_ms": d["p95_latency_ms"],
                        }
                    )

                centroids = {
                    tier: _tier_centroid(rows) for tier, rows in tier_rows.items()
                }

                # 5c. Reassign each task_kind to nearest centroid tier
                changes = 0
                for tk, d in current.items():
                    new_tier = _nearest_tier(
                        d["events_per_day"], d["p95_latency_ms"], centroids
                    )
                    if new_tier != d["tier"]:
                        await cur.execute(
                            """
                            UPDATE mnemos.knemon_tier_assignments
                            SET tier = :1,
                                iteration = :2,
                                last_updated = SYSTIMESTAMP
                            WHERE task_kind = :3
                            """,
                            (new_tier, iteration, tk),
                        )
                        changes += 1

                await conn.commit()

                # Print tier distribution
                dist = {t: 0 for t in ["B1", "B2", "C1", "C2"]}
                for d in current.values():
                    dist[d["tier"]] = dist.get(d["tier"], 0) + 1

                print(
                    f"[phase3]   iteration {iteration:2d}: "
                    f"changes={changes:4d}  "
                    f"B1={dist['B1']:4d} B2={dist['B2']:4d} "
                    f"C1={dist['C1']:4d} C2={dist['C2']:4d}"
                )

                if changes == 0:
                    print(f"[phase3] Converged at iteration {iteration}")
                    break
            else:
                print(
                    f"[phase3] Reached max iterations ({MAX_ITERATIONS}) "
                    f"without full convergence"
                )

            # ── 6. Final summary ───────────────────────────────────────
            await cur.execute(
                """
                SELECT tier, COUNT(*), SUM(events_total), SUM(sessions_total)
                FROM mnemos.knemon_tier_assignments
                GROUP BY tier
                ORDER BY tier
                """
            )
            print("[phase3] Final tier distribution:")
            total_tk = 0
            total_ev = 0
            async for row in cur:
                tier, cnt, ev, sess = row
                total_tk += cnt
                total_ev += int(ev or 0)
                print(f"  {tier}: {cnt:5d} task_kinds  {int(ev or 0):9d} events  {int(sess or 0):5d} sessions")
            print(f"  TOTAL: {total_tk} task_kinds  {total_ev} events")

            cur.close()

    finally:
        await pool.close()

    return 0


def _apply_migration(cur: Any, sql: str) -> None:
    """Apply a sqlplus-format migration inline (PL/SQL with / terminators)."""
    stmts: list[str] = []
    buf: list[str] = []
    in_plsql = False
    for raw in sql.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            buf.append(line)
            continue
        if stripped.upper().startswith(
            (
                "DECLARE",
                "BEGIN",
                "CREATE OR REPLACE PROCEDURE",
                "CREATE OR REPLACE FUNCTION",
                "CREATE OR REPLACE PACKAGE",
            )
        ):
            in_plsql = True
        if stripped == "/" and in_plsql:
            block = "\n".join(buf).strip()
            if block:
                stmts.append(block)
            buf = []
            in_plsql = False
            continue
        buf.append(line)
        if stripped.endswith(";") and not in_plsql:
            joined = "\n".join(buf).strip()
            if joined.endswith(";"):
                joined = joined[:-1]
            if joined.strip():
                stmts.append(joined)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)

    for i, stmt in enumerate(stmts, 1):
        head = stmt.splitlines()[0].strip()[:80]
        try:
            cur.execute(stmt)
            print(f"  [{i:2d}] OK  {head}")
        except Exception as e:
            msg = str(e).splitlines()[0]
            benign = any(
                code in msg
                for code in ("ORA-00955", "ORA-02275", "ORA-01430", "ORA-04081")
            )
            tag = "skip" if benign else "FAIL"
            print(f"  [{i:2d}] {tag} {head} -- {msg}")
            if not benign:
                raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    args = ap.parse_args()
    print(f"[phase3] Connecting to Oracle via {args.dsn.split('@')[-1] if '@' in args.dsn else args.dsn}")
    return asyncio.run(_run(args.dsn))


if __name__ == "__main__":
    sys.exit(main())
