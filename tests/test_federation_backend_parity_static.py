"""Static cross-backend parity checks for federation visibility/feed SQL.

These tests are intentionally driver-free: they import renderer helpers and
exercise DB2 repository SQL through fake cursors so parity regressions are
caught without live Postgres/MySQL/Oracle/Db2 services.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def test_every_backend_visibility_render_subtracts_exclude_namespaces() -> None:
    """Vault exclusions must survive default, READABLE, and OWN_ONLY scopes."""
    from mnemos.persistence.db2 import _render_visibility as db2_render_visibility
    from mnemos.persistence.mysql import _render_visibility as mysql_render_visibility
    from mnemos.persistence.oracle import _render_visibility as oracle_render_visibility
    from mnemos.persistence.postgres import _render_postgres_visibility
    from mnemos.persistence.sqlite import _render_sqlite_visibility

    readable = VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id="alice",
        group_ids=("team-a",),
        namespace="default",
        exclude_namespaces=("vault",),
    )
    own_only = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        group_ids=(),
        namespace="default",
        exclude_namespaces=("vault",),
    )
    default_root = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=("vault",),
    )

    sqlite_params: list[Any] = []
    sqlite_readable = _render_sqlite_visibility(readable, sqlite_params, table_alias="m")
    assert "m.namespace NOT IN (?)" in sqlite_readable
    assert sqlite_params[-1] == "vault"

    sqlite_params = []
    sqlite_own = _render_sqlite_visibility(own_only, sqlite_params, table_alias="m")
    assert "m.owner_id = ?" in sqlite_own
    assert "m.namespace NOT IN (?)" in sqlite_own
    assert sqlite_params == ["alice", "default", "vault"]

    sqlite_params = []
    sqlite_root = _render_sqlite_visibility(default_root, sqlite_params, table_alias="m")
    assert "m.namespace NOT IN (?)" in sqlite_root
    assert sqlite_params == ["vault"]

    oracle_clause, oracle_params = oracle_render_visibility(readable, table_alias="m", param_prefix="v")
    assert "m.namespace NOT IN (:v_xns_0)" in oracle_clause
    assert oracle_params["v_xns_0"] == "vault"

    db2_clause, db2_params = db2_render_visibility(own_only, table_alias="m", param_prefix="d")
    assert "m.owner_id = :d_owner" in db2_clause
    assert "m.namespace NOT IN (:d_xns_0)" in db2_clause
    assert db2_params["d_xns_0"] == "vault"

    mysql_clause, mysql_params = mysql_render_visibility(readable, table_alias="m")
    assert "m.namespace = %s" in mysql_clause
    assert "m.namespace NOT IN (%s)" in mysql_clause
    assert mysql_params[-2:] == ["default", "vault"]

    pg_clause, pg_params, _ = _render_postgres_visibility(readable, table_alias="m")
    assert "m.namespace=$" in pg_clause
    assert "m.namespace NOT IN ($" in pg_clause
    assert pg_params[-2:] == ["default", "vault"]


@pytest.mark.asyncio
async def test_db2_feed_queries_require_public_readable_and_exclude_vault() -> None:
    """Db2 feed SQL must match the public-readable gate used by PG/MySQL."""
    from mnemos.core.secret_detection import VAULT_NAMESPACE
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": tuple(params or ())})

        async def fetchall(self) -> list[Any]:
            return []

        async def fetchone(self) -> Any:
            return None

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()

    await repo.feed_query(
        tx,
        since_updated=None,
        since_id=None,
        namespaces=[],
        categories=[],
        limit=25,
        prefer_compressed=False,
    )
    await repo.get_feed_memory(tx, "mem-1", namespaces=[], categories=[])

    feed_sql = calls[0]["sql"].upper()
    feed_params = calls[0]["params"]
    get_sql = calls[1]["sql"].upper()
    get_params = calls[1]["params"]

    for sql in (feed_sql, get_sql):
        assert "M.FEDERATION_SOURCE IS NULL" in sql
        assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
        assert "M.DELETED_AT IS NULL" in sql
        assert "M.ARCHIVED_AT IS NULL" in sql
        assert "M.CONSOLIDATED_INTO IS NULL" in sql
        assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql

    assert feed_params == (VAULT_NAMESPACE, 25)
    assert get_params == ("mem-1", VAULT_NAMESPACE)
