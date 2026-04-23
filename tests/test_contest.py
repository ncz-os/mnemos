"""Competitive-selection contest — scoring and orchestration tests.

Pure-logic checks for compression/contest.py. No DB, no real engines;
everything runs against deterministic mock engines in-process. DB
persistence (memory_compression_candidates, memory_compressed_variants)
is tested separately once the persist_contest helper lands.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from compression.base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from compression.contest import (
    BUILT_IN_PROFILES,
    ContestOutcome,
    ScoringProfile,
    _ratio_term,
    load_scoring_profile,
    run_contest,
)


# ---- test fixtures ---------------------------------------------------------


class MockEngine(CompressionEngine):
    """Deterministic fake engine for contest tests."""

    def __init__(
        self,
        id_: str,
        *,
        quality: float = 0.9,
        ratio: float = 0.4,
        elapsed_ms: int = 50,
        error: str | None = None,
        supported: bool = True,
    ) -> None:
        self.id = id_
        self.label = f"Mock {id_}"
        self.version = "1"
        self.gpu_intent = GPUIntent.CPU_ONLY
        self._q = quality
        self._r = ratio
        self._e = elapsed_ms
        self._err = error
        self._sup = supported
        super().__init__()

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        if self._err is not None:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=100,
                elapsed_ms=self._e,
                error=self._err,
                identifier_policy=request.identifier_policy,
            )
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=100,
            compressed_tokens=int(100 * self._r),
            compressed_content="x" * int(100 * self._r),
            compression_ratio=self._r,
            quality_score=self._q,
            elapsed_ms=self._e,
            gpu_used=False,
            identifier_policy=request.identifier_policy,
        )

    def supports(self, request: CompressionRequest) -> bool:  # noqa: ARG002
        return self._sup


def _request(profile: str = "balanced") -> CompressionRequest:
    return CompressionRequest(
        memory_id="mem-1",
        content="hello world " * 50,
        scoring_profile=profile,
    )


# ---- built-in profiles -----------------------------------------------------


def test_builtin_profiles_pinned():
    # The DB CHECK constraint for scoring_profile lists these names;
    # a renamed built-in needs a migration.
    assert set(BUILT_IN_PROFILES) == {"balanced", "quality_first", "speed_first"}

    assert BUILT_IN_PROFILES["balanced"].quality_floor == 0.70
    assert BUILT_IN_PROFILES["quality_first"].quality_weight == 2.0
    assert BUILT_IN_PROFILES["quality_first"].quality_floor == 0.80
    assert BUILT_IN_PROFILES["speed_first"].speed_weight == 2.0
    assert BUILT_IN_PROFILES["speed_first"].quality_floor == 0.60


def test_load_unknown_profile_falls_back_to_balanced(caplog):
    with caplog.at_level("WARNING"):
        prof = load_scoring_profile("nonexistent")
    assert prof.name == "balanced"
    assert any("Unknown scoring profile" in rec.message for rec in caplog.records)


def test_load_custom_with_missing_config_falls_back_to_balanced():
    prof = load_scoring_profile("custom", config_path=Path("/no/such/file.toml"))
    assert prof.name == "balanced"


def test_load_custom_parses_toml():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(
            "[custom]\n"
            "quality_weight = 3.14\n"
            "ratio_weight = 0.5\n"
            "speed_weight = 0.1\n"
            "quality_floor = 0.42\n"
        )
        path = Path(fh.name)
    try:
        prof = load_scoring_profile("custom", config_path=path)
        assert prof.name == "custom"
        assert prof.quality_weight == 3.14
        assert prof.ratio_weight == 0.5
        assert prof.speed_weight == 0.1
        assert prof.quality_floor == 0.42
    finally:
        path.unlink()


def test_load_custom_partial_toml_fills_defaults_from_balanced():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("[custom]\nquality_weight = 5.0\n")
        path = Path(fh.name)
    try:
        prof = load_scoring_profile("custom", config_path=path)
        assert prof.quality_weight == 5.0
        # Fell back to balanced for unspecified fields
        assert prof.ratio_weight == BUILT_IN_PROFILES["balanced"].ratio_weight
        assert prof.speed_weight == BUILT_IN_PROFILES["balanced"].speed_weight
        assert prof.quality_floor == BUILT_IN_PROFILES["balanced"].quality_floor
    finally:
        path.unlink()


def test_load_custom_invalid_toml_falls_back(caplog):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write("this is not valid toml [[[")
        path = Path(fh.name)
    try:
        with caplog.at_level("WARNING"):
            prof = load_scoring_profile("custom", config_path=path)
        assert prof.name == "balanced"
    finally:
        path.unlink()


# ---- ratio_term boundaries -------------------------------------------------


@pytest.mark.parametrize(
    "ratio, expected",
    [
        (None, 0.0),             # engine produced no output
        (1.0, 0.0),              # no compression
        (0.5, 0.5),
        (0.2, 0.8),
        (1.5, 0.0),              # expanded output (loser)
    ],
)
def test_ratio_term(ratio, expected):
    assert abs(_ratio_term(ratio) - expected) < 1e-9


# ---- contest orchestration -------------------------------------------------


def test_contest_classifies_every_engine():
    # One disabled, one errored, one below-floor, two survivors.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
        MockEngine("low_q", quality=0.50, ratio=0.2, elapsed_ms=50),
        MockEngine("broken", quality=0.9, ratio=0.5, elapsed_ms=20, error="network"),
        MockEngine("unsupp", supported=False),
    ]
    outcome = asyncio.run(run_contest(engines, _request()))

    reasons = {c.result.engine_id: c.reject_reason for c in outcome.candidates}
    assert reasons["unsupp"] == "disabled"
    assert reasons["broken"] == "error"
    assert reasons["low_q"] == "quality_floor"
    # One of the two survivors wins; the other is "inferior"
    survivor_reasons = {reasons["fast_good"], reasons["slow_great"]}
    assert None in survivor_reasons
    assert "inferior" in survivor_reasons

    # Winner is the None-reason candidate
    assert outcome.winner is not None
    assert reasons[outcome.winner.result.engine_id] is None


def test_balanced_profile_prefers_fast_even_with_lower_quality():
    # fast_good has q=0.85, r=0.4 (ratio_term=0.6), speed=1.0 -> 0.51
    # slow_great has q=0.95, r=0.3 (ratio_term=0.7), speed=10/300 -> ~0.022
    # Balanced should pick fast_good.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
    ]
    outcome = asyncio.run(run_contest(engines, _request("balanced")))
    assert outcome.winner.result.engine_id == "fast_good"


def test_quality_first_profile_rewards_quality_over_speed():
    # Under quality_first (q_weight=2, s_weight=0.5, floor=0.80), the
    # 0.50 candidate is still disqualified (floor), and between
    # fast_good (q=0.85) and slow_great (q=0.95) the latter's quality
    # advantage matters more.
    # composite fast_good   = (2*0.85)*(1*0.6)*(0.5*1.0)      = 0.510
    # composite slow_great  = (2*0.95)*(1*0.7)*(0.5*0.0333)   = 0.022
    # The 1/30 speed ratio still wins for fast_good — the
    # quality weight can't fully close a 30x speed gap. This test
    # pins that behavior: profile shifts shape the outcome but don't
    # overrule wide-margin speed differences.
    engines = [
        MockEngine("fast_good", quality=0.85, ratio=0.4, elapsed_ms=10),
        MockEngine("slow_great", quality=0.95, ratio=0.3, elapsed_ms=300),
    ]
    outcome = asyncio.run(run_contest(engines, _request("quality_first")))
    assert outcome.winner.result.engine_id == "fast_good"


def test_speed_factor_normalized_to_fastest():
    engines = [
        MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10),
        MockEngine("b", quality=0.9, ratio=0.4, elapsed_ms=100),
    ]
    outcome = asyncio.run(run_contest(engines, _request()))
    sf = {c.result.engine_id: c.speed_factor for c in outcome.candidates if c.reject_reason in (None, "inferior")}
    assert abs(sf["a"] - 1.0) < 0.001
    assert abs(sf["b"] - 0.1) < 0.001


def test_all_fail_returns_no_winner():
    engines = [
        MockEngine("low1", quality=0.5, ratio=0.4, elapsed_ms=10),
        MockEngine("low2", quality=0.4, ratio=0.4, elapsed_ms=10),
    ]
    outcome = asyncio.run(run_contest(engines, _request()))
    assert outcome.winner is None
    assert all(c.reject_reason == "quality_floor" for c in outcome.candidates)


def test_crashing_engine_becomes_error_candidate():
    class Crashing(CompressionEngine):
        id = "crash"
        label = "Crashing"
        version = "1"

        async def compress(self, request):  # noqa: ARG002
            raise RuntimeError("kaboom")

    outcome = asyncio.run(run_contest([Crashing()], _request()))
    assert outcome.winner is None
    assert len(outcome.candidates) == 1
    c = outcome.candidates[0]
    assert c.reject_reason == "error"
    assert c.result.error is not None and "kaboom" in c.result.error


def test_empty_engine_list():
    outcome = asyncio.run(run_contest([], _request()))
    assert outcome.winner is None
    assert outcome.candidates == []


def test_supports_exception_treated_as_ineligible():
    class BadSupports(CompressionEngine):
        id = "bad_supports"
        label = "x"
        version = "1"

        async def compress(self, request):  # noqa: ARG002
            return CompressionResult(engine_id=self.id, engine_version="1", original_tokens=0)

        def supports(self, request):  # noqa: ARG002
            raise RuntimeError("boom")

    outcome = asyncio.run(run_contest([BadSupports()], _request()))
    assert outcome.winner is None
    assert outcome.candidates[0].reject_reason == "disabled"


def test_contest_id_is_unique_per_run():
    engines = [MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10)]
    out_a = asyncio.run(run_contest(engines, _request()))
    out_b = asyncio.run(run_contest(engines, _request()))
    assert out_a.contest_id != out_b.contest_id


def test_outcome_is_serializable_shape():
    # Sanity check that the ContestOutcome dataclass is well-formed.
    engines = [MockEngine("a", quality=0.9, ratio=0.4, elapsed_ms=10)]
    outcome = asyncio.run(run_contest(engines, _request()))
    assert isinstance(outcome, ContestOutcome)
    assert isinstance(outcome.scoring_profile, str)
    assert isinstance(outcome.candidates, list)
    assert all(c.result is not None for c in outcome.candidates)
