"""Distillation-worker helper: drains memory_compression_queue via the
v3.1 contest path.

Separate module so the queue-draining code can be unit-tested in
isolation (mock pool + mock engines) and live-tested against a
throwaway Postgres without booting the full MemoryDistillationWorker.
The existing `distillation_worker.py` keeps its direct-memory-polling
loop for v3.0 backward compat; in v3.1 it calls
`process_contest_queue()` once per loop iteration alongside the legacy
path.

Queue lifecycle per row:

    pending -> running          (atomic dequeue with SKIP LOCKED)
    running -> done             (contest had a winner, persist_contest
                                  wrote winner + losers)
    running -> failed           (no winner, OR persist raised, OR too
                                  many attempts reached)

Failure-recording rules:

  * attempts counter is incremented every time a row transitions from
    pending to running, regardless of outcome.
  * If a row's attempts exceeds its max on entry, we fast-fail it:
    mark failed immediately without re-running the contest. (Stops
    a persistently-broken memory from spinning forever.)
  * If the contest produces candidates but no winner (every engine
    disqualified), the queue row is marked 'failed' with a synthetic
    error summarizing the reject_reasons. The candidates are still
    persisted — operators can inspect the audit log to see why every
    engine failed.
  * If persist_contest raises, the queue row is marked 'failed' with
    the exception text. No partial state remains in the contest
    tables (persist_contest is in one transaction).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, Optional, Sequence

from .base import CompressionEngine, CompressionRequest, IdentifierPolicy
from .contest import run_contest
from .contest_store import persist_contest


logger = logging.getLogger(__name__)


_MAX_ATTEMPTS = 3


_DEQUEUE_SQL = """
WITH next AS (
    SELECT id
    FROM memory_compression_queue
    WHERE status = 'pending'
    ORDER BY priority DESC, enqueued_at
    FOR UPDATE SKIP LOCKED
    LIMIT $1
)
UPDATE memory_compression_queue q
SET status      = 'running',
    started_at  = NOW(),
    attempts    = q.attempts + 1
FROM next
WHERE q.id = next.id
RETURNING q.id, q.memory_id, q.owner_id, q.reason,
          q.scoring_profile, q.attempts
"""

_MEMORY_CONTENT_SQL = """
SELECT id, content, category, task_type
FROM memories
WHERE id = $1
"""

_MARK_DONE_SQL = """
UPDATE memory_compression_queue
SET status      = 'done',
    finished_at = NOW(),
    error       = NULL
WHERE id = $1
"""

_MARK_FAILED_SQL = """
UPDATE memory_compression_queue
SET status      = 'failed',
    finished_at = NOW(),
    error       = $2
WHERE id = $1
"""


async def process_contest_queue(
    pool: Any,
    engines: Sequence[CompressionEngine],
    *,
    batch_size: int = 5,
    max_attempts: int = _MAX_ATTEMPTS,
    judge_model: Optional[str] = None,
) -> Dict[str, int]:
    """Drain up to `batch_size` pending queue rows via the contest path.

    `pool` is an asyncpg Pool (or anything with acquire() that yields
    a connection supporting fetch/fetchrow/execute/transaction — the
    unit tests stub this with AsyncMock). Each queue row runs its
    contest in a separate connection so one row's DB activity doesn't
    stall or transact with another's. `max_attempts` caps retries per
    row (default 3, matching the legacy distillation worker's
    MAX_ATTEMPTS).

    Returns a dict {'dequeued', 'succeeded', 'failed', 'skipped_max_attempts',
    'missing_memory'} for the caller to log and for metrics.
    """

    counts: Counter[str] = Counter()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_DEQUEUE_SQL, batch_size)

    if not rows:
        return dict(counts)

    counts["dequeued"] = len(rows)

    for row in rows:
        queue_id = row["id"]
        memory_id = row["memory_id"]
        owner_id = row["owner_id"]
        attempts = row["attempts"]
        scoring_profile = row["scoring_profile"]

        if attempts > max_attempts:
            async with pool.acquire() as conn:
                await conn.execute(
                    _MARK_FAILED_SQL,
                    queue_id,
                    f"max_attempts exceeded ({attempts} > {max_attempts})",
                )
            counts["skipped_max_attempts"] += 1
            counts["failed"] += 1
            logger.warning(
                "contest_queue[%s]: skipped, attempts=%d > max=%d",
                memory_id, attempts, max_attempts,
            )
            continue

        try:
            await _process_one(
                pool,
                queue_id=queue_id,
                memory_id=memory_id,
                owner_id=owner_id,
                scoring_profile=scoring_profile,
                engines=engines,
                counts=counts,
                judge_model=judge_model,
            )
        except Exception as exc:
            logger.exception(
                "contest_queue[%s]: unhandled exception", memory_id
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    _MARK_FAILED_SQL,
                    queue_id,
                    f"{type(exc).__name__}: {exc}",
                )
            counts["failed"] += 1

    return dict(counts)


async def _process_one(
    pool: Any,
    *,
    queue_id: Any,
    memory_id: str,
    owner_id: str,
    scoring_profile: str,
    engines: Sequence[CompressionEngine],
    counts: Counter[str],
    judge_model: Optional[str],
) -> None:
    """Run the contest for a single dequeued queue row + persist + update."""

    async with pool.acquire() as conn:
        mem = await conn.fetchrow(_MEMORY_CONTENT_SQL, memory_id)

    if mem is None or not mem["content"]:
        async with pool.acquire() as conn:
            await conn.execute(
                _MARK_FAILED_SQL,
                queue_id,
                "memory not found or empty content",
            )
        counts["missing_memory"] += 1
        counts["failed"] += 1
        logger.warning(
            "contest_queue[%s]: memory not found or empty, marking failed",
            memory_id,
        )
        return

    request = CompressionRequest(
        memory_id=memory_id,
        content=mem["content"],
        owner_id=owner_id,
        task_type=mem.get("task_type") or mem["category"],
        scoring_profile=scoring_profile,
        identifier_policy=IdentifierPolicy.STRICT,
    )
    outcome = await run_contest(engines, request)

    async with pool.acquire() as conn:
        try:
            await persist_contest(conn, outcome, judge_model=judge_model)
        except Exception as exc:
            logger.exception(
                "contest_queue[%s]: persist_contest failed", memory_id
            )
            await conn.execute(
                _MARK_FAILED_SQL,
                queue_id,
                f"persist failed: {type(exc).__name__}: {exc}",
            )
            counts["failed"] += 1
            return

        if outcome.winner is None:
            reasons = Counter(
                c.reject_reason or "unknown" for c in outcome.candidates
            )
            reason_summary = ", ".join(
                f"{reason}={count}" for reason, count in reasons.most_common()
            )
            await conn.execute(
                _MARK_FAILED_SQL,
                queue_id,
                f"no winner: {reason_summary}",
            )
            counts["failed"] += 1
            logger.info(
                "contest_queue[%s]: no winner (%s)", memory_id, reason_summary
            )
        else:
            await conn.execute(_MARK_DONE_SQL, queue_id)
            counts["succeeded"] += 1
            logger.info(
                "contest_queue[%s]: winner=%s score=%.4f",
                memory_id,
                outcome.winner.result.engine_id,
                outcome.winner.composite_score,
            )


__all__ = ["process_contest_queue"]
