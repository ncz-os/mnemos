"""Per-backend dialect and inheritance guards for MORPHEUS item 11c."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.persistence.db2 import Db2MorpheusRepository
from mnemos.persistence.mariadb import MariadbMorpheusRepository
from mnemos.persistence.mysql import MysqlMorpheusRepository
from mnemos.persistence.oracle import OracleMorpheusRepository
from mnemos.persistence.postgres import PostgresMorpheusRepository
from mnemos.persistence.schema import split_db2_statements, split_oracle_statements
from mnemos.persistence.sqlite import SqliteMorpheusRepository


_PHASE_METHODS = {
    "phase_consolidate",
    "phase_synthesise_load",
    "phase_synthesise_store",
    "phase_extract_load",
    "phase_extract_failure",
    "phase_extract_store",
}


@pytest.mark.parametrize(
    "repository",
    [
        PostgresMorpheusRepository,
        SqliteMorpheusRepository,
        MysqlMorpheusRepository,
        MariadbMorpheusRepository,
        OracleMorpheusRepository,
        Db2MorpheusRepository,
    ],
)
def test_item_11c_methods_leave_every_backend_concrete(repository):
    assert not inspect.isabstract(repository)
    assert all(callable(getattr(repository, name)) for name in _PHASE_METHODS)


def test_postgres_keeps_native_jsonb_arrays_and_conflict_upsert():
    source = "\n".join(
        inspect.getsource(getattr(PostgresMorpheusRepository, name))
        for name in _PHASE_METHODS
    )
    assert "ANY($1::text[])" in source
    assert "jsonb_set" in source
    assert "to_jsonb(permission_mode)" in source
    assert "ON CONFLICT (memory_id) DO UPDATE" in source


def test_sqlite_uses_json1_expanded_params_and_conflict_upsert():
    source = "\n".join(
        inspect.getsource(getattr(SqliteMorpheusRepository, name))
        for name in _PHASE_METHODS
    )
    assert "json_set" in source
    assert "json_type" in source
    assert 'join("?" for _ in member_ids)' in source
    assert "ON CONFLICT(memory_id) DO UPDATE" in source


def test_mysql_and_mariadb_share_phase_dialect_except_longtext_json_store():
    mysql_source = "\n".join(
        inspect.getsource(getattr(MysqlMorpheusRepository, name))
        for name in _PHASE_METHODS
    )
    assert "JSON_CONTAINS_PATH" in mysql_source
    assert "JSON_SET" in mysql_source
    assert "ON DUPLICATE KEY UPDATE" in mysql_source
    assert "CAST(%s AS JSON)" in inspect.getsource(
        MysqlMorpheusRepository.phase_synthesise_store
    )
    assert (
        MariadbMorpheusRepository.phase_consolidate
        is MysqlMorpheusRepository.phase_consolidate
    )
    assert (
        MariadbMorpheusRepository.phase_extract_store
        is MysqlMorpheusRepository.phase_extract_store
    )
    assert "CAST(%s AS JSON)" not in inspect.getsource(
        MariadbMorpheusRepository.phase_synthesise_store
    )


class _MysqlConsolidateCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self._one = None
        self._all = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if normalized.startswith("SELECT config, cluster_min_size"):
            self._one = (
                json.dumps(
                    {
                        "clusters": [
                            {"member_memory_ids": ["canonical", "member"]}
                        ]
                    }
                ),
                2,
                None,
            )
        elif normalized.startswith("SELECT id, recall_count"):
            now = datetime.now(timezone.utc)
            self._all = [
                ("canonical", 10, now, 600, None, None, "{}"),
                ("member", 1, now, 644, None, None, "{}"),
            ]
        elif normalized.startswith("SELECT COUNT(*) FROM memories"):
            self._one = (0,)
        elif normalized.startswith("UPDATE memories"):
            # Emulate MySQL's left-to-right assignment behavior. The buggy
            # query reads the column after permission_mode was assigned and
            # therefore snapshots 400; the fixed query binds locked row[3].
            new_mode = int(params[1])
            original_mode_bind = int(params[5]) if len(params) == 9 else None
            self.connection.permission_mode = new_mode
            self.connection.metadata["pre_consolidate_permission_mode"] = (
                original_mode_bind
                if original_mode_bind is not None
                else self.connection.permission_mode
            )
            self.rowcount = 1

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._all


class _MysqlConsolidateConnection:
    def __init__(self):
        self.permission_mode = 644
        self.metadata = {}

    def cursor(self):
        return _MysqlConsolidateCursor(self)


@pytest.mark.asyncio
async def test_mysql_consolidate_snapshots_original_permission_mode():
    conn = _MysqlConsolidateConnection()
    result = await MysqlMorpheusRepository().phase_consolidate(
        SimpleNamespace(conn=conn),
        run_id="run-1",
        consolidated_permission_mode=400,
    )

    assert result is not None
    assert result.memories_consolidated == 1
    assert conn.permission_mode == 400
    assert conn.metadata["pre_consolidate_permission_mode"] == 644


def test_oracle_merge_and_db2_inheritance_keep_rmw_json_fallback():
    oracle_source = "\n".join(
        inspect.getsource(getattr(OracleMorpheusRepository, name))
        for name in _PHASE_METHODS
    )
    assert "MERGE INTO morpheus_extract_failures" in oracle_source
    assert "MERGE INTO morpheus_extract_run_memories" in oracle_source
    assert 'metadata.setdefault("pre_consolidate_permission_mode"' in oracle_source
    assert Db2MorpheusRepository.phase_consolidate is OracleMorpheusRepository.phase_consolidate
    assert Db2MorpheusRepository.phase_extract_store is OracleMorpheusRepository.phase_extract_store
    assert "oracledb" not in Db2MorpheusRepository._set_morpheus_clob_inputs.__code__.co_names


@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "oracle", "db2"])
def test_non_postgres_phase_parity_migrations_cover_retry_state(dialect):
    root = Path(__file__).resolve().parents[1]
    migration = (
        root
        / "mnemos"
        / "db_migrations"
        / f"migrations_{dialect}"
        / "0061d_morpheus_phase_parity.sql"
    ).read_text()
    assert "morpheus_extract_failures" in migration
    assert "source_memories" in migration
    assert "triples_extracted_at" in migration


def test_oracle_and_db2_phase_migrations_split_into_complete_statements():
    root = Path(__file__).resolve().parents[1] / "mnemos" / "db_migrations"
    oracle = (
        root / "migrations_oracle" / "0061d_morpheus_phase_parity.sql"
    ).read_text()
    db2 = (root / "migrations_db2" / "0061d_morpheus_phase_parity.sql").read_text()
    assert len(split_oracle_statements(oracle)) == 7
    assert len(split_db2_statements(db2)) == 8
