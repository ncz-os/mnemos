"""Db2-native ``semantic_search`` SQL-dialect probes.

Driver-free, DB-free tests that assert the post-translation SQL emitted
by ``Db2MemoryRepository.semantic_search`` engages the DiskANN vector
index (``EUCLIDEAN`` distance + ``FETCH APPROX FIRST K ROWS ONLY``) and
respects the ``MNEMOS_DB2_VECTOR_INDEX`` mode toggle.

These probes do NOT require ``ibm_db`` to be installed, nor a live
Db2 DSN — they capture the SQL via a fake sync cursor passed into
``_Db2AsyncCursor``. The live parity smoke probes are in
``tests/test_db2_live.py`` and gate on ``DB2_DSN``.

The load-bearing finding being asserted here:

Before this override, ``Db2MemoryRepository`` inherited
``OracleMemoryRepository.semantic_search`` verbatim — Oracle SQL with
``VECTOR_DISTANCE(..., COSINE)`` and ``FETCH FIRST K ROWS ONLY``, which
DOES NOT engage the Db2 12.1.5 DiskANN vector index. The app path
silently fell back to exact scan even when the operator had set
``DB2_VECTOR_INDEXING=YES`` and migrated the vector index.

The override emits ``VECTOR_DISTANCE(..., EUCLIDEAN)`` (the metric
supported by the 12.1.5 EAP vector index) and
``FETCH APPROX FIRST K ROWS ONLY`` (the syntax that engages the
DiskANN index). For L2-normalized embeddings (MNEMOS default) the
EUCLIDEAN top-K ordering is identical to COSINE.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest


def _capture_db2_translated_sql(repo, *, mode: str | None) -> str:
    """Drive ``Db2MemoryRepository.semantic_search`` through a fake
    cursor and return the post-translation SQL.
    """
    from mnemos.persistence.db2 import _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, str] = {}

    class _FakeSyncCursor:
        description = (
            ("id",),
            ("content",),
            ("category",),
            ("subcategory",),
            ("metadata",),
            ("quality_rating",),
            ("compressed_content",),
            ("verbatim_content",),
            ("owner_id",),
            ("namespace",),
            ("permission_mode",),
            ("source_model",),
            ("source_provider",),
            ("source_session",),
            ("source_agent",),
            ("group_id",),
            ("created",),
            ("updated",),
            ("archived_at",),
            ("recall_count",),
            ("last_recalled_at",),
            ("rank_score",),
        )
        rowcount = 0

        def execute(self, sql, params=None):  # noqa: D401
            captured["sql"] = sql

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")

    if mode is None:
        os.environ.pop("MNEMOS_DB2_VECTOR_INDEX", None)
    else:
        os.environ["MNEMOS_DB2_VECTOR_INDEX"] = mode

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.1, 0.2, 0.3],
                limit=5,
                visibility=visibility,
            )
        )
    finally:
        loop.close()
    return captured["sql"]


def _capture_db2_fetch_count(repo, *, boost_recency: bool, limit: int = 5) -> int:
    """Drive ``Db2MemoryRepository.semantic_search`` through a fake cursor and
    return the row-count bound to the FETCH clause (the last execute param).
    """
    from mnemos.persistence.db2 import _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, object] = {}

    class _FakeSyncCursor:
        description = (("id",), ("content",), ("updated",), ("rank_score",))
        rowcount = 0

        def execute(self, sql, params=None):
            captured["params"] = params

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")

    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "approx"
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.1, 0.2, 0.3],
                limit=limit,
                visibility=visibility,
                boost_recency=boost_recency,
            )
        )
    finally:
        loop.close()
    params = captured["params"]
    # Execute binds: (select_vec, *where, order_vec, fetch_count) — the FETCH
    # row-count is always the final positional parameter.
    return int(params[-1])


def test_db2_semantic_search_overfetches_candidates_for_recency() -> None:
    """Recency boost must widen the DiskANN candidate fetch beyond ``limit`` so
    a newer memory just outside the top-``limit`` by distance can be promoted;
    without boost the fetch is exactly ``limit``. Mirrors PostgresBackend.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository

    repo = Db2MemoryRepository()
    assert _capture_db2_fetch_count(repo, boost_recency=False, limit=5) == 5
    # candidate_limit = max(limit, min(limit*4, 200)) = 20 for limit=5
    assert _capture_db2_fetch_count(repo, boost_recency=True, limit=5) == 20
    # capped at 200 for large limits
    assert _capture_db2_fetch_count(repo, boost_recency=True, limit=100) == 200


