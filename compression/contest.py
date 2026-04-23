"""Competitive-selection orchestrator for the v3.1 compression platform.

One "contest" is one memory run through every eligible engine, with
the winner picked by composite score. Scoring is pure-Python and
side-effect-free; this module does not touch the database. DB
persistence lives in the distillation worker, which calls
persist_contest() (compression/contest_store.py) after run_contest()
returns.

Composite score (per ScoringProfile):

    composite = (q_w * quality_score)
              * (r_w * ratio_term)
              * (s_w * speed_factor)

    ratio_term  rewards reduction within [MIN_CHUNK_RATIO, 1.0). Below
                  the floor (degenerate/empty output) scores zero;
                  >= 1.0 (no compression or expanded) also zero.
                  Inside the band, ratio_term = 1 - ratio.
    speed_factor = fastest_elapsed_ms / this_elapsed_ms
                  normalized per-contest; fastest engine gets 1.0
    quality_floor applied as a pre-filter — candidates below the floor
                  are disqualified with reject_reason='quality_floor'
                  before scoring

Built-in profiles:

    balanced        q_w=1.0 r_w=1.0 s_w=1.0  floor=0.70
    quality_first   q_w=2.0 r_w=1.0 s_w=0.5  floor=0.80
    speed_first     q_w=0.8 r_w=1.0 s_w=2.0  floor=0.60

Custom profiles load from ~/.mnemos/compression_scoring.toml under a
[custom] table. Unknown profile names fall back to 'balanced' with a
warning.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .base import (
    MIN_CHUNK_RATIO,
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    IdentifierPolicy,
)

# Python 3.11+ floor per pyproject.toml; tomllib is stdlib.
import tomllib


logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_PATH = Path.home() / ".mnemos" / "compression_scoring.toml"


@dataclass(frozen=True)
class ScoringProfile:
    """Weights for the composite score.

    All weights are multiplicative. quality_floor is applied as a
    pre-filter — candidates with quality < floor are disqualified
    with reject_reason='quality_floor' and never scored.
    """

    name: str
    quality_weight: float
    ratio_weight: float
    speed_weight: float
    quality_floor: float


BUILT_IN_PROFILES: dict[str, ScoringProfile] = {
    "balanced": ScoringProfile(
        name="balanced",
        quality_weight=1.0,
        ratio_weight=1.0,
        speed_weight=1.0,
        quality_floor=0.70,
    ),
    "quality_first": ScoringProfile(
        name="quality_first",
        quality_weight=2.0,
        ratio_weight=1.0,
        speed_weight=0.5,
        quality_floor=0.80,
    ),
    "speed_first": ScoringProfile(
        name="speed_first",
        quality_weight=0.8,
        ratio_weight=1.0,
        speed_weight=2.0,
        quality_floor=0.60,
    ),
}


def load_scoring_profile(
    name: str = "balanced",
    config_path: Optional[Path] = None,
) -> ScoringProfile:
    """Resolve a profile name to a ScoringProfile.

    Built-in names ('balanced', 'quality_first', 'speed_first') return
    the pinned values. 'custom' loads from the TOML config file
    (default: ~/.mnemos/compression_scoring.toml); missing fields fall
    back to the 'balanced' values. Unknown names log a warning and
    return 'balanced'.
    """
    if name in BUILT_IN_PROFILES:
        return BUILT_IN_PROFILES[name]

    if name == "custom":
        path = config_path or _DEFAULT_CONFIG_PATH
        if not path.exists():
            logger.warning(
                "Scoring profile 'custom' requested but %s does not "
                "exist; falling back to 'balanced'",
                path,
            )
            return BUILT_IN_PROFILES["balanced"]
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            logger.warning(
                "Scoring profile config at %s is not valid TOML: %s — "
                "falling back to 'balanced'",
                path,
                exc,
            )
            return BUILT_IN_PROFILES["balanced"]

        custom = data.get("custom", {})
        base = BUILT_IN_PROFILES["balanced"]
        return ScoringProfile(
            name="custom",
            quality_weight=float(custom.get("quality_weight", base.quality_weight)),
            ratio_weight=float(custom.get("ratio_weight", base.ratio_weight)),
            speed_weight=float(custom.get("speed_weight", base.speed_weight)),
            quality_floor=float(custom.get("quality_floor", base.quality_floor)),
        )

    logger.warning("Unknown scoring profile %r; falling back to 'balanced'", name)
    return BUILT_IN_PROFILES["balanced"]


@dataclass
class ContestCandidate:
    """One engine's scored attempt in a contest round.

    speed_factor and composite_score are 0.0 for candidates that were
    rejected before scoring (disabled / error / no_output /
    quality_floor). Survivors that lose to the winner get
    reject_reason='inferior'; the winner has reject_reason=None.
    """

    result: CompressionResult
    speed_factor: float = 0.0
    composite_score: float = 0.0
    is_winner: bool = False
    reject_reason: Optional[str] = None


@dataclass
class ContestOutcome:
    """The full result of one contest round.

    Every engine considered appears in `candidates` — the ones skipped
    by supports() carry reject_reason='disabled' so the audit trail
    shows they were evaluated. `winner` is None if every candidate
    was disqualified.
    """

    contest_id: uuid.UUID
    memory_id: str
    owner_id: str
    scoring_profile: str
    candidates: List[ContestCandidate] = field(default_factory=list)
    winner: Optional[ContestCandidate] = None


def _ratio_term(ratio: Optional[float]) -> float:
    """Reward reduction within [MIN_CHUNK_RATIO, 1.0).

    Below MIN_CHUNK_RATIO the output is almost certainly degenerate
    (empty content, or a parse failure that produced ~nothing). Above
    or equal to 1.0 the engine did not compress or actively expanded.
    Both tails score zero. The operational sweet spot is a ratio in
    [0.2, 0.6] — ratio_term returns [0.4, 0.8] across that band.

    This floor catches a real regression found in live testing: when
    an LLM-assisted engine's importance-score response is unparseable,
    the adapter can silently return empty content with ratio=0.0. The
    naive "1 - ratio" would score that as maximum reward; with the
    floor applied, degenerate output lands with composite_score=0 and
    is classified 'inferior' (or lower) in the contest.
    """
    if ratio is None:
        return 0.0
    if ratio < MIN_CHUNK_RATIO or ratio >= 1.0:
        return 0.0
    return 1.0 - ratio


def _classify_failure(result: CompressionResult) -> str:
    """Map a non-succeeded result to a DB-allowlisted reject_reason."""
    if result.error is not None:
        return "error"
    return "no_output"


async def run_contest(
    engines: Sequence[CompressionEngine],
    request: CompressionRequest,
    *,
    profile: Optional[ScoringProfile] = None,
) -> ContestOutcome:
    """Run every eligible engine on one memory and select a winner.

    The function:

      1. Splits engines into eligible (supports()=True) and skipped
         (supports()=False → reject_reason='disabled').
      2. Runs eligible engines concurrently via asyncio.gather, with
         return_exceptions=True so one engine's crash doesn't kill the
         contest.
      3. Normalizes speed_factor across completed results (fastest=1.0).
      4. Applies the quality floor, then scores survivors.
      5. Picks the max-composite survivor as winner; remaining
         survivors get reject_reason='inferior'.

    The ContestOutcome is pure data — the caller writes it to the
    v3.1 tables via persist_contest() (separate module).
    """

    prof = profile or load_scoring_profile(request.scoring_profile)
    contest_id = uuid.uuid4()
    outcome = ContestOutcome(
        contest_id=contest_id,
        memory_id=request.memory_id,
        owner_id=request.owner_id,
        scoring_profile=prof.name,
    )

    eligible: list[CompressionEngine] = []
    original_token_estimate = len(request.content.split())

    for eng in engines:
        try:
            is_eligible = eng.supports(request)
        except Exception:
            logger.exception("Engine %r raised in supports(); skipping", eng.id)
            is_eligible = False
        if is_eligible:
            eligible.append(eng)
        else:
            placeholder = CompressionResult(
                engine_id=eng.id,
                engine_version=eng.version,
                original_tokens=original_token_estimate,
                identifier_policy=request.identifier_policy,
            )
            outcome.candidates.append(
                ContestCandidate(
                    result=placeholder,
                    reject_reason="disabled",
                )
            )

    if not eligible:
        return outcome

    gathered = await asyncio.gather(
        *(eng.compress(request) for eng in eligible),
        return_exceptions=True,
    )

    raw_results: list[CompressionResult] = []
    for eng, res in zip(eligible, gathered):
        if isinstance(res, BaseException):
            logger.exception("Engine %r raised in compress(): %r", eng.id, res)
            raw_results.append(
                CompressionResult(
                    engine_id=eng.id,
                    engine_version=eng.version,
                    original_tokens=original_token_estimate,
                    identifier_policy=request.identifier_policy,
                    error=f"{type(res).__name__}: {res}",
                )
            )
        else:
            raw_results.append(res)

    completed_times = [
        r.elapsed_ms for r in raw_results if r.succeeded() and r.elapsed_ms > 0
    ]
    fastest = min(completed_times) if completed_times else 0

    survivors: list[ContestCandidate] = []
    for r in raw_results:
        if not r.succeeded():
            outcome.candidates.append(
                ContestCandidate(
                    result=r,
                    reject_reason=_classify_failure(r),
                )
            )
            continue

        q = r.quality_score if r.quality_score is not None else 0.5
        if q < prof.quality_floor:
            outcome.candidates.append(
                ContestCandidate(
                    result=r,
                    reject_reason="quality_floor",
                )
            )
            continue

        if fastest > 0 and r.elapsed_ms > 0:
            speed_factor = fastest / r.elapsed_ms
        else:
            speed_factor = 1.0

        composite = (
            (prof.quality_weight * q)
            * (prof.ratio_weight * _ratio_term(r.compression_ratio))
            * (prof.speed_weight * speed_factor)
        )
        survivors.append(
            ContestCandidate(
                result=r,
                speed_factor=speed_factor,
                composite_score=composite,
            )
        )

    if survivors:
        winner = max(survivors, key=lambda c: c.composite_score)
        winner.is_winner = True
        outcome.winner = winner
        for c in survivors:
            if c is winner:
                c.reject_reason = None
            else:
                c.reject_reason = "inferior"
            outcome.candidates.append(c)

    return outcome


__all__ = [
    "BUILT_IN_PROFILES",
    "ContestCandidate",
    "ContestOutcome",
    "ScoringProfile",
    "load_scoring_profile",
    "run_contest",
]
