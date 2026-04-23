"""Persistence layer for v3.1 compression contests.

One public function, persist_contest(), writes a ContestOutcome from
compression/contest.py into the two tables it spans:

  * memory_compression_candidates — one row per engine attempt (winner
    + every loser, including disabled / error / no_output /
    quality_floor candidates) with their scoring fields.
  * memory_compressed_variants    — upserted for the memory with a
    pointer at the winning candidate's row and an inlined copy of the
    compressed_content so downstream reads don't require a join.

All writes happen in a single transaction so a partial failure can't
leave a memory with a variant whose winner_candidate_id points at a
row that isn't there. The transaction DOES NOT touch
memory_compression_queue — the distillation worker is responsible for
that row's lifecycle (status transitions, attempts counter, error
string) so the persistence function stays idempotent on its own
surface.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .contest import ContestOutcome

logger = logging.getLogger(__name__)


_INSERT_CANDIDATE_SQL = """
INSERT INTO memory_compression_candidates (
    memory_id, owner_id, contest_id, engine_id, engine_version,
    compressed_content, original_tokens, compressed_tokens,
    compression_ratio, quality_score, speed_factor, composite_score,
    scoring_profile, elapsed_ms, judge_model, gpu_used,
    is_winner, reject_reason, manifest
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15, $16,
    $17, $18, $19::jsonb
)
RETURNING id
"""

_UPSERT_VARIANT_SQL = """
INSERT INTO memory_compressed_variants (
    memory_id, owner_id, winner_candidate_id,
    engine_id, engine_version, compressed_content,
    compressed_tokens, compression_ratio, quality_score,
    composite_score, scoring_profile, judge_model
) VALUES (
    $1, $2, $3,
    $4, $5, $6,
    $7, $8, $9,
    $10, $11, $12
)
ON CONFLICT (memory_id) DO UPDATE SET
    winner_candidate_id = EXCLUDED.winner_candidate_id,
    engine_id           = EXCLUDED.engine_id,
    engine_version      = EXCLUDED.engine_version,
    compressed_content  = EXCLUDED.compressed_content,
    compressed_tokens   = EXCLUDED.compressed_tokens,
    compression_ratio   = EXCLUDED.compression_ratio,
    quality_score       = EXCLUDED.quality_score,
    composite_score     = EXCLUDED.composite_score,
    scoring_profile     = EXCLUDED.scoring_profile,
    judge_model         = EXCLUDED.judge_model,
    selected_at         = NOW()
"""


def _nullable_positive(value: Optional[float]) -> Optional[float]:
    """Coerce 0.0 or None to None for fields where 0 would be misleading.

    speed_factor and composite_score are 0.0 on rejected candidates
    (disabled / error / no_output / quality_floor) because they were
    never scored. The DB column allows NULL, so record NULL to make
    the rejection visible instead of an artificial zero.
    """
    if value is None:
        return None
    return value if value > 0 else None


async def persist_contest(
    conn: Any,
    outcome: ContestOutcome,
    *,
    judge_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the contest outcome to the v3.1 compression tables.

    `conn` is an asyncpg Connection (not a Pool). Callers who hold a
    Pool should acquire a connection themselves — requiring a single
    connection here keeps the transaction semantics obvious (no
    cross-connection .transaction() confusion).

    `judge_model` is used as a fallback for candidates whose result
    didn't record one (e.g., LETHE which self-assesses quality without
    an external judge). If the candidate already set judge_model,
    that value wins.

    Returns {'candidates_written', 'variant_written', 'contest_id',
    'winner_engine'} for the caller to log.
    """

    winner_candidate_db_id: Optional[Any] = None
    candidates_written = 0

    async with conn.transaction():
        for cand in outcome.candidates:
            r = cand.result
            manifest_json = json.dumps(r.manifest or {})
            row = await conn.fetchrow(
                _INSERT_CANDIDATE_SQL,
                outcome.memory_id,
                outcome.owner_id,
                outcome.contest_id,
                r.engine_id,
                r.engine_version,
                r.compressed_content,
                r.original_tokens,
                r.compressed_tokens,
                r.compression_ratio,
                r.quality_score,
                _nullable_positive(cand.speed_factor),
                _nullable_positive(cand.composite_score),
                outcome.scoring_profile,
                r.elapsed_ms if r.elapsed_ms > 0 else None,
                r.judge_model or judge_model,
                r.gpu_used,
                cand.is_winner,
                cand.reject_reason,
                manifest_json,
            )
            candidates_written += 1
            if cand.is_winner:
                winner_candidate_db_id = row["id"]

        variant_written = False
        if outcome.winner is not None and winner_candidate_db_id is not None:
            w = outcome.winner
            r = w.result
            await conn.execute(
                _UPSERT_VARIANT_SQL,
                outcome.memory_id,
                outcome.owner_id,
                winner_candidate_db_id,
                r.engine_id,
                r.engine_version,
                r.compressed_content,
                r.compressed_tokens,
                r.compression_ratio,
                r.quality_score,
                w.composite_score,
                outcome.scoring_profile,
                r.judge_model or judge_model,
            )
            variant_written = True

    return {
        "contest_id": str(outcome.contest_id),
        "memory_id": outcome.memory_id,
        "candidates_written": candidates_written,
        "variant_written": variant_written,
        "winner_engine": outcome.winner.result.engine_id if outcome.winner else None,
    }


__all__ = ["persist_contest"]
