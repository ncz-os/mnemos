#!/usr/bin/env python3
"""KNEMON Phase 1 — 48h ledger baseline generator (PREREQ gate).

Generates the event-level baseline snapshot ``mnemos.knemon_phase1_baseline_2026_05_28``
by querying ``usage_ledger`` on PYTHIA ORCLPDB1 for the 48h window
2026-05-26T00:00Z .. 2026-05-28T00:00Z.

Output:
  * Oracle table  ``mnemos.knemon_phase1_baseline_2026_05_28``
    columns: event_id, session_urn, plan_window_id, task_kind,
             provider, model, tokens_in, tokens_out, cost_usd, ts_utc
  * Parquet file  ``data/knemon_phase1_baseline_2026_05_28.parquet``
  * Registry row  ``mnemos.knemon_baselines``

This is the PREREQ for 4 gated jobs:
  019e6b4b       phase3-tier-split
  019e6b4b-6a70  phase4-ab-test
  019e6b0e-d000  post-knemon-1
  019e6b0e-d02e  post-knemon-2

Directive 7 — Iterate-in-place:
  Idempotent: re-running truncates and reloads the baseline table.
  Paginated scan: if the result set exceeds 100K rows the script
  paginates internally to avoid client-side memory pressure.

Usage::

    .venv/bin/python scripts/knemon_phase1_baseline_generate.py \\
        --dsn oracle://mnemos:mnemos_dev@192.168.207.67:1521/ORCLPDB1

Or set ORACLE_PROOF_DSN in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get(
    "ORACLE_PROOF_DSN",
    "oracle://mnemos:mnemos_dev@192.168.207.67:1521/ORCLPDB1",
)

WINDOW_START = datetime(2026, 5, 26, 0, 0, 0, tzinfo=timezone.utc)
WINDOW_END   = datetime(2026, 5, 28, 0, 0, 0, tzinfo=timezone.utc)
BASELINE_NAME = "knemon_phase1_baseline_2026_05_28"
TABLE_NAME    = BASELINE_NAME
PAGE_SIZE     = 50_000   # rows per fetch iteration


def _parsed_dsn(raw: str) -> str:
    """Return a display-safe DSN fragment (host:port/service)."""
    if "@" in raw:
        return raw.split("@")[-1]
    return raw


async def _apply_migration(cur, migration_path: Path) -> None:
    """Apply a sqlplus-format migration inline (PL/SQL with / terminators)."""
    sql = migration_path.read_text()
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
            await cur.execute(stmt)
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


async def _export_parquet(rows: list[dict], out_path: Path) -> None:
    """Export rows to a Parquet file using pyarrow (if available)."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("[phase1] WARNING: pyarrow not installed; skipping parquet export")
        # Fallback: write JSON lines
        import json
        json_path = out_path.with_suffix(".jsonl")
        with open(json_path, "w") as f:
            for r in rows:
                # Convert datetime to ISO string for JSON
                r_copy = dict(r)
                if "ts_utc" in r_copy and hasattr(r_copy["ts_utc"], "isoformat"):
                    r_copy["ts_utc"] = r_copy["ts_utc"].isoformat()
                f.write(json.dumps(r_copy, default=str) + "\n")
        print(f"[phase1]   JSON fallback written: {json_path} ({len(rows)} rows)")
        return

    # Build pyarrow schema
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([
        ("event_id",        pa.int64()),
        ("session_urn",     pa.string()),
        ("plan_window_id",  pa.string()),
        ("task_kind",       pa.string()),
        ("provider",        pa.string()),
        ("model",           pa.string()),
        ("tokens_in",       pa.int64()),
        ("tokens_out",      pa.int64()),
        ("cost_usd",        pa.float64()),
        ("ts_utc",          pa.timestamp("us", tz="UTC")),
    ])

    arrays = {
        "event_id":        [r["event_id"] for r in rows],
        "session_urn":     [r.get("session_urn") or "" for r in rows],
        "plan_window_id":  [r.get("plan_window_id") or "" for r in rows],
        "task_kind":       [r["task_kind"] for r in rows],
        "provider":        [r["provider"] for r in rows],
        "model":           [r["model"] for r in rows],
        "tokens_in":       [int(r["tokens_in"]) for r in rows],
        "tokens_out":      [int(r["tokens_out"]) for r in rows],
        "cost_usd":        [float(r["cost_usd"]) for r in rows],
        "ts_utc":          [r["ts_utc"] for r in rows],
    }

    table = pa.table(arrays, schema=schema)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(out_path), compression="zstd")
    print(f"[phase1]   Parquet written: {out_path} ({len(rows)} rows, {out_path.stat().st_size} bytes)")


