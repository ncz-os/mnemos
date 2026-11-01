"""F-10: Postgres index hygiene and raw-``limit`` caps.

Source: Fable federation report (2026-10-31, commit 0073bb4), F-10 PERF db:

    "F-10 PERF db: Postgres index hygiene: ``(updated, id)`` partial on
    ``deleted_at IS NULL`` (SQLite has it,
    ``migrations_sqlite/migrations.sql:53``; Postgres keyset paths
    ``postgres.py:4813-4845``, ``:4954-5133`` have none),
    ``(namespace, created DESC)`` partial, drop prefix-redundant
    ``idx_memories_owner_id``, cap raw ``limit`` on sessions history
    (``routes/sessions.py:268``) and federation log
    (``routes/federation.py:559``) (S)."

Re-verified against ARGONAS master 68d5891a (48 commits newer than the
report's 0073bb4). What the report said and what the tree looked like:

* "Postgres keyset paths have none" — CONFIRMED. Postgres had NO index
  on ``memories (updated, id)`` anywhere in its migration set, while
  the federation feed path paginates with
  ``WHERE deleted_at IS NULL AND (updated > $1 OR (updated = $1 AND
  id > $2)) ORDER BY updated ASC, id ASC``. SQLite carries
  ``idx_memories_updated_id``; Oracle and Db2 carry
  ``idx_memories_updated_cursor ON memories(deleted_at, updated, id)``.
  Postgres was the only backend without one.

* "``(namespace, created DESC)`` partial" — CONFIRMED. Postgres had
  ``idx_memories_namespace ON memories(namespace)`` (namespace alone,
  no ordering column), so every namespace-scoped listing sorted on the
  fly.

* "drop prefix-redundant ``idx_memories_owner_id``" — CONFIRMED.
  ``idx_memories_owner_cat ON memories(owner_id, category)`` already
  has ``owner_id`` as its leading column, so the single-column index
  is pure write amplification.

* "cap raw ``limit`` on sessions history and federation log" —
  CONFIRMED. Both routes took a bare ``limit: int = 50`` with no
  bound, and handed it straight to the repository's SQL ``LIMIT``.

These tests are SQLite-safe: the route caps are exercised through the
in-process ASGI client (SQLite-backed fake pool), and the index
assertions read the shipped SQL / the SQLite catalogue.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PG_MIGRATION_DIR = REPO_ROOT / "mnemos" / "db_migrations" / "migrations"
SQLITE_MIGRATION_DIR = REPO_ROOT / "mnemos" / "db_migrations" / "migrations_sqlite"


# ── 1. The (updated, id) partial index ─────────────────────────────────────


def _pg_0067_sql() -> str:
    return (PG_MIGRATION_DIR / "0067_memory_index_hygiene.sql").read_text()


def test_the_pg_migration_creates_the_updated_id_partial_index() -> None:
    """Postgres must carry an ``(updated, id)`` index for the keyset path.

    The keyset pagination the federation feed uses filters
    ``deleted_at IS NULL``, so the index is partial on that predicate:
    a partial index stays small and hot on a live corpus where the
    soft-deleted rows are the minority.
    """
    sql = _pg_0067_sql()
    assert "idx_memories_updated_id" in sql
    assert "ON memories(updated ASC, id ASC)" in sql
    assert "WHERE deleted_at IS NULL" in sql


def test_the_updated_id_index_is_replay_safe() -> None:
    """The Postgres migration runner replays every file at every boot.

    There is no applied-state ledger yet (that is F-11), so every
    statement must be idempotent.
    """
    sql = _pg_0067_sql()
    assert "CREATE INDEX IF NOT EXISTS idx_memories_updated_id" in sql
    assert "DROP INDEX IF EXISTS idx_memories_owner_id" in sql


def test_the_keyset_path_filters_deleted_at_is_null() -> None:
    """The partial index predicate must match what the query filters.

    A partial index whose predicate does not match the query's WHERE
    clause cannot be used by the planner — the index would sit there
    doing nothing.
    """
    postgres_src = (REPO_ROOT / "mnemos" / "persistence" / "postgres.py").read_text()
    # The federation feed keyset page.
    assert (
        "WHERE deleted_at IS NULL\n                  AND (updated > $1 OR (updated = $1 AND id > $2))" in postgres_src
    ), "the federation keyset page no longer filters deleted_at IS NULL; the partial index predicate must follow"


def test_sqlite_already_carries_the_updated_id_index() -> None:
    """SQLite has carried this index all along — the report's premise."""
    sql = (SQLITE_MIGRATION_DIR / "migrations.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_memories_updated_id ON memories(updated ASC, id ASC);" in sql


def test_sqlite_explain_uses_the_updated_id_index_for_the_keyset_scan() -> None:
    """EXPLAIN-based assertion: SQLite's planner picks the index.

    This is the ``EXPLAIN``-based assertion the task asks for where
    the backend allows it. SQLite reports the index name in the query
    plan, so the assertion is on the plan text, not on timing.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, updated TEXT, deleted_at TEXT)")
        conn.execute("CREATE INDEX idx_memories_updated_id ON memories(updated ASC, id ASC)")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id FROM memories "
            "WHERE deleted_at IS NULL "
            "AND (updated > ? OR (updated = ? AND id > ?)) "
            "ORDER BY updated ASC, id ASC",
            ("2026-01-01", "2026-01-01", "mem_1"),
        ).fetchall()
    finally:
        conn.close()

    plan_text = " ".join(str(cell) for row in plan for cell in row)
    assert "idx_memories_updated_id" in plan_text, plan_text


# ── 2. The (namespace, created DESC) partial index ─────────────────────────


def test_the_pg_migration_creates_the_namespace_created_partial_index() -> None:
    """Postgres must carry a ``(namespace, created DESC)`` index.

    ``idx_memories_namespace ON memories(namespace)`` filters but does
    not order, so every namespace-scoped listing had to sort.
    """
    sql = _pg_0067_sql()
    assert "idx_memories_namespace_created" in sql
    assert "ON memories(namespace, created DESC)" in sql
    assert "WHERE deleted_at IS NULL" in sql


def test_the_namespace_created_index_is_replay_safe() -> None:
    sql = _pg_0067_sql()
    assert "CREATE INDEX IF NOT EXISTS idx_memories_namespace_created" in sql


def test_sqlite_already_carries_a_namespace_index() -> None:
    """SQLite carries ``idx_memories_namespace`` (namespace only)."""
    sql = (SQLITE_MIGRATION_DIR / "migrations.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);" in sql


# ── 3. Dropping the prefix-redundant owner_id index ───────────────────────


def test_the_pg_migration_drops_the_prefix_redundant_owner_id_index() -> None:
    """``idx_memories_owner_id`` is a prefix duplicate.

    ``idx_memories_owner_cat ON memories(owner_id, category)`` already
    has ``owner_id`` as its leading column, so every ``owner_id``
    lookup is served by that index. Keeping both means every INSERT
    and every ``owner_id`` UPDATE maintains two indexes for one
    access path.
    """
    sql = _pg_0067_sql()
    assert "DROP INDEX IF EXISTS idx_memories_owner_id" in sql


def test_the_owner_cat_index_that_covers_owner_id_still_exists() -> None:
    """The covering index must NOT be dropped with it."""
    sql = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations_v1_multiuser.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_memories_owner_cat ON memories(owner_id, category);" in sql


def test_the_drop_runs_after_the_index_it_drops_is_created() -> None:
    """Migration order: the drop must come after the creation.

    ``migrations_v1_multiuser.sql`` creates ``idx_memories_owner_id``
    and the numbered ``0067`` drops it. If the order were reversed the
    drop would be a no-op on a fresh database and the index would
    come back.
    """
    from mnemos.persistence.schema import postgres_migration_paths

    names = [path.name for path in postgres_migration_paths()]
    assert "migrations_v1_multiuser.sql" in names
    assert "0067_memory_index_hygiene.sql" in names
    assert names.index("0067_memory_index_hygiene.sql") > names.index("migrations_v1_multiuser.sql")


def test_the_other_backends_keep_their_owner_indexes() -> None:
    """Only Postgres drops ``idx_memories_owner_id``.

    The other backends never had that exact index, and their owner
    indexes carry more columns, so there is nothing to drop.
    """
    oracle_sql = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations_oracle" / "0001_core_schema.sql").read_text()
    assert "idx_memories_owner_id" not in oracle_sql
    assert "idx_memories_owner_ns" in oracle_sql

    db2_sql = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations_db2" / "0001_core_schema.sql").read_text()
    assert "idx_memories_owner_id" not in db2_sql
    assert "idx_memories_live_owner_ns" in db2_sql


def test_the_oracle_and_db2_parity_files_are_executable_no_ops() -> None:
    """The parity files must contain no executable statements.

    The parity gate requires the same basename in the pg / oracle / db2
    directories. The Oracle and Db2 files therefore carry the
    reasoning and nothing executable.
    """
    from mnemos.persistence.schema import split_db2_statements, split_oracle_statements

    oracle_sql = (
        REPO_ROOT / "mnemos" / "db_migrations" / "migrations_oracle" / "0067_memory_index_hygiene.sql"
    ).read_text()
    db2_sql = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations_db2" / "0067_memory_index_hygiene.sql").read_text()

    assert split_oracle_statements(oracle_sql) == []
    assert split_db2_statements(db2_sql) == []


def test_the_pg_migration_splits_into_three_executable_statements() -> None:
    """The Postgres file must split into exactly the three statements."""
    from mnemos.persistence.schema import split_postgres_statements

    statements = split_postgres_statements(_pg_0067_sql())
    assert len(statements) == 3
    joined = " ".join(" ".join(s.split()) for s in statements)
    assert "CREATE INDEX IF NOT EXISTS idx_memories_updated_id" in joined
    assert "CREATE INDEX IF NOT EXISTS idx_memories_namespace_created" in joined
    assert "DROP INDEX IF EXISTS idx_memories_owner_id" in joined


# ── 4. The raw limit caps ──────────────────────────────────────────────────


def test_the_sessions_history_limit_is_capped() -> None:
    """The sessions-history route must bound its ``limit``.

    Before F-10 the parameter was a bare ``limit: int = 50`` with no
    bound, so ``?limit=100000000`` went straight into the repository's
    SQL ``LIMIT``.
    """
    import inspect

    from mnemos.api.routes import sessions

    src = inspect.getsource(sessions.get_session_history)
    assert "Query(50, ge=1, le=500)" in src, (
        f"F-10 regression: the sessions history route must cap the raw limit with Query(..., ge=1, le=...). Got:\n{src}"
    )


def test_the_federation_sync_log_limit_is_capped() -> None:
    """The federation peer sync-log route must bound its ``limit``."""
    import inspect

    from mnemos.api.routes import federation

    src = inspect.getsource(federation.peer_sync_log)
    assert "Query(50, ge=1, le=500)" in src, (
        "F-10 regression: the federation sync-log route must cap the "
        f"raw limit with Query(..., ge=1, le=...). Got:\n{src}"
    )


def test_the_sessions_history_offset_is_bounded() -> None:
    """A negative OFFSET is rejected at the query boundary."""
    import inspect

    from mnemos.api.routes import sessions

    src = inspect.getsource(sessions.get_session_history)
    assert "Query(0, ge=0)" in src


@pytest.mark.asyncio
async def test_the_sessions_history_route_rejects_an_over_cap_limit(client, auth_headers):
    """End-to-end: an over-cap limit is a 422, not a giant query.

    The in-process client is SQLite-backed (the fake pool), so this
    exercises the real FastAPI validation path. The validation runs
    BEFORE the handler body, so the 422 is produced without the
    sessions repository ever being touched.
    """
    resp = await client.get("/v1/sessions/does-not-exist/history?limit=100000000", headers=auth_headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_the_sessions_history_route_rejects_a_zero_limit(client, auth_headers):
    """A zero limit is rejected — a page of zero rows is never useful."""
    resp = await client.get("/v1/sessions/does-not-exist/history?limit=0", headers=auth_headers)
    assert resp.status_code == 422, resp.text


def test_the_sessions_history_limit_cap_is_published_in_the_openapi_schema() -> None:
    """The cap is part of the published API contract.

    This also proves a limit exactly AT the cap is accepted: FastAPI
    validates ``minimum <= value <= maximum``, so a schema with
    ``minimum: 1`` and ``maximum: 500`` accepts 500 and rejects
    100000000.
    """
    from mnemos.api.main import app

    schema = app.openapi()
    params = schema["paths"]["/v1/sessions/{session_id}/history"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}
    limit_schema = by_name["limit"]["schema"]
    assert limit_schema["maximum"] == 500
    assert limit_schema["minimum"] == 1
    assert limit_schema["default"] == 50

    offset_schema = by_name["offset"]["schema"]
    assert offset_schema["minimum"] == 0
    assert offset_schema["default"] == 0


def test_the_federation_sync_log_limit_cap_is_published_in_the_openapi_schema() -> None:
    """Same contract check for the federation peer sync-log route."""
    from mnemos.api.main import app

    schema = app.openapi()
    params = schema["paths"]["/v1/federation/peers/{peer_id}/log"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}
    limit_schema = by_name["limit"]["schema"]
    assert limit_schema["maximum"] == 500
    assert limit_schema["minimum"] == 1
    assert limit_schema["default"] == 50


def test_the_other_paginated_routes_already_cap_their_limit() -> None:
    """The federation feed route already capped its limit — the F-10
    step brought the two remaining routes in line with it."""
    import inspect

    from mnemos.api.routes import federation

    src = inspect.getsource(federation.federation_feed)
    assert "Query(100, ge=1, le=500)" in src


def test_the_limit_caps_are_consistent_across_the_two_routes() -> None:
    """Both capped routes use the same ceiling."""
    import inspect

    from mnemos.api.routes import federation, sessions

    sessions_src = inspect.getsource(sessions.get_session_history)
    federation_src = inspect.getsource(federation.peer_sync_log)
    assert "le=500" in sessions_src
    assert "le=500" in federation_src


def test_the_capped_routes_still_declare_a_default() -> None:
    """The default stays 50 — the cap bounds the maximum, it does not
    change the default page size."""
    import inspect

    from mnemos.api.routes import federation, sessions

    assert "Query(50, ge=1, le=500)" in inspect.getsource(sessions.get_session_history)
    assert "Query(50, ge=1, le=500)" in inspect.getsource(federation.peer_sync_log)
