"""SQL-shape tests for the Oracle / Db2 lifecycle statements (FB98).

No Oracle or Db2 server is reachable from this tree, so these tests pin the
statement the worker *would* send: each fake backend records the rendered SQL
and answers with an empty result set. That proves the statement is one the
backend parses and that its binds line up -- not that it selects the right
rows there. The live proof stays with the Oracle/Db2 integration suites.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.persistence.worker_lifecycle import (
    _Ops,
    _hard_delete_identity_rows,
    _scope_live_counts_identity,
    active_deletion_for_scope,
    sweep_for_archival,
)


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self.description = []

    async def execute(self, sql, params=()):
        self._conn.statements.append((" ".join(sql.split()), tuple(params)))
        return self

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    async def close(self):
        return None


class _RecordingConn:
    """DB-API style connection: cursor() for enterprise drivers, execute() for SQLite."""

    def __init__(self):
        self.statements = []

    def cursor(self):
        return _RecordingCursor(self)

    async def execute(self, sql, params=()):
        return await _RecordingCursor(self).execute(sql, params)


class _RecordingPostgresConn:
    """asyncpg style: no cursors, one awaitable per call."""

    def __init__(self):
        self.statements = []

    async def fetchrow(self, sql, *params):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return None

    async def fetch(self, sql, *params):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return []

    async def execute(self, sql, *params):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return "UPDATE 0"


class _FakeTx:
    def __init__(self, backend):
        self.conn = backend.conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeBackend:
    def __init__(self):
        self.conn = _RecordingConn()

    def transactional(self):
        return _FakeTx(self)


# ``backend_dialect`` / ``transaction_dialect`` dispatch on the type name, so
# the fakes are named after the backends they stand in for.
class FakeOracleBackend(_FakeBackend):
    pass


class FakeDb2Backend(_FakeBackend):
    pass


class FakeSqliteBackend(_FakeBackend):
    pass


class FakePostgresBackend(_FakeBackend):
    def __init__(self):
        self.conn = _RecordingPostgresConn()


# ---------------------------------------------------------------------------
# P1 · the deletion write fence must emit a row limiter every backend parses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_cls", [FakeOracleBackend, FakeDb2Backend])
async def test_deletion_write_fence_avoids_limit_on_oracle_and_db2(backend_cls):
    backend = backend_cls()

    row = await active_deletion_for_scope(
        backend,
        target_user_id="user-1",
        target_namespace=None,
    )

    assert row is None
    ((sql, params),) = backend.conn.statements
    # Oracle rejects LIMIT outright (ORA-00933) and Db2 only accepts it under a
    # compatibility vector. A syntax error here is swallowed by the caller, so
    # the fence silently stops existing and writes land inside a GDPR deletion.
    assert "LIMIT" not in sql
    assert sql.endswith("FETCH FIRST 1 ROWS ONLY")
    assert "ORDER BY COALESCE(soft_deleted_at, confirmed_at) DESC" in sql
    assert params == ("user-1", None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_cls", [FakeSqliteBackend, FakePostgresBackend])
async def test_deletion_write_fence_keeps_limit_where_the_dialect_has_it(backend_cls):
    backend = backend_cls()

    row = await active_deletion_for_scope(
        backend,
        target_user_id="user-1",
        target_namespace="private",
    )

    assert row is None
    ((sql, params),) = backend.conn.statements
    assert sql.endswith("LIMIT 1")
    assert "FETCH FIRST" not in sql
    assert params == ("user-1", "private", "private")


# ---------------------------------------------------------------------------
# S4 · a marker used only by ``IS NULL`` needs a type on Db2
# ---------------------------------------------------------------------------


def test_null_scope_marker_is_typed_for_each_driver():
    template = "owner_col = ? AND (? IS NULL OR namespace = ?)"

    db2 = _Ops(SimpleNamespace(conn=None), "db2").sql(template)
    assert db2 == "owner_col = ? AND (CAST(? AS VARCHAR(256)) IS NULL OR namespace = ?)"
    # The cast introduces no extra marker, so the callers' positional binds
    # and Db2's ``?`` paramstyle still line up one-for-one.
    assert db2.count("?") == 3

    assert _Ops(SimpleNamespace(conn=None), "sqlite").sql(template) == template
    assert (
        _Ops(SimpleNamespace(conn=None), "postgres").sql(template)
        == "owner_col = $1 AND ($2::text IS NULL OR namespace = $3)"
    )
    assert (
        _Ops(SimpleNamespace(conn=None), "oracle").sql(template)
        == "owner_col = :1 AND (:2 IS NULL OR namespace = :3)"
    )


# ---------------------------------------------------------------------------
# P2 · GDPR identity erasure uses the Oracle/Db2 column names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["oracle", "db2"])
async def test_gdpr_identity_erasure_matches_the_oracle_db2_schema(dialect):
    conn = _RecordingConn()
    ops = _Ops(SimpleNamespace(conn=conn), dialect)

    counts = await _hard_delete_identity_rows(ops, "user-1", "private")

    revoke_sql = conn.statements[0][0]
    # migrations_oracle/0003_api_keys.sql:14-15 declares ``last_used_at`` and
    # ``revoked_at``; there is no boolean ``revoked`` / ``last_used`` column on
    # either enterprise port, so the Postgres statement raises ORA-00904.
    assert "revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)" in revoke_sql
    assert "last_used_at = NULL" in revoke_sql
    assert "revoked = TRUE" not in revoke_sql
    assert "WHERE owner_id = " in revoke_sql

    render = _Ops(SimpleNamespace(conn=None), dialect).sql
    deletes = {sql.split()[2]: (sql, params) for sql, params in conn.statements[1:]}
    assert set(deletes) == {"api_keys", "oauth_sessions", "oauth_identities", "user_groups"}
    # Only api_keys carries the ``namespace`` column the enterprise ports added.
    assert deletes["api_keys"] == (
        render("DELETE FROM api_keys WHERE owner_id = ? AND namespace = ?"),
        ("user-1", "private"),
    )
    # 0005:41 / 0006:38 / 0008:4 key these on ``user_id`` and have no namespace
    # column, so the owner_id + namespace filter raised ORA-00904 and rolled
    # the whole erasure back.
    for table in ("oauth_sessions", "oauth_identities", "user_groups"):
        assert deletes[table] == (
            render(f"DELETE FROM {table} WHERE user_id = ?"),
            ("user-1",),
        )
    assert counts["api_keys_revoked"] == 0
    # A namespace-scoped request must not delete the account row.
    assert "users" not in counts


@pytest.mark.asyncio
async def test_identity_erasure_keeps_the_postgres_shape():
    conn = _RecordingPostgresConn()
    ops = _Ops(SimpleNamespace(conn=conn), "postgres")

    await _hard_delete_identity_rows(ops, "user-1", None)

    rendered = [sql for sql, _ in conn.statements]
    assert rendered[0] == "UPDATE api_keys SET revoked = TRUE, last_used = NULL WHERE user_id = $1"
    assert "DELETE FROM oauth_sessions WHERE user_id = $1" in rendered
    # An all-namespace request also removes the account row.
    assert "DELETE FROM users WHERE id = $1" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["oracle", "db2"])
async def test_identity_scope_counts_target_the_same_columns_as_the_deletes(dialect):
    conn = _RecordingConn()
    ops = _Ops(SimpleNamespace(conn=conn), dialect)

    counts = await _scope_live_counts_identity(ops, "user-1", "private")

    assert counts["oauth_sessions"] == 0
    for sql, _ in conn.statements:
        if "FROM api_keys" in sql:
            assert "owner_id" in sql and "namespace" in sql
        else:
            assert "user_id" in sql
            assert "namespace" not in sql


# ---------------------------------------------------------------------------
# S1 · the archive claim must not wrap ROWNUM in a FOR UPDATE target (ORA-02014)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_claim_locks_the_memories_table_on_oracle():
    pytest.importorskip("zstandard", reason="persephone archive extra not installed")
    backend = FakeOracleBackend()

    archived = await sweep_for_archival(backend, namespace="ns", archive_after_days=30, batch_size=5)

    assert archived == 0
    ((sql, params),) = backend.conn.statements
    # ORA-02014: the lock may not target an inline view carrying ROWNUM, which
    # is what ``SELECT id FROM (<ordered>) WHERE ROWNUM <= ? FOR UPDATE`` did.
    # The ordering and the limit stay in a predicate subquery instead.
    assert sql.startswith("SELECT id FROM memories WHERE id IN (SELECT id FROM (SELECT id FROM memories ")
    assert sql.endswith(") WHERE ROWNUM <= :4) FOR UPDATE SKIP LOCKED")
    assert params[2:] == ("ns", 5)


@pytest.mark.asyncio
async def test_archive_claim_uses_db2_locking_syntax():
    pytest.importorskip("zstandard", reason="persephone archive extra not installed")
    backend = FakeDb2Backend()

    archived = await sweep_for_archival(backend, namespace="ns", archive_after_days=30, batch_size=5)

    assert archived == 0
    ((sql, params),) = backend.conn.statements
    assert "ROWNUM" not in sql
    assert sql.endswith(
        "ORDER BY created ASC FETCH FIRST 5 ROWS ONLY "
        "FOR UPDATE WITH RS USE AND KEEP UPDATE LOCKS SKIP LOCKED DATA"
    )
    # Db2's FETCH FIRST takes an integer, not a marker, so the validated batch
    # size is inlined and only the three row predicates are bound.
    assert params[2:] == ("ns",)


@pytest.mark.asyncio
async def test_archive_claim_is_unchanged_on_sqlite():
    pytest.importorskip("zstandard", reason="persephone archive extra not installed")
    backend = FakeSqliteBackend()

    archived = await sweep_for_archival(backend, namespace="ns", archive_after_days=30, batch_size=5)

    assert archived == 0
    ((sql, params),) = backend.conn.statements
    assert sql.endswith("ORDER BY created ASC LIMIT ?")
    assert params[2:] == ("ns", 5)
