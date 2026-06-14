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
from mnemos.persistence.db2 import Db2MemoryRepository, _Db2AsyncCursor
from mnemos.persistence.mysql import MysqlMemoryRepository
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


_SEMANTIC_RERANK_COLUMNS = (
    "id",
    "content",
    "category",
    "subcategory",
    "metadata",
    "quality_rating",
    "compressed_content",
    "verbatim_content",
    "owner_id",
    "namespace",
    "permission_mode",
    "source_model",
    "source_provider",
    "source_session",
    "source_agent",
    "group_id",
    "created",
    "updated",
    "archived_at",
    "recall_count",
    "last_recalled_at",
    "consolidated_into",
    "rank_score",
)

_MYSQL_FALLBACK_COLUMNS = (*_SEMANTIC_RERANK_COLUMNS[:-1], "embedding_json")


def _semantic_tuple(
    memory_id: str,
    *,
    updated: datetime,
    consolidated_into: str | None,
    rank_score: float | None = None,
    embedding_json: str | None = None,
) -> tuple:
    return (
        memory_id,
        memory_id,
        "facts",
        None,
        "{}",
        80,
        None,
        memory_id,
        "alice",
        "alice-ns",
        600,
        None,
        None,
        None,
        None,
        None,
        updated,
        updated,
        None,
        0,
        None,
        consolidated_into,
        embedding_json if embedding_json is not None else rank_score,
    )


class _MysqlCursor:
    rowcount = 0

    def __init__(self, rows: list[tuple], columns: tuple[str, ...]) -> None:
        self._rows = rows
        self.description = tuple((column,) for column in columns)

    async def __aenter__(self) -> "_MysqlCursor":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        return None

    async def execute(self, _sql: str, _params) -> None:
        return None

    async def fetchall(self) -> list[tuple]:
        return self._rows


class _MysqlConn:
    def __init__(self, rows: list[tuple], columns: tuple[str, ...]) -> None:
        self._rows = rows
        self._columns = columns

    def cursor(self) -> _MysqlCursor:
        return _MysqlCursor(self._rows, self._columns)


class _Db2SyncCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.description = tuple((column,) for column in _SEMANTIC_RERANK_COLUMNS)
        self.rowcount = len(rows)

    def execute(self, _sql: str, _params=None) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self):
        return None

    def close(self) -> None:
        return None


class _Db2Conn:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def cursor(self) -> _Db2AsyncCursor:
        return _Db2AsyncCursor(_Db2SyncCursor(self._rows))


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


@pytest.mark.asyncio
async def test_boosted_route_ood_gate_still_rejects_flat_unanchored_results(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.SEMANTIC_SCORE_COLUMN = "similarity"
    backend.memories.SEMANTIC_SCORE_METRIC = METRIC_COSINE_SIMILARITY
    backend.memories.configure_return(
        "semantic_search",
        [
            _row(
                f"flat_{idx}",
                "florida restaurant inspection records",
                similarity=0.72 - 0.0005 * idx,
                _composite_score=0.99,
            )
            for idx in range(10)
        ],
    )
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    resp = await memories_handler.search_memories(
        MemorySearchRequest(
            query="asdfqwer zxcv blorf",
            semantic=True,
            boost_recency=True,
            recency_weight=1.0,
        ),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert resp.count == 0
    assert resp.memories == []


@pytest.mark.asyncio
async def test_boosted_route_keeps_current_order_when_superseded_row_triggers_decay(monkeypatch):
    backend = install_fake_backend(monkeypatch)
    backend.memories.SEMANTIC_SCORE_COLUMN = "similarity"
    backend.memories.SEMANTIC_SCORE_METRIC = METRIC_COSINE_SIMILARITY
    backend.memories.configure_return(
        "semantic_search",
        [
            _row("fresh", "restaurant inspections recent", similarity=0.90),
            _row("old", "restaurant inspections relevant", similarity=0.91),
            _row("stale", "restaurant inspections stale", similarity=0.99, consolidated_into="fresh"),
        ],
    )
    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    resp = await memories_handler.search_memories(
        MemorySearchRequest(
            query="restaurant inspections",
            semantic=True,
            min_score=0.0,
            boost_recency=True,
            recency_weight=1.0,
        ),
        user=_alice(),
    )
    await asyncio.sleep(0)

    assert [m.id for m in resp.memories] == ["fresh", "old", "stale"]
    assert resp.memories[-1].superseded_by == "fresh"


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


@pytest.mark.asyncio
async def test_mysql_boosted_rerank_keeps_superseded_behind_current():
    repo = MysqlMemoryRepository()
    repo._expected_embedding_dim = 2
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)
    now = datetime.now(timezone.utc)
    rows = [
        _semantic_tuple("stale", updated=now, consolidated_into="current", rank_score=0.0),
        _semantic_tuple("current", updated=now - timedelta(days=30), consolidated_into=None, rank_score=0.5),
    ]
    tx = SimpleNamespace(conn=_MysqlConn(rows, _SEMANTIC_RERANK_COLUMNS))

    out = await repo.semantic_search(
        tx,
        embedding=[1.0, 0.0],
        limit=1,
        visibility=visibility,
        boost_recency=True,
        recency_weight=1.0,
    )

    assert [row["id"] for row in out] == ["current"]


@pytest.mark.asyncio
async def test_mysql_python_cosine_boosted_rerank_keeps_superseded_behind_current():
    repo = MysqlMemoryRepository()
    now = datetime.now(timezone.utc)
    rows = [
        _semantic_tuple("stale", updated=now, consolidated_into="current", embedding_json="[1.0,0.0]"),
        _semantic_tuple(
            "current",
            updated=now - timedelta(days=30),
            consolidated_into=None,
            embedding_json="[0.5,0.8660254038]",
        ),
    ]
    tx = SimpleNamespace(conn=_MysqlConn(rows, _MYSQL_FALLBACK_COLUMNS))

    out = await repo._python_cosine_search(
        tx,
        vec_literal="[1.0,0.0]",
        where=["1 = 1"],
        params=[],
        limit=1,
        boost_recency=True,
        recency_weight=1.0,
    )

    assert [row["id"] for row in out] == ["current"]


@pytest.mark.asyncio
async def test_db2_boosted_rerank_keeps_superseded_behind_current():
    repo = Db2MemoryRepository()
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None)
    now = datetime.now(timezone.utc)
    rows = [
        _semantic_tuple("stale", updated=now, consolidated_into="current", rank_score=0.0),
        _semantic_tuple("current", updated=now - timedelta(days=30), consolidated_into=None, rank_score=0.5),
    ]
    tx = SimpleNamespace(conn=_Db2Conn(rows))

    out = await repo.semantic_search(
        tx,
        embedding=[1.0, 0.0],
        limit=1,
        visibility=visibility,
        boost_recency=True,
        recency_weight=1.0,
    )

    assert [row["id"] for row in out] == ["current"]


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
