from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import METRIC_COSINE_SIMILARITY, MemorySearchRequest
from mnemos.domain.search.decay import DecayParams, apply_decay
from mnemos.persistence import SqliteBackend
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
from mnemos.persistence.sqlite import _execute
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
        "consolidated_into": None,
    }
    row.update(extra)
    return row


async def _noop_bump(_ids):
    return None


async def _empty_decay(_backend):
    return {}


async def _fake_embed(_query):
    return [1.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_route_forwards_boost_recency_and_changes_ordering(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.SEMANTIC_SCORE_COLUMN = "similarity"
    backend.memories.SEMANTIC_SCORE_METRIC = METRIC_COSINE_SIMILARITY

    old = _row("old", "restaurant inspections relevant", similarity=0.91)
    fresh = _row("fresh", "restaurant inspections recent", similarity=0.90)

    async def semantic_search(_tx, **kwargs):
        backend.memories.calls.append(("semantic_search", kwargs))
        rows = [old, fresh]
        if kwargs.get("boost_recency"):
            rows = [fresh, old]
        return [dict(r) for r in rows[: kwargs["limit"]]]

    monkeypatch.setattr(backend.memories, "semantic_search", semantic_search)
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    unboosted = await memories_handler.search_memories(
        MemorySearchRequest(query="restaurant inspections", semantic=True, min_score=0.0, boost_recency=False),
        user=_alice(),
    )
    boosted = await memories_handler.search_memories(
        MemorySearchRequest(
            query="restaurant inspections",
            semantic=True,
            min_score=0.0,
            boost_recency=True,
            recency_weight=0.8,
        ),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert [m.id for m in unboosted.memories] == ["old", "fresh"]
    assert [m.id for m in boosted.memories] == ["fresh", "old"]
    boosted_call = backend.memories.calls[-1][1]
    assert boosted_call["boost_recency"] is True
    assert boosted_call["recency_weight"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_recency_min_score_still_uses_raw_similarity(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.SEMANTIC_SCORE_COLUMN = "similarity"
    backend.memories.SEMANTIC_SCORE_METRIC = METRIC_COSINE_SIMILARITY
    backend.memories.configure_return(
        "semantic_search",
        [_row("weak_recent", "quantum gardening submarine", similarity=0.49, _composite_score=0.99)],
    )
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    resp = await memories_handler.search_memories(
        MemorySearchRequest(
            query="quantum gardening submarine",
            semantic=True,
            boost_recency=True,
            recency_weight=1.0,
            min_score=0.50,
        ),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert resp.count == 0
    assert resp.memories == []


def test_apply_decay_keeps_superseded_rows_behind_current():
    stale = _row("stale", "old", created="2026-06-14T00:00:00+00:00", consolidated_into="current")
    current = _row("current", "new", created="2026-06-01T00:00:00+00:00")
    stale_item = memories_handler._row_to_memory(stale)
    current_item = memories_handler._row_to_memory(current)

    out = apply_decay(
        [stale_item, current_item],
        {"facts": DecayParams(category="facts", half_life_days=1.0, decay_kind="exponential", floor=0.0)},
        now=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )

    assert [m.id for m in out] == ["current", "stale"]
    assert out[1].superseded_by == "current"


async def _insert_sqlite_memory(
    backend: SqliteBackend,
    tx,
    *,
    memory_id: str,
    content: str,
    updated: datetime,
) -> None:
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=80,
        owner_id="sqlite-owner",
        namespace="default",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=updated,
        updated=updated,
    )


@pytest.mark.asyncio
async def test_sqlite_boosted_recency_preserves_similarity_and_limit(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "boost-parity.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)
    now = datetime.now(timezone.utc)

    try:
        async with backend.transactional() as tx:
            await _insert_sqlite_memory(backend, tx, memory_id="old", content="old", updated=now - timedelta(days=30))
            await _insert_sqlite_memory(backend, tx, memory_id="new", content="new", updated=now)
            await backend.memories.upsert_memory_embedding(tx, "old", [1.0, 0.0, 0.0])
            await backend.memories.upsert_memory_embedding(tx, "new", [0.99, 0.1410673598, 0.0])

            rows = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=1,
                visibility=visibility,
                boost_recency=True,
                recency_weight=0.8,
            )
    finally:
        await backend.close()

    assert len(rows) == 1
    assert rows[0]["id"] == "new"
    assert rows[0]["similarity"] == pytest.approx(0.99, abs=1e-6)


@pytest.mark.asyncio
async def test_sqlite_boosted_recency_keeps_superseded_behind_current(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "boost-superseded.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)
    now = datetime.now(timezone.utc)

    try:
        async with backend.transactional() as tx:
            await _insert_sqlite_memory(backend, tx, memory_id="stale", content="stale", updated=now)
            await _insert_sqlite_memory(backend, tx, memory_id="current", content="current", updated=now - timedelta(days=30))
            await backend.memories.upsert_memory_embedding(tx, "stale", [1.0, 0.0, 0.0])
            await backend.memories.upsert_memory_embedding(tx, "current", [0.5, 0.8660254038, 0.0])
            await _execute(
                tx.conn,
                "UPDATE memories SET consolidated_into = ? WHERE id = ?",
                ("current", "stale"),
            )

            rows = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=1,
                visibility=visibility,
                boost_recency=True,
                recency_weight=1.0,
            )
    finally:
        await backend.close()

    assert [row["id"] for row in rows] == ["current"]


class _PgRow(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def items(self):
        return super().items()


class _PgConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, _sql, *params):
        self.params = params
        return self.rows


class _PgTx(PostgresTransaction):
    def __init__(self, rows):
        self._conn = _PgConn(rows)
        self._tx = None
        self._closed = False
        self._after_commit = []


@pytest.mark.asyncio
async def test_postgres_boosted_rerank_slices_to_limit_and_preserves_similarity():
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 2
    rows = [
        _PgRow(id="a", similarity=0.90, _embedding_text="[0.90,0.4358898944]", _recency_boost=0.0, consolidated_into=None),
        _PgRow(id="b", similarity=0.89, _embedding_text="[0.89,0.4559605246]", _recency_boost=1.0, consolidated_into=None),
        _PgRow(id="c", similarity=0.88, _embedding_text="[0.88,0.4749736835]", _recency_boost=0.9, consolidated_into=None),
    ]
    tx = _PgTx(rows)
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)

    out = await repo.semantic_search(
        tx,
        embedding=[1.0, 0.0],
        limit=1,
        visibility=visibility,
        boost_recency=True,
        recency_weight=0.8,
    )

    assert len(out) == 1
    assert out[0]["id"] == "b"
    assert out[0]["similarity"] == pytest.approx(0.89)
    assert out[0]["_composite_score"] > out[0]["similarity"]


@pytest.mark.asyncio
async def test_postgres_boosted_rerank_keeps_superseded_behind_current():
    repo = PostgresMemoryRepository()
    repo._expected_embedding_dim = 2
    rows = [
        _PgRow(
            id="stale",
            similarity=1.0,
            _embedding_text="[1.0,0.0]",
            _recency_boost=1.0,
            consolidated_into="current",
        ),
        _PgRow(
            id="current",
            similarity=0.5,
            _embedding_text="[0.5,0.8660254038]",
            _recency_boost=0.0,
            consolidated_into=None,
        ),
    ]
    tx = _PgTx(rows)
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)

    out = await repo.semantic_search(
        tx,
        embedding=[1.0, 0.0],
        limit=1,
        visibility=visibility,
        boost_recency=True,
        recency_weight=1.0,
    )

    assert [row["id"] for row in out] == ["current"]
