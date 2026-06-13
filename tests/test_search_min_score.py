"""Semantic relevance floor + score exposure (UAT 2026-06-13).

Covers:
  * normalize_similarity() across backend score conventions.
  * MemoryItem.score populated for semantic rows, None for FTS rows.
  * min_score floor drops the low-relevance tail and returns EMPTY for
    nonsense queries, while FTS results are never filtered.
  * min_score=0.0 opts out (legacy top-k-nearest).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import (
    DEFAULT_SEMANTIC_FLOOR,
    MemorySearchRequest,
    normalize_similarity,
    row_to_memory,
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


def _row(memory_id: str, content: str, *, similarity=None, rank_score=None) -> dict:
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
    if similarity is not None:
        row["similarity"] = similarity
    if rank_score is not None:
        row["rank_score"] = rank_score
    return row


# ---- normalize_similarity unit coverage --------------------------------


def test_normalize_similarity_from_similarity_col():
    # sqlite/postgres: already-cosine similarity, higher = better.
    assert normalize_similarity(_row("a", "x", similarity=0.73)) == pytest.approx(0.73)


def test_normalize_similarity_from_rank_score_distance():
    # oracle/mysql: COSINE distance -> 1 - distance.
    assert normalize_similarity(_row("a", "x", rank_score=0.40)) == pytest.approx(0.60)


def test_normalize_similarity_clamps_range():
    assert normalize_similarity(_row("a", "x", similarity=1.2)) == 1.0
    assert normalize_similarity(_row("a", "x", rank_score=1.4)) == 0.0  # 1 - 1.4 -> clamp


def test_normalize_similarity_none_for_fts_row():
    assert normalize_similarity(_row("a", "x")) is None


def test_normalize_similarity_handles_non_finite():
    assert normalize_similarity(_row("a", "x", similarity=float("nan"))) is None


def test_row_to_memory_sets_score():
    item = row_to_memory(_row("a", "x", similarity=0.66))
    assert item.score == pytest.approx(0.66)


def test_row_to_memory_score_none_for_fts():
    assert row_to_memory(_row("a", "x")).score is None


# ---- route-level floor behavior ---------------------------------------


async def _noop_bump(_ids):
    return None


async def _empty_decay(_backend):
    return {}


def _wire(monkeypatch, semantic_rows):
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("semantic_search", semantic_rows)
    backend.memories.configure_return("fts_search", [_row("fts_x", "fts fallback hit")])
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)
    return backend


async def _fake_embed(_query):
    return [0.1] * 8


@pytest.mark.asyncio
async def test_default_floor_drops_low_score_tail(monkeypatch):
    _wire(
        monkeypatch,
        [
            _row("good", "relevant", similarity=0.78),
            _row("tail", "noise", similarity=0.40),  # below 0.55 floor
        ],
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="real query", semantic=True),
        user=_alice(),
    )
    await asyncio.sleep(0)
    ids = [m.id for m in resp.memories]
    assert ids == ["good"]
    assert resp.count == 1
    assert resp.memories[0].score == pytest.approx(0.78)


@pytest.mark.asyncio
async def test_nonsense_query_returns_empty(monkeypatch):
    # All nearest neighbours are below the floor -> empty, NOT the
    # (zero-row -> FTS fallback) path, because rows existed but were
    # filtered out by relevance.
    _wire(
        monkeypatch,
        [
            _row("n1", "irrelevant", similarity=0.41),
            _row("n2", "irrelevant", similarity=0.33),
        ],
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="quantum gardening submarine", semantic=True),
        user=_alice(),
    )
    await asyncio.sleep(0)
    assert resp.count == 0
    assert resp.memories == []


@pytest.mark.asyncio
async def test_min_score_zero_opts_out(monkeypatch):
    _wire(
        monkeypatch,
        [
            _row("good", "relevant", similarity=0.78),
            _row("tail", "noise", similarity=0.20),
        ],
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="real query", semantic=True, min_score=0.0),
        user=_alice(),
    )
    await asyncio.sleep(0)
    assert {m.id for m in resp.memories} == {"good", "tail"}


@pytest.mark.asyncio
async def test_custom_floor_respected(monkeypatch):
    _wire(
        monkeypatch,
        [
            _row("hi", "a", similarity=0.85),
            _row("mid", "b", similarity=0.62),
        ],
    )
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="q", semantic=True, min_score=0.70),
        user=_alice(),
    )
    await asyncio.sleep(0)
    assert [m.id for m in resp.memories] == ["hi"]


@pytest.mark.asyncio
async def test_fts_results_never_filtered(monkeypatch):
    # FTS path: rows carry no score; the floor must not touch them.
    _wire(monkeypatch, [])
    resp = await memories_handler.search_memories(
        MemorySearchRequest(query="q", semantic=False, min_score=0.99),
        user=_alice(),
    )
    await asyncio.sleep(0)
    assert [m.id for m in resp.memories] == ["fts_x"]
    assert resp.memories[0].score is None


def test_default_floor_value_sane():
    assert 0.5 <= DEFAULT_SEMANTIC_FLOOR <= 0.65
