"""MORPHEUS run orchestrator.

Creates a morpheus_runs row, walks through the configured phases, and
commits status + counters as it goes. Each phase is a separate async
function; the runner tags every memory mutation with morpheus_run_id so
rollback is a single DELETE.

v1 slice 1: phases are stubbed with TODO markers; the run row is
real, the audit trail is real, the API can list/inspect runs and roll
them back. Slice 2 fills in the actual REPLAY/CLUSTER/SYNTHESISE work.
"""
from __future__ import annotations

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


async def begin_run(
    pool: asyncpg.Pool,
    *,
    triggered_by: str = "cron",
    window_hours: int = 168,
    cluster_min_size: int = 3,
    config: Optional[dict] = None,
) -> str:
    """Open a new MORPHEUS run row and return its UUID as a string.

    Caller is responsible for advancing the row through phases via
    set_phase() and finalising via finish_run() (or fail_run() on
    exception). The row is created with status='running' so an inspector
    polling /v1/morpheus/runs sees the dream in flight.
    """
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=window_hours)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO morpheus_runs
                (triggered_by, window_started_at, window_ended_at,
                 window_hours, cluster_min_size, config)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            triggered_by, window_start, window_end,
            window_hours, cluster_min_size,
            json.dumps(config or {}),
        )
    run_id = str(row["id"])
    logger.info(
        "[MORPHEUS] run %s opened (window=%dh, triggered_by=%s)",
        run_id, window_hours, triggered_by,
    )
    return run_id


async def set_phase(pool: asyncpg.Pool, run_id: str, phase: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET phase=$2 WHERE id=$1::uuid",
            run_id, phase,
        )
    logger.info("[MORPHEUS] run %s → phase=%s", run_id, phase)


async def update_counters(
    pool: asyncpg.Pool,
    run_id: str,
    *,
    memories_scanned: Optional[int] = None,
    clusters_found: Optional[int] = None,
    summaries_created: Optional[int] = None,
) -> None:
    """Bump counters as phases finish. Pass only the fields to update."""
    sets: list[str] = []
    args: list = []
    if memories_scanned is not None:
        args.append(memories_scanned)
        sets.append(f"memories_scanned=${len(args)}")
    if clusters_found is not None:
        args.append(clusters_found)
        sets.append(f"clusters_found=${len(args)}")
    if summaries_created is not None:
        args.append(summaries_created)
        sets.append(f"summaries_created=${len(args)}")
    if not sets:
        return
    args.append(run_id)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE morpheus_runs SET {', '.join(sets)} "
            f"WHERE id=${len(args)}::uuid",
            *args,
        )


async def finish_run(pool: asyncpg.Pool, run_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET status='success', finished_at=now() "
            "WHERE id=$1::uuid",
            run_id,
        )
    logger.info("[MORPHEUS] run %s finished SUCCESS", run_id)


async def fail_run(pool: asyncpg.Pool, run_id: str, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE morpheus_runs SET status='failed', finished_at=now(), error=$2 "
            "WHERE id=$1::uuid",
            run_id, error[:4000],
        )
    logger.warning("[MORPHEUS] run %s finished FAILED: %s", run_id, error[:200])


async def rollback_run(pool: asyncpg.Pool, run_id: str) -> Tuple[int, int]:
    """Delete every memory tagged with this run + mark the run rolled_back.

    Returns (memories_deleted, run_rows_updated).

    v1 only inserts memories (append-only synthesis), so DELETE is the
    full undo. v2 mutation paths will also need to restore
    consolidated_into pointers and undo archive moves — those will
    extend this function. For now: simple, safe, deterministic.
    """
    try:
        UUID(run_id)
    except (ValueError, TypeError):
        raise ValueError(f"invalid run_id: {run_id!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Per-row tagging means rollback never crosses runs.
            del_result = await conn.execute(
                "DELETE FROM memories WHERE morpheus_run_id=$1::uuid",
                run_id,
            )
            # asyncpg returns "DELETE <n>"; parse the count.
            try:
                n_deleted = int(del_result.rsplit(" ", 1)[-1])
            except ValueError:
                n_deleted = 0
            run_result = await conn.execute(
                "UPDATE morpheus_runs "
                "SET status='rolled_back', finished_at=COALESCE(finished_at, now()) "
                "WHERE id=$1::uuid",
                run_id,
            )
            try:
                n_run = int(run_result.rsplit(" ", 1)[-1])
            except ValueError:
                n_run = 0
    logger.warning(
        "[MORPHEUS] run %s rolled back: %d memories deleted",
        run_id, n_deleted,
    )
    return n_deleted, n_run


# ── Phase stubs (slice 2 fills these in) ──────────────────────────────────────
#
# The phase functions below produce no side effects yet. Slice 2 wires up
# the actual REPLAY → CLUSTER → SYNTHESISE → COMMIT pipeline. They are
# defined here so the runner shape and the rollback contract are real
# from day one — the API can already trigger a "no-op dream" and undo
# it, which is the foundation we want before touching synthesis logic.

async def phase_replay(pool: asyncpg.Pool, run_id: str) -> int:
    """Scan memories from the run's window. Returns count scanned."""
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM memories m
            JOIN morpheus_runs r ON r.id = $1::uuid
            WHERE m.created BETWEEN r.window_started_at AND r.window_ended_at
              AND m.provenance IS DISTINCT FROM 'morpheus_local'
              AND m.morpheus_run_id IS NULL
            """,
            run_id,
        )
    await update_counters(pool, run_id, memories_scanned=int(n or 0))
    return int(n or 0)


async def phase_cluster(pool: asyncpg.Pool, run_id: str) -> int:
    """Cosine-cluster the replayed memories. Returns cluster count.

    SLICE 2: pgvector cosine over `embedding`, group with a similarity
    threshold (default 0.78). For now returns 0 — clustering is the
    first slice 2 deliverable since it has no mutation cost.
    """
    await update_counters(pool, run_id, clusters_found=0)
    return 0


async def phase_synthesise(pool: asyncpg.Pool, run_id: str) -> int:
    """Generate summary memories per cluster via LLM. Returns count created.

    SLICE 2: per-cluster LLM call, INSERT INTO memories with
    provenance='morpheus_local', morpheus_run_id=<run>,
    source_memories=<original ids>. Stub returns 0.
    """
    await update_counters(pool, run_id, summaries_created=0)
    return 0


async def run_dream(
    pool: asyncpg.Pool,
    *,
    triggered_by: str = "cron",
    window_hours: int = 168,
    cluster_min_size: int = 3,
    config: Optional[dict] = None,
) -> str:
    """End-to-end MORPHEUS run.

    Returns the run_id whether the run succeeded, failed, or
    short-circuited (zero memories in window). Caller can poll
    /v1/morpheus/runs/{id} for the final state. Exceptions inside
    phases are caught and recorded on the run row; they do not
    propagate to the trigger (cron / API caller / scheduler).
    """
    run_id = await begin_run(
        pool,
        triggered_by=triggered_by,
        window_hours=window_hours,
        cluster_min_size=cluster_min_size,
        config=config,
    )
    try:
        await set_phase(pool, run_id, "replay")
        await phase_replay(pool, run_id)
        await set_phase(pool, run_id, "cluster")
        await phase_cluster(pool, run_id)
        await set_phase(pool, run_id, "synthesise")
        await phase_synthesise(pool, run_id)
        await set_phase(pool, run_id, "commit")
        await finish_run(pool, run_id)
    except Exception as exc:
        logger.exception("[MORPHEUS] run %s failed in phase", run_id)
        await fail_run(pool, run_id, f"{type(exc).__name__}: {exc}")
    return run_id