def test_db2_semantic_search_recency_rerank_sorts_invalid_scores_last() -> None:
    """Recency rerank must never promote unscored rows ahead of finite
    Db2 vector distances.
    """
    from datetime import datetime, timedelta, timezone

    from mnemos.persistence.db2 import Db2MemoryRepository, _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    today = datetime.now(timezone.utc).date()
    old = today - timedelta(days=30)
    fetched_rows = [
        ("valid-old-best", "a", old, 0.20),
        ("valid-old-next", "b", old, 0.25),
        ("valid-fresh", "c", today, 0.31),
        ("rank-none", "none", today, None),
        ("rank-invalid", "bad", today, "bad-score"),
        ("rank-nan", "nan", today, "nan"),
        ("valid-fresh-late", "d", today, 0.50),
    ]

    class _FakeSyncCursor:
        description = (("id",), ("content",), ("updated",), ("rank_score",))
        rowcount = len(fetched_rows)

        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return fetched_rows

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")
    repo = Db2MemoryRepository()
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "approx"

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.1, 0.2, 0.3],
                limit=3,
                visibility=visibility,
                boost_recency=True,
                recency_weight=0.1,
            )
        )
    finally:
        loop.close()

    ids = [row["id"] for row in result]
    assert ids == ["valid-old-best", "valid-fresh", "valid-old-next"]
    assert {"rank-none", "rank-invalid", "rank-nan"}.isdisjoint(ids)
    assert [row["rank_score"] for row in result] == sorted(row["rank_score"] for row in result)
    assert len(result) <= 3


def _capture_db2_fts_sql(repo, *, text_mode: str | None) -> str:
    """Drive ``Db2MemoryRepository.fts_search`` through a fake cursor and return
    the emitted SQL, under the given MNEMOS_DB2_TEXT_SEARCH mode.
    """
    from mnemos.persistence.db2 import _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, str] = {}

    class _FakeSyncCursor:
        description = (
            ("id",),
            ("content",),
            ("category",),
            ("subcategory",),
            ("metadata",),
            ("quality_rating",),
            ("owner_id",),
            ("namespace",),
            ("created",),
            ("updated",),
        )
        rowcount = 0

        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")

    if text_mode is None:
        os.environ.pop("MNEMOS_DB2_TEXT_SEARCH", None)
    else:
        os.environ["MNEMOS_DB2_TEXT_SEARCH"] = text_mode

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(repo.fts_search(tx, query="needle", limit=5, visibility=visibility))
    finally:
        loop.close()
        os.environ.pop("MNEMOS_DB2_TEXT_SEARCH", None)
    return captured["sql"]


def test_db2_fts_defaults_to_like_scan() -> None:
    """Without the toggle, FTS uses the stock LIKE substring scan (no Text
    Search server required)."""
    from mnemos.persistence.db2 import Db2MemoryRepository

    sql = _capture_db2_fts_sql(Db2MemoryRepository(), text_mode=None).upper()
    assert "LIKE" in sql
    assert "CONTAINS(" not in sql


def test_db2_fts_contains_mode_engages_text_index() -> None:
    """MNEMOS_DB2_TEXT_SEARCH=contains emits the native CONTAINS() predicate
    that engages the Db2 Text Search index."""
    from mnemos.persistence.db2 import Db2MemoryRepository

    sql = _capture_db2_fts_sql(Db2MemoryRepository(), text_mode="contains").upper()
    assert "CONTAINS(M.CONTENT, ?) = 1" in sql
    assert "LIKE" not in sql


def test_db2_fts_invalid_mode_falls_back_to_like() -> None:
    """An unrecognized mode warns and falls back to the safe LIKE scan."""
    from mnemos.persistence.db2 import Db2MemoryRepository

    sql = _capture_db2_fts_sql(Db2MemoryRepository(), text_mode="bogus").upper()
    assert "LIKE" in sql
    assert "CONTAINS(" not in sql


def test_db2_semantic_search_uses_native_dialect() -> None:
    """Default mode emits EUCLIDEAN + FETCH APPROX FIRST — engages
    the Db2 12.1.5 DiskANN vector index.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository

    repo = Db2MemoryRepository()
    sql = _capture_db2_translated_sql(repo, mode="approx")
    sql_u = sql.upper()
    assert "FETCH APPROX FIRST" in sql_u, sql
    assert "EUCLIDEAN" in sql_u, sql
    assert "COSINE" not in sql_u, sql
    assert ":" not in sql, sql


def test_db2_semantic_search_exact_mode_falls_back() -> None:
    """``MNEMOS_DB2_VECTOR_INDEX=exact`` produces an exact-scan
    ``FETCH FIRST`` (no ``APPROX``) while still using EUCLIDEAN.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository

    repo = Db2MemoryRepository()
    sql = _capture_db2_translated_sql(repo, mode="exact")
    sql_u = sql.upper()
    assert "FETCH FIRST" in sql_u
    assert "FETCH APPROX FIRST" not in sql_u
    assert "EUCLIDEAN" in sql_u
    assert ":" not in sql


