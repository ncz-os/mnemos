"""Unit tests for v6.2 M-2.2.4 per-category temporal decay."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio


from mnemos.domain.search.decay import (
    DEFAULT_CATEGORY,
    DecayParams,
    apply_decay,
    invalidate_decay_cache,
    load_decay_table,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate_decay_cache()
    yield
    invalidate_decay_cache()


class TestMultiplier:
    def test_exponential_at_zero_returns_one(self):
        p = DecayParams("feedback", 365, "exponential", 0.5)
        assert p.multiplier(0) == pytest.approx(1.0)

    def test_exponential_at_half_life_returns_half(self):
        p = DecayParams("feedback", 365, "exponential", 0.0)
        assert p.multiplier(365) == pytest.approx(0.5, rel=1e-3)

    def test_exponential_floor_clamps(self):
        p = DecayParams("feedback", 30, "exponential", 0.5)
        # After 10 half-lives raw value is ~0.001 -- floor must clamp to 0.5
        assert p.multiplier(30 * 10) == pytest.approx(0.5)

    def test_sigmoid_collapses_around_threshold(self):
        p = DecayParams("credentials", 14, "sigmoid", 0.0)
        # Well-before threshold: near 1
        assert p.multiplier(0) > 0.99
        # At threshold: ~0.5
        assert 0.4 < p.multiplier(14) < 0.6
        # Well-after threshold: near 0
        assert p.multiplier(28) < 0.05

    def test_none_kind_no_decay(self):
        p = DecayParams("rules", 1, "none", 0.0)
        assert p.multiplier(1000) == pytest.approx(1.0)


class TestApplyDecay:
    def _build_memory(self, category: str, age_days: int, quality_rating: int = 80):
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        created = (now - timedelta(days=age_days)).isoformat()
        return SimpleNamespace(
            id=f"mem_{uuid.uuid4().hex[:8]}",
            category=category,
            created=created,
            quality_rating=quality_rating,
        )

    def test_decay_reorders_by_category(self):
        """Old project memory should rank below new credential memory
        even when base scores are equal."""
        table = {
            "project": DecayParams("project", 60, "exponential", 0.05),
            "credentials": DecayParams("credentials", 14, "sigmoid", 0.0),
        }
        new_cred = self._build_memory("credentials", age_days=1, quality_rating=80)
        old_proj = self._build_memory("project", age_days=200, quality_rating=80)
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        ordered = apply_decay([old_proj, new_cred], table, now=now)
        assert ordered[0].id == new_cred.id
        assert ordered[1].id == old_proj.id

    def test_default_category_fallback(self):
        """Unknown categories fall back to (default)."""
        table = {
            DEFAULT_CATEGORY: DecayParams(DEFAULT_CATEGORY, 180, "exponential", 0.1),
        }
        m = self._build_memory("unknown-cat", age_days=180)
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        out = apply_decay([m], table, now=now)
        # ~0.5 multiplier at half-life -> 80 * 0.5 = 40
        assert getattr(out[0], "decay_multiplier") == pytest.approx(0.5, rel=1e-2)

    def test_universal_override(self):
        """overrides={'*': 1.0} disables decay across all categories."""
        table = {
            "project": DecayParams("project", 60, "exponential", 0.05),
        }
        m = self._build_memory("project", age_days=300)
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        out = apply_decay([m], table, now=now, overrides={"*": 1.0})
        assert getattr(out[0], "decay_multiplier") == 1.0

    def test_per_category_override(self):
        """overrides={'project': 0.0} kills project category scores."""
        table = {
            "project": DecayParams("project", 60, "exponential", 0.05),
        }
        m = self._build_memory("project", age_days=10)
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        out = apply_decay([m], table, now=now, overrides={"project": 0.0})
        assert getattr(out[0], "decay_multiplier") == 0.0
        assert getattr(out[0], "decay_final_score") == 0.0

    def test_recency_weight_rescales(self):
        """recency_weight=0.5 doubles effective half-life."""
        table = {
            "facts": DecayParams("facts", 100, "exponential", 0.0),
        }
        m1 = self._build_memory("facts", age_days=100)
        m2 = self._build_memory("facts", age_days=100)
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        # Default weight: half_life=100 -> ~0.5 at 100d
        out_default = apply_decay([m1], table, now=now, recency_weight=1.0)
        # Doubled half_life -> 100d is only half a half-life -> ~0.707
        out_doubled = apply_decay([m2], table, now=now, recency_weight=0.5)
        assert getattr(out_default[0], "decay_multiplier") < getattr(out_doubled[0], "decay_multiplier")

    def test_empty_list_returns_empty(self):
        assert apply_decay([], {"x": DecayParams("x", 1, "exponential", 0)}) == []

    def test_no_table_returns_input_unchanged(self):
        """Empty table + no overrides -> no decay applied."""
        m = self._build_memory("project", age_days=200)
        out = apply_decay([m], {})
        assert out == [m]


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    class _S:
        class database:
            embedding_dim = 1024

    backend = SqliteBackend(tmp_path / "decay.db", _S())
    await backend.open()
    yield backend
    await backend.close()


@pytest.mark.asyncio
async def test_load_decay_table_returns_seeded_rows(sqlite_backend):
    """Schema migration seeds the table with 9+1 (default) entries."""
    table = await load_decay_table(sqlite_backend)
    assert "feedback" in table
    assert "credentials" in table
    assert DEFAULT_CATEGORY in table
    feedback = table["feedback"]
    assert feedback.half_life_days == 365
    assert feedback.decay_kind == "exponential"
    assert feedback.floor == 0.5


@pytest.mark.asyncio
async def test_load_decay_table_caches(sqlite_backend):
    """Second call within TTL returns cached snapshot without re-querying."""
    t1 = await load_decay_table(sqlite_backend)
    t2 = await load_decay_table(sqlite_backend)
    # Same dict object -> cache hit
    assert t1 is t2
