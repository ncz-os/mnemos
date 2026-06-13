"""Semantic relevance floor + score exposure (UAT 2026-06-13).

Covers:
  * score_to_similarity() across backend distance metrics.
  * normalize_similarity() reads only the canonical SEMANTIC_SCORE_KEY
    (never backend score columns -> no FTS rank collision).
  * MemoryItem.score populated for semantic rows, None for FTS rows.
  * min_score floor drops the low-relevance tail and returns EMPTY for
    nonsense queries, while FTS results are never filtered.
  * min_score=0.0 opts out; missing semantic score fails closed.
  * effective_semantic_floor() honors MNEMOS_SEMANTIC_FLOOR.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import (
    DEFAULT_SEMANTIC_FLOOR,
    METRIC_COSINE_DISTANCE,
    METRIC_COSINE_SIMILARITY,
    METRIC_EUCLIDEAN_UNIT,
    SEMANTIC_SCORE_KEY,
    MemorySearchRequest,
    normalize_similarity,
    row_to_memory,
    score_to_similarity,
)

from tests._fake_backend import install_fake_backend

_TS = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _row(memory_id: str, content: str, **extra) -> dict:
    row = {
        "id": memory_id,
        "content": content,
        "category": "facts",
        "subcategory": None,
        "created": _TS,
        "updated": _TS,
        "metadata": {},
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": content,
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }
    row.update(extra)
    return row


# ---- score_to_similarity unit coverage ---------------------------------


def test_score_cosine_similarity_passthrough():
    assert score_to_similarity(0.73, METRIC_COSINE_SIMILARITY) == pytest.approx(0.73)


def test_score_cosine_distance_inverts():
    assert score_to_similarity(0.40, METRIC_COSINE_DISTANCE) == pytest.approx(0.60)


def test_score_euclidean_unit_formula():
    # cos = 1 - d^2/2; identical vectors d=0 -> 1.0; orthogonal d=sqrt2 -> 0.0
    assert score_to_similarity(0.0, METRIC_EUCLIDEAN_UNIT) == pytest.approx(1.0)
    assert score_to_similarity(2.0**0.5, METRIC_EUCLIDEAN_UNIT) == pytest.approx(0.0)


def test_score_clamps_and_handles_bad():
    assert score_to_similarity(1.4, METRIC_COSINE_DISTANCE) == 0.0  # 1-1.4 clamp
    assert score_to_similarity(1.2, METRIC_COSINE_SIMILARITY) == 1.0
    assert score_to_similarity(float("nan"), METRIC_COSINE_SIMILARITY) is None
    assert score_to_similarity(None, METRIC_COSINE_SIMILARITY) is None


def test_score_unknown_metric_fails_closed():
    # A typo'd/misconfigured metric must NOT treat a distance as a
    # similarity (which would bypass the floor) -> None.
    assert score_to_similarity(0.1, "bogus_metric") is None


# ---- normalize_similarity reads only the canonical key -----------------


def test_normalize_reads_canonical_key_only():
    assert normalize_similarity(_row("a", "x", **{SEMANTIC_SCORE_KEY: 0.66})) == pytest.approx(0.66)
    # raw backend columns are IGNORED (FTS rank collision protection)
    assert normalize_similarity(_row("a", "x", rank_score=0.4)) is None
    assert normalize_similarity(_row("a", "x", similarity=0.9)) is None


def test_row_to_memory_score_from_canonical_key():
    assert row_to_memory(_row("a", "x", **{SEMANTIC_SCORE_KEY: 0.7})).score == pytest.approx(0.7)


def test_row_to_memory_score_none_for_fts():
    assert row_to_memory(_row("a", "x", rank_score=0.5)).score is None


# ---- route-level floor behavior ---------------------------------------


async def _noop_bump(_ids):
    return None


async def _empty_decay(_backend):
    return {}


async def _fake_embed(_query):
    return [0.1] * 8


def _wire(monkeypatch, semantic_rows):
    # Fake repo carries no SEMANTIC_SCORE_COLUMN attr -> route uses the
    # MemoryRepository default (rank_score / cosine_distance), matching
    # oracle/mysql (production). So semantic rows here use rank_score.
    backend = install_fake_backend(monkeypatch)
    # The fake repo returns an AsyncMock for any unset attribute, so set
    # the score-column contract explicitly (production repos declare it
    # as a class attr). Matches oracle/mysql: rank_score / cosine_distance.
    backend.memories.SEMANTIC_SCORE_COLUMN = "rank_score"
    backend.memories.SEMANTIC_SCORE_METRIC = METRIC_COSINE_DISTANCE
    backend.memories.configure_return("semantic_search", semantic_rows)
    backend.memories.configure_return("fts_search", [_row("fts_x", "fts fallback hit")])
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)
    return backend


@pytest.mark.asyncio
async def test_default_floor_drops_low_score_tail(monkeypatch):
    # rank_score (cosine distance): 0.20 -> sim 0.80 (keep); 0.60 -> 0.40 (drop)
    _wire(
        monkeypatch,
        [_row("good", "relevant", rank_score=0.20), _row("tail", "noise", rank_score=0.60)],
    )
    resp = await memories_handler.search_memories(MemorySearchRequest(query="real query", semantic=True), user=_alice())
    await asyncio.sleep(0)
    assert [m.id for m in resp.memories] == ["good"]
    assert resp.count == 1
    assert resp.memories[0].score == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_nonsense_query_returns_empty(monkeypatch):
    # all nearest neighbours below floor -> empty (rows existed, filtered)
    _wire(
        monkeypatch,
        [_row("n1", "x", rank_score=0.59), _row("n2", "y", rank_score=0.67)],  # sims 0.41, 0.33
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="quantum gardening submarine", semantic=True), user=_alice()
    )
    await asyncio.sleep(0)
    assert resp.count == 0
    assert resp.memories == []


@pytest.mark.asyncio
async def test_min_score_zero_opts_out(monkeypatch):
    _wire(
        monkeypatch,
        [_row("good", "a", rank_score=0.22), _row("tail", "b", rank_score=0.80)],
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="q", semantic=True, min_score=0.0), user=_alice()
    )
    await asyncio.sleep(0)
    assert {m.id for m in resp.memories} == {"good", "tail"}


@pytest.mark.asyncio
async def test_custom_floor_respected(monkeypatch):
    _wire(
        monkeypatch,
        [_row("hi", "a", rank_score=0.15), _row("mid", "b", rank_score=0.38)],  # sims 0.85, 0.62
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="q", semantic=True, min_score=0.70), user=_alice()
    )
    await asyncio.sleep(0)
    assert [m.id for m in resp.memories] == ["hi"]


@pytest.mark.asyncio
async def test_missing_semantic_score_fails_closed(monkeypatch):
    # genuine semantic row with NO score column -> treated as below floor
    _wire(monkeypatch, [_row("noscore", "x")])
    resp = await memories_handler.search_memories(MemorySearchRequest(query="q", semantic=True), user=_alice())
    await asyncio.sleep(0)
    assert resp.count == 0


@pytest.mark.asyncio
async def test_fts_results_never_filtered(monkeypatch):
    _wire(monkeypatch, [])  # 0 semantic rows -> FTS fallback
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="q", semantic=False, min_score=0.99), user=_alice()
    )
    await asyncio.sleep(0)
    assert [m.id for m in resp.memories] == ["fts_x"]
    assert resp.memories[0].score is None


def test_default_floor_value_sane():
    assert 0.5 <= DEFAULT_SEMANTIC_FLOOR <= 0.65


def test_effective_floor_env_override(monkeypatch):
    monkeypatch.setenv("MNEMOS_SEMANTIC_FLOOR", "0.0")
    assert memories_handler.effective_semantic_floor() == 0.0
    monkeypatch.setenv("MNEMOS_SEMANTIC_FLOOR", "0.62")
    assert memories_handler.effective_semantic_floor() == pytest.approx(0.62)
    monkeypatch.setenv("MNEMOS_SEMANTIC_FLOOR", "garbage")
    assert memories_handler.effective_semantic_floor() == DEFAULT_SEMANTIC_FLOOR
    # non-finite must fall back to default, not clamp to 1.0/0.0
    for bad in ("nan", "inf", "-inf"):
        monkeypatch.setenv("MNEMOS_SEMANTIC_FLOOR", bad)
        assert memories_handler.effective_semantic_floor() == DEFAULT_SEMANTIC_FLOOR
    monkeypatch.delenv("MNEMOS_SEMANTIC_FLOOR", raising=False)
    assert memories_handler.effective_semantic_floor() == DEFAULT_SEMANTIC_FLOOR