def test_db2_semantic_search_runs_on_native_cursor() -> None:
    """Native dialect uses pass-through cursors, so semantic_search must
    emit positional Db2 SQL directly rather than relying on compat
    Oracle-bind translation.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository, _Db2NativeAsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, object] = {}

    class _FakeSyncCursor:
        description = (
            ("id",),
            ("content",),
            ("category",),
            ("subcategory",),
            ("metadata",),
            ("quality_rating",),
            ("compressed_content",),
            ("verbatim_content",),
            ("owner_id",),
            ("namespace",),
            ("permission_mode",),
            ("source_model",),
            ("source_provider",),
            ("source_session",),
            ("source_agent",),
            ("group_id",),
            ("created",),
            ("updated",),
            ("archived_at",),
            ("recall_count",),
            ("last_recalled_at",),
            ("rank_score",),
        )
        rowcount = 0

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return []

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2NativeAsyncCursor(_FakeSyncCursor())

    visibility = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        namespace="ns1",
        group_ids=frozenset(),
    )
    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "approx"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.1, 0.2, 0.3],
                limit=5,
                visibility=visibility,
                category="notes",
            )
        )
    finally:
        loop.close()

    sql = str(captured["sql"])
    params = captured["params"]
    assert ":" not in sql
    assert "VECTOR(?, 3, FLOAT32)" in sql
    assert "FETCH APPROX FIRST ? ROWS ONLY" in sql
    assert params == ("[0.1000000,0.2000000,0.3000000]", "alice", "ns1", "notes", "[0.1000000,0.2000000,0.3000000]", 5)


def test_db2_semantic_search_filters_preserved() -> None:
    """Visibility / deleted_at / archived_at / category / subcategory /
    source_provider / source_model / source_agent filters from the
    Oracle implementation must all be preserved in the override.
    """
    from mnemos.persistence.db2 import Db2MemoryRepository, _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, str] = {}

    class _FakeSyncCursor:
        description = (("id",),)
        rowcount = 0

        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="ns1")
    repo = Db2MemoryRepository()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.1, 0.2, 0.3],
                limit=10,
                visibility=visibility,
                category="cat-x",
                subcategory="sub-y",
                source_provider="prov-z",
                source_model="mdl-a",
                source_agent="agt-b",
            )
        )
    finally:
        loop.close()

    sql_u = captured["sql"].upper()
    assert "DELETED_AT IS NULL" in sql_u
    assert "ARCHIVED_AT IS NULL" in sql_u
    assert "CATEGORY" in sql_u
    assert "SUBCATEGORY" in sql_u
    assert "SOURCE_PROVIDER" in sql_u
    assert "SOURCE_MODEL" in sql_u
    assert "SOURCE_AGENT" in sql_u


def test_db2_semantic_search_recency_boost_keeps_index_engaged() -> None:
    """With ``boost_recency=True``, ``ORDER BY`` should still be the
    bare ``VECTOR_DISTANCE(..., EUCLIDEAN)`` expression — the recency
    adjustment runs in Python after fetch. This keeps the DiskANN
    index engaged (the optimizer can't push the recency arithmetic
    through the index).
    """
    from mnemos.persistence.db2 import Db2MemoryRepository, _Db2AsyncCursor
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    captured: dict[str, str] = {}

    class _FakeSyncCursor:
        description = (
            ("id",),
            ("content",),
            ("category",),
            ("subcategory",),
            ("metadata",),
            ("quality_rating",),
            ("compressed_content",),
            ("verbatim_content",),
            ("owner_id",),
            ("namespace",),
            ("permission_mode",),
            ("source_model",),
            ("source_provider",),
            ("source_session",),
            ("source_agent",),
            ("group_id",),
            ("created",),
            ("updated",),
            ("archived_at",),
            ("recall_count",),
            ("last_recalled_at",),
            ("rank_score",),
        )
        rowcount = 0

        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

    tx = SimpleNamespace(conn=_FakeConn())
    visibility = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")
    repo = Db2MemoryRepository()
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "approx"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            repo.semantic_search(
                tx,
                embedding=[0.5, 0.5, 0.5],
                limit=5,
                visibility=visibility,
                boost_recency=True,
                recency_weight=0.2,
            )
        )
    finally:
        loop.close()

    sql_u = captured["sql"].upper()
    # The rank expression must NOT include the recency-arithmetic
    # subtraction — that's applied post-fetch in Python so the
    # ORDER BY stays index-friendly.
    assert "ORDER BY" in sql_u
    # No "- :W *" or "CURRENT DATE" arithmetic in the rank expression.
    assert "CURRENT DATE" not in sql_u
    assert "SYSDATE" not in sql_u
    assert "FETCH APPROX FIRST" in sql_u


def test_db2_resolve_vector_index_mode_default_and_fallback() -> None:
    """The mode resolver returns ``approx`` by default, honours the
    env var, and falls back to ``approx`` on invalid input.
    """
    from mnemos.persistence.db2 import _resolve_db2_vector_index_mode

    # Default
    os.environ.pop("MNEMOS_DB2_VECTOR_INDEX", None)
    assert _resolve_db2_vector_index_mode(None) == "approx"

    # Explicit exact
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "exact"
    assert _resolve_db2_vector_index_mode(None) == "exact"

    # Case-insensitive
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "APPROX"
    assert _resolve_db2_vector_index_mode(None) == "approx"

    # Invalid → fallback
    os.environ["MNEMOS_DB2_VECTOR_INDEX"] = "diskann"
    assert _resolve_db2_vector_index_mode(None) == "approx"

    # Settings fallback
    os.environ.pop("MNEMOS_DB2_VECTOR_INDEX", None)
    settings = SimpleNamespace(db2_vector_index="exact")
    assert _resolve_db2_vector_index_mode(settings) == "exact"


def test_db2_backend_init_propagates_settings_to_memory_repo() -> None:
    """``Db2Backend.__init__`` must thread ``settings`` to
    ``Db2MemoryRepository._settings`` so the override can read
    ``settings.db2_vector_index`` (env var still wins).
    """
    from mnemos.persistence.db2 import Db2Backend, Db2MemoryRepository

    fake_pool = SimpleNamespace()
    settings = SimpleNamespace(db2_vector_index="exact")
    backend = Db2Backend(fake_pool, settings)
    assert isinstance(backend._memories_repo, Db2MemoryRepository)
    assert backend._memories_repo._settings is settings


def test_db2_backend_is_vector_indexing_enabled_default_false() -> None:
    """Before ``open()`` runs the probe, the property is ``False`` so
    health-check surfaces the operator-action warning by default.
    """
    from mnemos.persistence.db2 import Db2Backend

    fake_pool = SimpleNamespace()
    backend = Db2Backend(fake_pool, SimpleNamespace())
    assert backend.is_vector_indexing_enabled is False
    assert backend._db2_vector_indexing_value is None


@pytest.mark.asyncio
async def test_db2_backend_open_probe_warns_when_registry_not_yes(caplog) -> None:
    """Open hook runs the probe; when the registry var is not 'YES'
    the property reads False and a clear WARNING is logged.
    """
    import logging

    from mnemos.persistence.db2 import Db2Backend, _Db2AsyncCursor

    class _FakeSyncCursor:
        description = (("REG_VAR_VALUE",),)
        rowcount = 1

        def execute(self, sql, params=None):  # noqa: D401
            # Echo the captured SQL for sanity; result is fetched below.
            self._last = sql

        def fetchone(self):
            return ("NO",)

        def fetchall(self):
            return [("NO",)]

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

        async def close(self):
            return None

    class _FakePool:
        def __init__(self):
            self._conn = _FakeConn()

        def acquire(self):
            outer = self

            class _CM:
                async def __aenter__(self_inner):
                    return outer._conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _CM()

        async def close(self):
            return None

    pool = _FakePool()
    backend = Db2Backend(pool, SimpleNamespace())
    caplog.set_level(logging.WARNING, logger="mnemos.persistence.db2")
    await backend.open()
    assert backend.is_vector_indexing_enabled is False
    assert backend._db2_vector_indexing_value == "NO"
    assert any("DB2_VECTOR_INDEXING" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_db2_backend_open_probe_yes_clears_warning(caplog) -> None:
    """When the registry var is 'YES', the probe records the value and
    the property reads True; no warning is logged at WARNING level.
    """
    import logging

    from mnemos.persistence.db2 import Db2Backend, _Db2AsyncCursor

    class _FakeSyncCursor:
        description = (("REG_VAR_VALUE",),)
        rowcount = 1

        def execute(self, sql, params=None):
            self._last = sql

        def fetchone(self):
            return ("YES",)

        def fetchall(self):
            return [("YES",)]

        def close(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _Db2AsyncCursor(_FakeSyncCursor())

        async def close(self):
            return None

    class _FakePool:
        def acquire(self):
            outer_conn = _FakeConn()

            class _CM:
                async def __aenter__(self_inner):
                    return outer_conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _CM()

        async def close(self):
            return None

    pool = _FakePool()
    backend = Db2Backend(pool, SimpleNamespace())
    caplog.set_level(logging.WARNING, logger="mnemos.persistence.db2")
    await backend.open()
    assert backend.is_vector_indexing_enabled is True
    assert backend._db2_vector_indexing_value == "YES"
    assert not any(
        "DB2_VECTOR_INDEXING" in record.getMessage() and record.levelno >= logging.WARNING for record in caplog.records
    )
