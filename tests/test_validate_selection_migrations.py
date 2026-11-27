import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mnemos.persistence import sqlite as sq
from mnemos.persistence.oracle import OracleConsultationsRepository
from mnemos.persistence.db2 import Db2ConsultationsRepository
from tests.test_validate_session_rows import Cursor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repo_type", [sq.SqliteConsultationsRepository, OracleConsultationsRepository, Db2ConsultationsRepository]
)
async def test_select_models_by_tier(repo_type):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""CREATE TABLE model_registry (provider TEXT, model_id TEXT, available INTEGER,
        deprecated INTEGER, input_cost_per_mtok REAL, output_cost_per_mtok REAL, graeae_weight REAL, arena_rank INTEGER);
        INSERT INTO model_registry VALUES ('provider', 'small', 1, 0, 1, 1, .8, 4),
        ('provider', 'large', 1, 0, 10, 10, .9, 3), ('provider', 'retired', 1, 1, 0, 0, 1, 1), ('provider', 'pending', 0, 0, 0, 0, 1, 1);""")
    tx = SimpleNamespace(
        conn=conn if repo_type == sq.SqliteConsultationsRepository else SimpleNamespace(cursor=lambda: Cursor(conn))
    )
    if repo_type.__module__ == sq.__name__ and repo_type.__name__ == sq.SqliteConsultationsRepository.__name__:
        tx = sq.SqliteTransaction(conn)
    repo = object.__new__(repo_type)
    try:
        assert [r["model_id"] for r in await repo.resolve_tier_lineup(tx, "budget")] == ["small"]
        assert [r["model_id"] for r in await repo.resolve_tier_lineup(tx, "frontier")] == ["large"]
        assert [r["model_id"] for r in await repo.resolve_tier_lineup(tx, "premium")] == ["large"]
        assert [r["model_id"] for r in await repo.resolve_models(tx, ["small", "retired", "pending"])] == ["small"]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_validate_complete_migration_manifest(monkeypatch):
    backend = object.__new__(sq.SqliteBackend)
    original = Path.is_file
    missing = sq.SQLITE_MIGRATION_FILES[-1]
    monkeypatch.setattr(Path, "is_file", lambda path: False if path.name == missing else original(path))
    conn = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(RuntimeError, match=missing):
        await backend._apply_migrations(conn)
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_manifest_before_open(monkeypatch, tmp_path):
    from unittest.mock import Mock

    missing = sq.SQLITE_MIGRATION_FILES[-1]
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path.name == missing else original(path))
    monkeypatch.setattr(sq, "aiosqlite", None)
    connect = Mock(side_effect=AssertionError("connection opened before manifest validation"))
    monkeypatch.setattr(sq.sqlite3, "connect", connect)
    backend = sq.SqliteBackend(tmp_path / "data.db", SimpleNamespace())
    with pytest.raises(RuntimeError, match=missing):
        await backend.open()
    assert backend._conn is None
    connect.assert_not_called()
