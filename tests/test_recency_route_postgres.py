from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence.postgres import PostgresMemoryRepository
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
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
        "archived_at": None,
    }
    row.update(extra)
    return row


async def _noop_bump(_ids):
    return None


async def _empty_decay(_backend):
    return {}


async def _fake_embed(_query):
    return [0.1] * 8


@pytest.mark.asyncio
async def test_search_memories_threads_recency_boost_to_semantic_backend(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.SEMANTIC_SCORE_COLUMN = "similarity"
    backend.memories.SEMANTIC_SCORE_METRIC = "cosine_similarity"
    backend.memories.configure_return("semantic_search", [_row("recent", "real query", similarity=0.90)])
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    resp = await memories_handler.search_memories(
        MemorySearchRequest(
            query="real query",
            semantic=True,
            boost_recency=True,
            recency_weight=0.42,
            min_margin=0.0,
        ),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert [m.id for m in resp.memories] == ["recent"]
    call = next(args for name, args in backend.memories.calls if name == "semantic_search")
    assert call["boost_recency"] is True
    assert call["recency_weight"] == pytest.approx(0.42)


class _PgRecord(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def items(self):
        return super().items()


class _PgConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows


@pytest.mark.asyncio
async def test_postgres_boosted_semantic_search_slices_reranked_results_and_preserves_similarity(monkeypatch):
    rows = [
        _PgRecord(id=f"row-{idx}", similarity=0.95 - idx * 0.01, _embedding_text="[1.0,0.0]", _recency_boost=0.0)
        for idx in range(5)
    ]
    conn = _PgConn(rows)
    tx = SimpleNamespace(conn=conn)
    repo = PostgresMemoryRepository()
    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda _tx: tx)
    monkeypatch.setattr(
        "mnemos.persistence.postgres._rerank_composite",
        lambda *_args, **_kwargs: [(idx, 1.0 - idx * 0.1) for idx in range(len(rows))],
    )

    result = await repo.semantic_search(
        tx,
        embedding=[1.0, 0.0],
        limit=2,
        visibility=VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=None,
        ),
        boost_recency=True,
        recency_weight=0.5,
    )

    assert [row["id"] for row in result] == ["row-0", "row-1"]
    assert len(result) == 2
    assert result[0]["similarity"] == pytest.approx(0.95)
    assert result[0]["_composite_score"] == pytest.approx(1.0)