async def _run(dsn: str) -> int:
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()

            # ── 0. Ensure target tables exist ───────────────────────────
            migration_sql = (
                REPO_ROOT
                / "mnemos"
                / "db_migrations"
                / "migrations_oracle"
                / "0042_knemon_baseline_tables.sql"
            )
            if migration_sql.exists():
                print(f"[phase1] Applying migration {migration_sql.name} …")
                await _apply_migration(cur, migration_sql)
                await conn.commit()
            else:
                print("[phase1] WARNING: migration 0042 not found; tables may not exist")

            # ── 1. Count source rows ────────────────────────────────────
            print(f"[phase1] Counting usage_ledger rows in window "
                  f"{WINDOW_START.isoformat()} .. {WINDOW_END.isoformat()} …")
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM usage_ledger
                WHERE ts >= :start_ts AND ts < :end_ts
                """,
                {"start_ts": WINDOW_START, "end_ts": WINDOW_END},
            )
            (total_rows,) = await cur.fetchone()
            print(f"[phase1]   {total_rows} rows in window")

            if total_rows == 0:
                print("[phase1] ERROR: zero rows in window — nothing to snapshot",
                      file=sys.stderr)
                return 1

            # ── 2. Truncate target (idempotent re-run) ──────────────────
            print(f"[phase1] Truncating {TABLE_NAME} …")
            try:
                await cur.execute(f"TRUNCATE TABLE {TABLE_NAME}")
            except Exception:
                await cur.execute(f"DELETE FROM {TABLE_NAME}")
            await conn.commit()

            # ── 3. Paginated INSERT … SELECT ────────────────────────────
            print(f"[phase1] Streaming INSERT … SELECT (page_size={PAGE_SIZE}) …")
            offset = 0
            inserted = 0
            while offset < total_rows:
                await cur.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                        (event_id, session_urn, plan_window_id, task_kind,
                         provider, model, tokens_in, tokens_out, cost_usd, ts_utc)
                    SELECT id, session_id, plan_window_id, task_kind,
                           provider, model, tokens_in, tokens_out, est_cost_usd, ts
                    FROM (
                        SELECT id, session_id, plan_window_id, task_kind,
                               provider, model, tokens_in, tokens_out,
                               est_cost_usd, ts
                        FROM usage_ledger
                        WHERE ts >= :start_ts AND ts < :end_ts
                        ORDER BY id
                        OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
                    )
                    """,
                    {
                        "start_ts": WINDOW_START,
                        "end_ts": WINDOW_END,
                        "offset": offset,
                        "page_size": PAGE_SIZE,
                    },
                )
                page_inserted = cur.rowcount
                inserted += page_inserted
                offset += PAGE_SIZE
                if page_inserted > 0:
                    print(f"[phase1]   … {inserted:,} / {total_rows:,} rows inserted")
                await conn.commit()

            print(f"[phase1]   Total inserted: {inserted:,} rows")

            # ── 4. Compute aggregate stats ──────────────────────────────
            await cur.execute(
                f"""
                SELECT COUNT(DISTINCT session_urn),
                       COUNT(DISTINCT task_kind)
                FROM {TABLE_NAME}
                """
            )
            session_count, task_kind_count = await cur.fetchone()
            print(f"[phase1]   Distinct sessions: {session_count}")
            print(f"[phase1]   Distinct task_kinds: {task_kind_count}")

            # ── 5. Register snapshot in knemon_baselines ─────────────────
            print("[phase1] Registering snapshot in knemon_baselines …")
            # Upsert: delete existing row for this baseline name, then insert
            await cur.execute(
                """
                DELETE FROM knemon_baselines WHERE baseline_name = :name
                """,
                {"name": BASELINE_NAME},
            )
            await cur.execute(
                """
                INSERT INTO knemon_baselines
                    (baseline_name, table_name, window_start, window_end,
                     event_count, session_count, task_kind_count, source_table, notes)
                VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9)
                """,
                (
                    BASELINE_NAME,
                    TABLE_NAME,
                    WINDOW_START,
                    WINDOW_END,
                    inserted,
                    session_count,
                    task_kind_count,
                    "usage_ledger",
                    "Phase 1 48h baseline: plan-status + model-routing, "
                    f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}",
                ),
            )
            await conn.commit()
            print(f"[phase1]   Registered: {BASELINE_NAME} "
                  f"({inserted} events / {session_count} sessions / {task_kind_count} task_kinds)")

            # ── 6. Export parquet (directive 7 — iterate-in-place) ──────
            parquet_path = REPO_ROOT / "data" / f"{BASELINE_NAME}.parquet"
            print(f"[phase1] Exporting parquet → {parquet_path} …")

            # Re-read from the snapshot table for a clean export
            await cur.execute(
                f"""
                SELECT event_id, session_urn, plan_window_id, task_kind,
                       provider, model, tokens_in, tokens_out, cost_usd, ts_utc
                FROM {TABLE_NAME}
                ORDER BY event_id
                """
            )
            col_info = [(d[0].lower(), i) for i, d in enumerate(cur.description)]
            export_rows: list[dict] = []
            async for row in cur:
                export_rows.append({name: row[idx] for name, idx in col_info})
            await _export_parquet(export_rows, parquet_path)

            cur.close()

    finally:
        await pool.close()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="Oracle DSN (or set ORACLE_PROOF_DSN)")
    args = ap.parse_args()
    print(f"[phase1] KNEMON Phase 1 — 48h ledger baseline generator")
    print(f"[phase1] Connecting to Oracle via {_parsed_dsn(args.dsn)}")
    print(f"[phase1] Window: {WINDOW_START.isoformat()} .. {WINDOW_END.isoformat()}")
    return asyncio.run(_run(args.dsn))


if __name__ == "__main__":
    sys.exit(main())
