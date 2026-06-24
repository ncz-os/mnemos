from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.schema import (
    db2_migration_paths,
    ensure_postgres_schema,
    oracle_migration_paths,
    postgres_migration_paths,
    render_migration_sql,
)


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

    assert "db/migrations.sql" in paths
    assert "db/migrations/0029_memory_audit_chain.sql" in paths
    assert "db/migrations/0030_memory_audit_roots.sql" in paths
    assert "db/migrations/0031_memory_category_decay.sql" in paths
    assert "db/migrations/0040_memory_compression_queue_parity.sql" in paths
    assert "db/migrations/0044_model_registry_pricing.sql" in paths
    assert "db/migrations/0046_graeae_soft_delete_ownership.sql" in paths


def test_oracle_and_db2_standup_use_full_migration_sets_and_dim_templates() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    oracle_paths = oracle_migration_paths()
    db2_paths = db2_migration_paths()

    assert len(oracle_paths) > 1
    assert len(db2_paths) > 1
    assert (repo_root / "db/migrations_oracle/0046_graeae_soft_delete_ownership.sql") in oracle_paths
    assert (repo_root / "db/migrations_db2/0046_graeae_soft_delete_ownership.sql") in db2_paths

    oracle_sql = render_migration_sql(
        repo_root / "db/migrations_oracle/0001_core_schema.sql",
        backend="oracle",
        embedding_dim=1024,
    )
    db2_sql = render_migration_sql(
        repo_root / "db/migrations_db2/0001_core_schema.sql",
        backend="db2",
        embedding_dim=1024,
    )

    assert "VECTOR(1024, FLOAT32)" in oracle_sql
    assert "VECTOR(*, FLOAT32)" not in oracle_sql
    assert "VECTOR(1024, FLOAT32)" in db2_sql
    assert "{{embedding_dim}}" not in db2_sql
