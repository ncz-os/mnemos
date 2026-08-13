from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.schema import (
    _is_benign_db2_error,
    _is_benign_oracle_error,
    _is_benign_postgres_error,
    db2_migration_paths,
    ensure_postgres_schema,
    oracle_migration_paths,
    postgres_migration_paths,
    render_migration_sql,
)


class _FakeSqlstateError(Exception):
    def __init__(self, message: str, sqlstate: str = "") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _Acquire:
    def __init__(self, conn: "_FakePgConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakePgConn":
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePgPool:
    def __init__(self, conn: "_FakePgConn") -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _FakePgConn:
    def __init__(self, *, current_type: str = "vector(1024)", index_method: str | None = "hnsw") -> None:
        self.current_type = current_type
        self.index_method = index_method
        self.statements: list[str] = []

    async def execute(self, statement: str) -> str:
        self.statements.append(statement)
        return "OK"

    async def fetchval(self, query: str, *_args: Any) -> Any:
        if "format_type(atttypid, atttypmod)" in query:
            return self.current_type
        if "COUNT(*) FROM memories WHERE embedding IS NOT NULL" in query:
            return 0
        if "idx_memories_embedding" in query and "am.amname" in query:
            return self.index_method
        return None


def _settings(dim: int) -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=dim))


@pytest.mark.asyncio
async def test_postgres_standup_substitutes_embedding_dim_and_hnsw() -> None:
    conn = _FakePgConn(current_type="vector(1024)", index_method="hnsw")

    await ensure_postgres_schema(_FakePgPool(conn), _settings(1024))

    applied = "\n".join(conn.statements)
    assert "embedding vector(1024)" in applied
    assert "USING hnsw (embedding vector_cosine_ops)" in applied
    assert "ivfflat" not in applied.lower()


@pytest.mark.asyncio
async def test_postgres_standup_repairs_empty_wrong_dim_column_and_index() -> None:
    conn = _FakePgConn(current_type="vector(768)", index_method="ivfflat")

    await ensure_postgres_schema(_FakePgPool(conn), _settings(1024))

    applied = "\n".join(conn.statements)
    assert "ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024) USING NULL" in applied
    assert "DROP INDEX IF EXISTS idx_memories_embedding" in applied
    assert "ON memories USING hnsw (embedding vector_cosine_ops)" in applied


def test_postgres_standup_migration_order_includes_full_numbered_surface() -> None:
    paths = {path.relative_to(Path(__file__).resolve().parents[1]).as_posix() for path in postgres_migration_paths()}

    assert "mnemos/db_migrations/migrations.sql" in paths
    assert "mnemos/db_migrations/migrations/0029_memory_audit_chain.sql" in paths
    assert "mnemos/db_migrations/migrations/0030_memory_audit_roots.sql" in paths
    assert "mnemos/db_migrations/migrations/0031_memory_category_decay.sql" in paths
    assert "mnemos/db_migrations/migrations/0040_memory_compression_queue_parity.sql" in paths
    assert "mnemos/db_migrations/migrations/0044_model_registry_pricing.sql" in paths
    assert "mnemos/db_migrations/migrations/0046_graeae_soft_delete_ownership.sql" in paths


def test_oracle_and_db2_standup_use_full_migration_sets_and_dim_templates() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    oracle_paths = oracle_migration_paths()
    db2_paths = db2_migration_paths()

    assert len(oracle_paths) > 1
    assert len(db2_paths) > 1
    assert (repo_root / "mnemos/db_migrations/migrations_oracle/0046_graeae_soft_delete_ownership.sql") in oracle_paths
    assert (repo_root / "mnemos/db_migrations/migrations_db2/0046_graeae_soft_delete_ownership.sql") in db2_paths
    oracle_lifecycle = repo_root / "mnemos/db_migrations/migrations_oracle/0051_lifecycle_schema_parity.sql"
    db2_lifecycle = repo_root / "mnemos/db_migrations/migrations_db2/0051_lifecycle_schema_parity.sql"
    assert oracle_lifecycle in oracle_paths
    assert db2_lifecycle in db2_paths

    for path in (oracle_lifecycle, db2_lifecycle):
        lifecycle_sql = path.read_text().lower()
        assert "memory_branches" in lifecycle_sql and "deleted_at" in lifecycle_sql
        assert "session_memory_injections" in lifecycle_sql
        assert "entities" in lifecycle_sql and "owner_id" in lifecycle_sql and "namespace" in lifecycle_sql
        assert "memory_archive" in lifecycle_sql and "varchar" in lifecycle_sql and "100" in lifecycle_sql

    oracle_sql = render_migration_sql(
        repo_root / "mnemos/db_migrations/migrations_oracle/0001_core_schema.sql",
        backend="oracle",
        embedding_dim=1024,
    )
    db2_sql = render_migration_sql(
        repo_root / "mnemos/db_migrations/migrations_db2/0001_core_schema.sql",
        backend="db2",
        embedding_dim=1024,
    )

    assert "VECTOR(1024, FLOAT32)" in oracle_sql
    assert "VECTOR(*, FLOAT32)" not in oracle_sql
    assert "VECTOR(1024, FLOAT32)" in db2_sql
    assert "{{embedding_dim}}" not in db2_sql


def test_db2_unique_violation_on_seed_insert_replay_is_benign() -> None:
    # Regression (found 2026-07-11): a static seed-data INSERT (e.g.
    # 0033_subscription_plans) is replayed on every startup with no separate
    # migration-tracking table. A duplicate-key hit on replay means "this
    # exact row was already inserted by a prior run" -- benign, not a real
    # conflict. Without this, a Db2-backed host crash-loops forever after any
    # restart following a partial-then-recovered prior migration run.
    exc = Exception(
        "ibm_db_dbi::IntegrityError: Statement Execute Failed: "
        "[IBM][CLI Driver][DB2/LINUXX8664] SQL0803N One or more values in "
        "the INSERT statement... SQLSTATE=23505 SQLCODE=-803"
    )
    assert _is_benign_db2_error("INSERT INTO subscription_plans (...) VALUES (...)", exc)


def test_db2_genuine_duplicate_object_error_still_benign() -> None:
    # Existing coverage: DDL replay (CREATE TABLE on an already-provisioned
    # table) must remain benign -- this fix must not regress it.
    exc = Exception("SQLCODE=-601 SQLSTATE=42710 table already exists")
    assert _is_benign_db2_error("CREATE TABLE subscription_plans (...)", exc)


def test_postgres_unique_violation_on_seed_insert_replay_is_benign() -> None:
    exc = _FakeSqlstateError("duplicate key value violates unique constraint", sqlstate="23505")
    assert _is_benign_postgres_error("INSERT INTO subscription_plans (...) VALUES (...)", exc)


def test_oracle_unique_constraint_violation_on_seed_insert_replay_is_benign() -> None:
    exc = Exception("ORA-00001: unique constraint (MNEMOS.PK_SUBSCRIPTION_PLANS) violated")
    assert _is_benign_oracle_error(exc)


def test_db2_unrelated_error_is_not_benign() -> None:
    # Guard against over-broadening: a genuine, unrelated Db2 error must
    # still surface as a hard failure.
    exc = Exception("SQLCODE=-104 SQLSTATE=42601 unexpected token")
    assert not _is_benign_db2_error("SELECT * FROM memories", exc)
