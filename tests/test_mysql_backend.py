from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mnemos.persistence.base import (
    ALL_CAPABILITIES,
    CONSULTATIONS_CAPABILITY,
    CORE_CAPABILITY,
    FEDERATION_CAPABILITY,
    OAUTH_CAPABILITY,
    STATE_CAPABILITY,
    CorePersistence,
)
from mnemos.persistence.mariadb import MariadbBackend
from mnemos.persistence.mysql import MysqlBackend, MysqlMemoryRepository
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


pytestmark = pytest.mark.asyncio


class _AsyncCursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _tx_for_cursor(cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    return SimpleNamespace(conn=conn)


class _AsyncPoolContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    def acquire(self):
        return _AsyncPoolContext(self._conn)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


async def test_mysql_backend_advertises_implemented_capabilities_and_pings():
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,))
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    backend = MysqlBackend(_FakePool(conn), SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))

    # OAuth now shares the same backend; this remains a strict subset of ALL.
    assert backend.capabilities == {CORE_CAPABILITY, STATE_CAPABILITY, FEDERATION_CAPABILITY, OAUTH_CAPABILITY}
    assert backend.capabilities != set(ALL_CAPABILITIES)
    assert isinstance(backend, CorePersistence)
    assert await backend.ping() is True


async def test_mysql_and_mariadb_omit_consultations_capability():
    """MySQL/MariaDB do not implement the GRAEAE ``consultations``
    capability. Pin this so a future change adding the capability
    cannot silently re-enable GRAEAE on those backends without first
    wiring the consultations repository.

    The documented MySQL/MariaDB deployment commands rely on this
    contract: with the default GRAEAE-on flag, the lifecycle logs a
    warning and disables the unsupported layer rather than refusing
    to start (see test_unsupported_layers_are_disabled_rather_than_blocking_startup
    in test_layered_install.py). Operators who want GRAEAE on MySQL
    need a backend that advertises consultations.
    """
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,))
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    mysql = MysqlBackend(_FakePool(conn), SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
    mariadb = MariadbBackend(_FakePool(conn), SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))

    assert CONSULTATIONS_CAPABILITY not in mysql.capabilities
    assert CONSULTATIONS_CAPABILITY not in mariadb.capabilities


async def test_mysql_open_propagates_schema_provisioning_failure():
    cursor = MagicMock()
    cursor.execute = AsyncMock(side_effect=RuntimeError("ddl denied"))
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    backend = MysqlBackend(_FakePool(conn), SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))

    with pytest.raises(RuntimeError, match="ddl denied"):
        await backend.open()


async def test_mysql_semantic_search_without_recency_uses_visibility_params_once():
    repo = MysqlMemoryRepository()
    repo._expected_embedding_dim = 3
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    captured: dict[str, object] = {}

    async def execute(sql, params):
        captured["sql"] = sql
        captured["params"] = tuple(params)

    cursor.execute = AsyncMock(side_effect=execute)
    tx = _tx_for_cursor(cursor)
    embedder = MagicMock()
    embedder.embed.return_value = [0.25, 0.5, 0.75]

    with patch("mnemos.core.lifecycle.get_embedder", return_value=embedder, create=True):
        from mnemos.core import lifecycle

        embedding = lifecycle.get_embedder().embed("needle")

    await repo.semantic_search(
        tx,
        embedding=embedding,
        limit=7,
        visibility=VisibilityFilter(
            scope=VisibilityScope.OWN_ONLY,
            user_id="alice",
            namespace="ns1",
            group_ids=frozenset(),
        ),
        boost_recency=False,
    )

    sql = str(captured["sql"])
    compact_sql = " ".join(sql.split()).lower()
    params = captured["params"]

    assert "recency" not in compact_sql
    assert "timestampdiff" not in compact_sql
    assert "created_at" not in compact_sql
    assert compact_sql.count("vector_distance(m.embedding, to_vector(%s), 'cosine')") == 1
    assert "vec_fromtext" not in compact_sql
    assert "vec_distance_cosine" not in compact_sql
    assert "where" in compact_sql
    assert "m.owner_id = %s and m.namespace = %s" in compact_sql
    assert "order by rank_score asc" in compact_sql
    assert params == ("[0.2500000,0.5000000,0.7500000]", "alice", "ns1", 7)
    assert len(params) == 4
    assert params[1:3] == ("alice", "ns1")
    embedder.embed.assert_called_once_with("needle")


async def test_mysql_fetch_memory_context_tolerates_non_root_without_namespace(monkeypatch):
    """Regression: ``fetch_memory_context`` must not assume ``user.namespace``.

    The method is typed ``user: Any`` and is reachable with a duck-typed
    caller (the Db2/Oracle siblings already defend with ``getattr``).
    Dereferencing ``user.namespace`` directly raised ``AttributeError`` for
    any non-root caller that omitted the field instead of falling back to
    ``"default"``.
    """
    from mnemos.core import lifecycle

    async def _fake_embedding(_query):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(lifecycle, "_get_embedding", _fake_embedding)

    repo = MysqlMemoryRepository()
    captured: dict[str, object] = {}

    async def _fake_semantic_search(tx, *, embedding, limit, visibility):
        captured["visibility"] = visibility
        captured["limit"] = limit
        return [{"id": "m1", "content": "ctx", "namespace": "default"}]

    monkeypatch.setattr(repo, "semantic_search", _fake_semantic_search)

    user = SimpleNamespace(user_id="bob", group_ids=[], role="user")  # no .namespace
    rows = await repo.fetch_memory_context(SimpleNamespace(), "needle", user, limit=3)

    assert rows == [{"id": "m1", "content": "ctx", "namespace": "default"}]
    vis = captured["visibility"]
    assert isinstance(vis, VisibilityFilter)
    assert vis.scope is VisibilityScope.READABLE
    assert vis.user_id == "bob"
    # Fallback is "default" — not None (which would raise in for_read) and
    # not silently dropped.
    assert vis.namespace == "default"
    assert captured["limit"] == 3


async def test_mysql_fetch_memory_context_root_reads_cross_tenant(monkeypatch):
    """Root keeps the cross-tenant read; the vault subtraction still applies."""
    from mnemos.core import lifecycle

    async def _fake_embedding(_query):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(lifecycle, "_get_embedding", _fake_embedding)

    repo = MysqlMemoryRepository()
    captured: dict[str, object] = {}

    async def _fake_semantic_search(tx, *, embedding, limit, visibility):
        captured["visibility"] = visibility
        return []

    monkeypatch.setattr(repo, "semantic_search", _fake_semantic_search)

    root = SimpleNamespace(user_id="root", group_ids=[], role="root", namespace="root-ns")
    await repo.fetch_memory_context(SimpleNamespace(), "needle", root, limit=5)

    vis = captured["visibility"]
    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None
