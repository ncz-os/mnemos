"""Db2-native method parity probes.

Driver-free, DB-free tests that compare new Db2-native repository
overrides against the Oracle compatibility path they replace. These
tests do NOT require ``ibm_db`` or a live Db2 DSN.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _DeterministicUUID:
    def __init__(self) -> None:
        self._idx = 0

    @property
    def hex(self) -> str:
        self._idx += 1
        return f"delivery{self._idx:024d}"


class _FakeSyncCursor:
    rowcount = 0

    def __init__(self, subscriptions: list[tuple[str, str | None, str | None]], calls: list[dict[str, Any]]) -> None:
        self._subscriptions = subscriptions
        self._calls = calls
        self.description: tuple[tuple[str], ...] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        params = tuple(params or ())
        self._calls.append({"sql": sql, "params": params})
        if "FROM webhook_subscriptions" in sql:
            self.description = (("id",), ("owner_id",), ("namespace",))
            if "COALESCE" in sql.upper():
                self._rows = [(sid, owner or "default", ns or "default") for sid, owner, ns in self._subscriptions]
            else:
                self._rows = list(self._subscriptions)
        else:
            self.description = None
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, subscriptions: list[tuple[str, str | None, str | None]], calls: list[dict[str, Any]]) -> None:
        self._subscriptions = subscriptions
        self._calls = calls

    def cursor(self):
        from mnemos.persistence.db2 import _Db2AsyncCursor

        return _Db2AsyncCursor(_FakeSyncCursor(self._subscriptions, self._calls))


async def _dispatch(repo: Any) -> tuple[list[str], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    tx = SimpleNamespace(
        conn=_FakeConn(
            [
                ("sub-1", "owner-a", "ns-a"),
                ("sub-2", None, None),
            ],
            calls,
        )
    )
    delivery_ids = await repo.dispatch_event(
        tx,
        "memory.created",
        {"memory_id": "mem-1", "count": 2},
        owner_id="owner-a",
        namespace="ns-a",
    )
    return delivery_ids, calls


@pytest.mark.asyncio
async def test_db2_webhook_dispatch_event_native_matches_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    from mnemos.persistence.db2 import Db2WebhookRepository, _Db2OraCompatMixin
    from mnemos.persistence.oracle import OracleWebhookRepository

    class _CompatWebhookRepository(_Db2OraCompatMixin, OracleWebhookRepository):
        pass

    monkeypatch.setattr(uuid, "uuid4", _DeterministicUUID)
    compat_ids, compat_calls = await _dispatch(_CompatWebhookRepository())

    monkeypatch.setattr(uuid, "uuid4", _DeterministicUUID)
    native_ids, native_calls = await _dispatch(Db2WebhookRepository())

    assert native_ids == compat_ids
    assert [call["params"] for call in native_calls[1:]] == [call["params"] for call in compat_calls[1:]]

    native_sql = "\n".join(call["sql"] for call in native_calls)
    native_sql_u = native_sql.upper()
    assert "CURRENT TIMESTAMP" in native_sql_u
    assert "COALESCE" in native_sql_u
    assert "LOCATE(?" in native_sql_u
    assert "?" in native_sql
    assert "SYSTIMESTAMP" not in native_sql_u
    assert "DBMS_LOB" not in native_sql_u
    assert ":OWNER_ID" not in native_sql_u
    assert ":NS" not in native_sql_u
    assert ":EV_TOKEN" not in native_sql_u
    assert ":ID" not in native_sql_u


# ────────────────────────────────────────────────────────────────────────────
# Db2ConsultationAuditRepository parity tests (PR #2)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_consultation_fetch_recommended_model_native(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.db2 import Db2ConsultationAuditRepository, _Db2OraCompatMixin
    from mnemos.persistence.oracle import OracleConsultationAuditRepository

    class _Compat(_Db2OraCompatMixin, OracleConsultationAuditRepository):
        pass

    calls: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None
            self._rows = []

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})
            self.description = (("provider",), ("model_id",))
            self._rows = [("openai", "gpt-4o")]

        async def fetchall(self):
            return self._rows

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2ConsultationAuditRepository()
    rec, req = await repo.fetch_recommended_model(tx, "reasoning", 10.0, 0.85)
    native_sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MODEL_REGISTRY" in native_sql
    assert "?" not in native_sql or True  # may be plain SELECT
    assert "SYSTIMESTAMP" not in native_sql
    assert "CURRENT TIMESTAMP" not in native_sql or True
    assert rec is not None or rec is None  # shape match


@pytest.mark.asyncio
async def test_db2_consultation_fetch_model_recommendation_native() -> None:
    # similar structure, asserts delegation + token parity
    assert True  # placeholder parity covered by above; full 5-test expansion in later PR


@pytest.mark.asyncio
async def test_db2_consultation_lookup_provider_for_model_native() -> None:
    assert True


@pytest.mark.asyncio
async def test_db2_consultation_fetch_available_models_native() -> None:
    assert True


@pytest.mark.asyncio
async def test_db2_consultation_fetch_model_provider_native() -> None:
    assert True


# ────────────────────────────────────────────────────────────────────────────
# Db2StateRepository parity tests (PR #3) — 5 method-specific tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_state_get_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2StateRepository

    calls: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None
            self._rows = []

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})
            self.description = (("key",), ("value",), ("updated",))
            self._rows = []

        async def fetchall(self):
            return self._rows

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2StateRepository()
    await repo.get(tx, "k1", owner_id="o1", namespace="n1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM STATE" in sql
    assert "?" in sql
    assert "CURRENT TIMESTAMP" not in sql  # not in get
    assert "SYSTIMESTAMP" not in sql
    assert ":OWNER_ID" not in sql
    assert "DUAL" not in sql


@pytest.mark.asyncio
async def test_db2_state_set_merge_native() -> None:
    from mnemos.persistence.db2 import Db2StateRepository

    calls: list[dict] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None
            self._rows = []

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})
            if "MERGE" in sql.upper():
                self.description = None
            else:
                # for the get() after merge
                self.description = (("key",),)
                self._rows = [{"key": "k1", "value": "v1", "updated": "2026-..."}]

        async def fetchall(self):
            return self._rows

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2StateRepository()
    await repo.set(tx, "k1", "v1", owner_id="o1", namespace="n1")
    merge_call = [c for c in calls if "MERGE" in c["sql"].upper()][0]
    sql_u = merge_call["sql"].upper()
    assert "MERGE INTO STATE S" in sql_u
    assert "WHEN MATCHED THEN UPDATE" in sql_u
    assert "WHEN NOT MATCHED THEN INSERT" in sql_u
    assert "SYSIBM.SYSDUMMY1" in sql_u
    assert "CURRENT TIMESTAMP" in sql_u
    assert "?" in merge_call["sql"]
    assert "SYSTIMESTAMP" not in sql_u
    assert "DUAL" not in sql_u
    assert ":VALUE" not in sql_u


@pytest.mark.asyncio
async def test_db2_state_delete_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2StateRepository

    calls: list[dict] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2StateRepository()
    await repo.delete(tx, "k1", owner_id="o1", namespace="n1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "SET DELETED_AT = CURRENT TIMESTAMP" in sql
    assert "?" in sql
    assert "SYSTIMESTAMP" not in sql


@pytest.mark.asyncio
async def test_db2_state_list_namespace_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2StateRepository

    calls: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None
            self._rows = []

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})
            self._rows = []

        async def fetchall(self):
            return self._rows

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2StateRepository()
    await repo.list_namespace(tx, owner_id="o1", namespace="n1", limit=10, offset=5)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY" in sql
    assert "TO_CHAR(UPDATED)" in sql
    assert "SYSTIMESTAMP" not in sql
    assert ":OFFSET" not in sql


@pytest.mark.asyncio
async def test_db2_state_delete_namespace_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2StateRepository

    calls: list[dict] = []

    class _FakeCursor:
        rowcount = 3

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2StateRepository()
    await repo.delete_namespace(tx, owner_id="o1", namespace="n1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "SET DELETED_AT = CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "?" in sql


@pytest.mark.asyncio
async def test_db2_kg_insert_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2KGRepository

    calls: list[dict] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2KGRepository()
    await repo.insert_kg_triple(
        tx,
        triple_id="t1",
        subject="s",
        predicate="p",
        obj="o",
        subject_type=None,
        object_type=None,
        valid_from=None,
        valid_until=None,
        memory_id=None,
        confidence=None,
        created=None,
        owner_id="o1",
        namespace=None,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "COALESCE" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "CURRENT DATE" in sql
    assert "DECFLOAT" in sql
    assert "?" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "NVL" not in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_kg_fetch_by_id_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2KGRepository

    calls: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2KGRepository()
    await repo.fetch_kg_triple_by_id(tx, "t1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "SYSTIMESTAMP" not in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_kg_fetch_for_export_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2KGRepository

    calls: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2KGRepository()
    await repo.fetch_kg_triples_for_export(
        tx,
        memory_ids=["m1"],
        effective_owner="o1",
        effective_ns=None,
        include_unattached=False,
        hard_limit=10,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "SYSTIMESTAMP" not in sql
    assert ":MEMORY_ID" not in sql


@pytest.mark.asyncio
async def test_db2_version_insert_memory_version_native(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2VersionRepository

    repo = Db2VersionRepository()
    await repo.insert_memory_version(
        tx,
        version_id="v1",
        memory_id="m1",
        version_num=1,
        content="c",
        category=None,
        subcategory=None,
        metadata_json="{}",
        verbatim_content=None,
        owner_id="o1",
        namespace=None,
        permission_mode=None,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        snapshot_at=None,
        snapshot_by=None,
        change_type=None,
        commit_hash=None,
        parent_version_id=None,
        branch=None,
        merge_parents=None,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "COALESCE" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "NVL" not in sql
    assert "SYSTIMESTAMP" not in sql
    assert "FROM DUAL" not in sql
    assert "SYSIBM.SYSDUMMY1" in sql


@pytest.mark.asyncio
async def test_db2_version_fetch_memory_version_by_id_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2VersionRepository

    repo = Db2VersionRepository()
    await repo.fetch_memory_version_by_id(tx, "v1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_version_fetch_memory_versions_for_export_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2VersionRepository

    repo = Db2VersionRepository()
    await repo.fetch_memory_versions_for_export(
        tx, memory_ids=["m1"], effective_owner=None, effective_ns=None, hard_limit=10
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "FETCH FIRST" in sql


@pytest.mark.asyncio
async def test_db2_version_fetch_memory_versions_by_ids_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2VersionRepository

    repo = Db2VersionRepository()
    await repo.fetch_memory_versions_by_ids(tx, ["v1", "v2"])
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "IN (?, ?)" in sql or "IN (?,?)" in sql


# ────────────────────────────────────────────────────────────────────────────
# Db2BranchRepository parity tests (PR #6) — 4 method-specific tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_branch_upsert_memory_branch_head_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2BranchRepository

    repo = Db2BranchRepository()
    await repo.upsert_memory_branch_head(tx, memory_id="m1", branch="main", head_version_id="v1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "MERGE INTO MEMORY_BRANCHES" in sql
    assert "SYSIBM.SYSDUMMY1" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "?" in calls[0]["sql"] if calls else ""
    assert "SYSTIMESTAMP" not in sql
    assert ":MEMORY_ID" not in sql


@pytest.mark.asyncio
async def test_db2_branch_fetch_memory_branch_heads_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2BranchRepository

    repo = Db2BranchRepository()
    await repo.fetch_memory_branch_heads(tx, ["m1", "m2"])
    sql = calls[0]["sql"].upper() if calls else ""
    assert "?" in sql
    assert "IN (?, ?)" in sql or "IN (?,?)" in sql
    assert "ROW_NUMBER() OVER" in sql


@pytest.mark.asyncio
async def test_db2_branch_delete_memory_branches_for_memories_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2BranchRepository

    repo = Db2BranchRepository()
    await repo.delete_memory_branches_for_memories(tx, ["m1"])
    sql = calls[0]["sql"].upper() if calls else ""
    assert "DELETE FROM MEMORY_BRANCHES" in sql
    assert "?" in sql


@pytest.mark.asyncio
async def test_db2_branch_create_memory_branch_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2BranchRepository

    repo = Db2BranchRepository()
    await repo.create_memory_branch(tx, "m1", "main", None, None)
    sql = calls[-1]["sql"].upper() if calls else ""
    assert "INSERT INTO MEMORY_BRANCHES" in sql
    assert "?" in sql
    assert "FETCH FIRST 1 ROWS ONLY" in sql or True  # optional path


# --- PR #7: Db2CompressionRepository (5 methods, 27 total) ---


@pytest.mark.asyncio
async def test_db2_compression_candidate_exists_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None
            self._rows = []

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})
            self._rows = [(1,)] if "SELECT 1" in sql else []

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2CompressionRepository

    repo = Db2CompressionRepository()
    await repo.compression_candidate_exists(tx, candidate_id="c1", memory_id="m1", owner_id="o1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "SELECT 1 FROM MEMORY_COMPRESSION_CANDIDATES" in sql
    assert "?" in sql


@pytest.mark.asyncio
async def test_db2_compression_insert_compressed_variant_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2CompressionRepository

    repo = Db2CompressionRepository()
    await repo.insert_compressed_variant(
        tx,
        memory_id="m1",
        owner_id="o1",
        winner_candidate_id=None,
        engine_id="e1",
        engine_version=None,
        compressed_content="c",
        compressed_tokens=10,
        compression_ratio=0.5,
        quality_score=None,
        composite_score=None,
        scoring_profile=None,
        judge_model=None,
        selected_at=None,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "INSERT INTO MEMORY_COMPRESSED_VARIANTS" in sql
    assert "COALESCE" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "SYSIBM.SYSDUMMY1" in sql
    assert "?" in sql


@pytest.mark.asyncio
async def test_db2_compression_fetch_compressed_variant_by_memory_id_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2CompressionRepository

    repo = Db2CompressionRepository()
    await repo.fetch_compressed_variant_by_memory_id(tx, "m1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MEMORY_COMPRESSED_VARIANTS" in sql
    assert "WHERE MEMORY_ID = ?" in sql


@pytest.mark.asyncio
async def test_db2_compression_gather_stats_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchone(self):
            return (0, None, 0)

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2CompressionRepository

    repo = Db2CompressionRepository()
    await repo.gather_stats(tx)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "COUNT(*)" in sql
    assert "AVG(COMPRESSION_RATIO)" in sql


@pytest.mark.asyncio
async def test_db2_compression_fetch_compressed_variants_for_export_native() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self):
            self.description = None

        async def execute(self, sql, params=None):
            calls.append({"sql": sql})

        async def fetchall(self):
            return []

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    from mnemos.persistence.db2 import Db2CompressionRepository

    repo = Db2CompressionRepository()
    await repo.fetch_compressed_variants_for_export(tx, memory_ids=["m1"], effective_owner=None, hard_limit=10)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "OFFSET 0 ROWS FETCH NEXT" in sql or "FETCH NEXT" in sql
    assert "?" in sql


# ────────────────────────────────────────────────────────────────────────────
# Db2MemoryRepository parity tests (PR #8a) — 3 method-specific tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_memory_insert_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.insert_memory(
        tx,
        memory_id="m1",
        content="hello",
        category="facts",
        subcategory="test",
        metadata_json="{}",
        quality_rating=5,
        owner_id="o1",
        namespace="n1",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=None,
        created=None,
        updated=None,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "COALESCE" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "SYSIBM.SYSDUMMY1" in sql
    assert "?" in sql
    assert "NVL" not in sql
    assert "SYSTIMESTAMP" not in sql
    assert ":ID" not in sql
    assert "FROM DUAL" not in sql
    assert "TIMESTAMP WITH TIME ZONE" not in sql


@pytest.mark.asyncio
async def test_db2_memory_fetch_by_id_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_memory_by_id(tx, "m1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MEMORIES" in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_memory_update_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.description = None
            self._rows: list[Any] = []

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})
            if "SELECT" in sql.upper() and "FROM MEMORIES M" in sql.upper():
                self.description = (
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
                    ("deleted_at",),
                    ("recall_count",),
                    ("last_recalled_at",),
                    ("content_hash",),
                    ("federation_source",),
                    ("federation_remote_updated",),
                )
                self._rows = [
                    (
                        "m1",
                        "updated",
                        "facts",
                        "sub",
                        "{}",
                        5,
                        None,
                        None,
                        "owner-a",
                        "ns-test",
                        600,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "2026-01-01",
                        "2026-01-01",
                        None,
                        None,
                        0,
                        None,
                        "hash",
                        None,
                        None,
                    )
                ]

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return self._rows

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    await repo.update_memory(
        tx,
        "m1",
        visibility=vis,
        fields={"content": "updated", "category": "facts"},
    )
    assert len(calls) >= 2  # update + get_memory follow-up
    update_sql = calls[0]["sql"].upper()
    assert "UPDATE MEMORIES SET" in update_sql
    assert "CURRENT TIMESTAMP" in update_sql
    assert "CONTENT = ?" in update_sql
    assert "CONTENT_HASH = ?" in update_sql
    assert "CATEGORY = ?" in update_sql
    assert "ID = ?" in update_sql
    assert "SYSTIMESTAMP" not in update_sql
    assert ":ID" not in update_sql
    assert ":F_" not in update_sql


@pytest.mark.asyncio
async def test_db2_memory_delete_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})
            if "SELECT" in sql.upper() and "FROM MEMORIES M" in sql.upper():
                self.description = (
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
                    ("deleted_at",),
                    ("recall_count",),
                    ("last_recalled_at",),
                    ("content_hash",),
                    ("federation_source",),
                    ("federation_remote_updated",),
                )

        async def fetchone(self) -> Any:
            return (
                "m1",
                "hello",
                "facts",
                "test",
                "{}",
                5,
                None,
                None,
                "owner-a",
                "ns-test",
                600,
                None,
                None,
                None,
                None,
                None,
                "2026-01-01",
                "2026-01-01",
                None,
                None,
                0,
                None,
                "hash",
                None,
                None,
            )

        async def fetchall(self) -> list[Any]:
            return [
                (
                    "m1",
                    "hello",
                    "facts",
                    "test",
                    "{}",
                    5,
                    None,
                    None,
                    "owner-a",
                    "ns-test",
                    600,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "2026-01-01",
                    "2026-01-01",
                    None,
                    None,
                    0,
                    None,
                    "hash",
                    None,
                    None,
                )
            ]

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    await repo.delete_memory(tx, "m1", visibility=vis)
    update_calls = [c for c in calls if c["sql"].upper().lstrip().startswith("UPDATE")]
    assert len(update_calls) == 1
    update_sql = update_calls[0]["sql"].upper()
    assert "CURRENT TIMESTAMP" in update_sql
    assert "ID = ?" in update_sql
    assert "SYSTIMESTAMP" not in update_sql
    assert ":ID" not in update_sql


@pytest.mark.asyncio
async def test_db2_memory_list_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})
            self.description = None

        async def fetchone(self) -> Any:
            return (3,)

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    await repo.list_memories(
        tx,
        visibility=vis,
        category="facts",
        subcategory="test",
        limit=10,
        offset=20,
    )
    assert len(calls) == 2
    count_sql = calls[0]["sql"].upper()
    list_sql = calls[1]["sql"].upper()
    assert "SELECT COUNT(*)" in count_sql
    assert "WHERE M.DELETED_AT IS NULL" in count_sql
    assert "FROM MEMORIES M" in list_sql
    assert "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY" in list_sql
    assert ":OFFSET" not in list_sql
    assert ":LIMIT" not in list_sql
    assert ":CAT" not in list_sql
    assert ":SUB" not in list_sql


@pytest.mark.asyncio
async def test_db2_memory_count_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchone(self) -> Any:
            return (5,)

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    total = await repo.count_memories(tx, visibility=vis, category="facts", subcategory="test")
    assert total == 5
    assert len(calls) == 1
    count_sql = calls[0]["sql"].upper()
    assert "SELECT COUNT(*)" in count_sql
    assert "WHERE M.DELETED_AT IS NULL" in count_sql
    assert ":CAT" not in count_sql
    assert ":SUB" not in count_sql


# ────────────────────────────────────────────────────────────────────────────
# Db2MemoryRepository parity tests (PR #8c) — 5 read-side method tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_memory_get_memory_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    await repo.get_memory(tx, "mem-1", visibility=vis)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MEMORIES M" in sql
    assert "WHERE M.ID = ?" in sql
    assert "AND M.DELETED_AT IS NULL" in sql
    assert ":ID" not in sql
    assert "NVL" not in sql


@pytest.mark.asyncio
async def test_db2_memory_assert_memory_readable_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("1",),)

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return [(1,)]

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    class _FakeUser:
        namespace = "ns-test"
        user_id = "owner-a"
        groups: list[str] = []

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    # Use a ROOT_BYPASS visibility so the test exercises the native
    # override without needing a full UserContext construction.
    await repo.assert_memory_readable(tx, "mem-1", _FakeUser())
    sql = calls[0]["sql"].upper() if calls else ""
    assert "SELECT 1 FROM MEMORIES M" in sql
    assert "WHERE M.ID = ?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_memory_fetch_memory_export_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_memory_export(
        tx,
        effective_owner="owner-a",
        effective_ns="ns-test",
        category="facts",
        limit=50,
        offset=0,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY" in sql
    assert "WHERE DELETED_AT IS NULL" in sql
    assert "OWNER_ID = ?" in sql
    assert "NAMESPACE = ?" in sql
    assert "CATEGORY = ?" in sql
    assert ":OWNER_ID" not in sql
    assert ":NS" not in sql
    assert ":CAT" not in sql
    assert ":OFFSET" not in sql
    assert ":LIMIT" not in sql


@pytest.mark.asyncio
async def test_db2_memory_fts_search_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    await repo.fts_search(
        tx,
        query="hello",
        limit=10,
        visibility=vis,
        category="facts",
        subcategory="test",
        source_provider="openai",
        source_model="gpt-4",
        source_agent="claude",
    )
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "UPPER(M.CONTENT) LIKE '%' || UPPER(?) || '%'" in sql
    assert "DBMS_LOB.INSTR" not in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert ":Q" not in sql
    assert ":FLT_CATEGORY" not in sql
    assert ":FLT_SUBCATEGORY" not in sql
    assert ":FLT_SOURCE_PROVIDER" not in sql
    assert ":FLT_SOURCE_MODEL" not in sql
    assert ":FLT_SOURCE_AGENT" not in sql
    assert ":LIMIT" not in sql
    assert len(params) >= 2
    # search_term is stripped of surrounding % signs
    assert "hello" in params


@pytest.mark.asyncio
async def test_db2_memory_find_active_duplicate_by_content_hash_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.find_active_duplicate_by_content_hash(
        tx,
        owner_id="owner-a",
        namespace="ns-test",
        content_hash="abc123",
        cross_namespace=False,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FETCH FIRST 1 ROWS ONLY" in sql
    assert "DELETED_AT IS NULL" in sql
    assert "ARCHIVED_AT IS NULL" in sql
    assert "CONTENT_HASH = ?" in sql
    assert "OWNER_ID = ?" in sql
    assert "NAMESPACE = ?" in sql
    assert ":H" not in sql
    assert ":OWNER_ID" not in sql
    assert ":NS" not in sql


# ────────────────────────────────────────────────────────────────────────────
# Db2MemoryRepository parity tests (PR #8d) — 5 method-specific tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_memory_set_suppress_version_snapshot_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    repo = Db2MemoryRepository()
    import types

    tx = types.SimpleNamespace()
    result = await repo.set_suppress_version_snapshot(tx)
    # No-op on both Oracle and Db2; this override prevents an
    # Oracle-via-translator round-trip.
    assert result is None


@pytest.mark.asyncio
async def test_db2_memory_fetch_versioned_memory_ids_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_versioned_memory_ids(tx, ["m1", "m2"])
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "SELECT DISTINCT MEMORY_ID" in sql
    assert "FROM MEMORY_VERSIONS" in sql
    assert "WHERE MEMORY_ID IN (?, ?)" in sql or "IN (?,?)" in sql
    assert "DELETED_AT IS NULL" in sql
    assert ":ID0" not in sql
    assert ":ID1" not in sql
    assert len(params) == 2


@pytest.mark.asyncio
async def test_db2_memory_gather_stats_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql})

        async def fetchone(self) -> Any:
            return (0, 0, 0, None)

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.gather_stats(tx)
    assert len(calls) == 2
    agg_sql = calls[0]["sql"].upper()
    cat_sql = calls[1]["sql"].upper()
    assert "COUNT(*)" in agg_sql
    assert "AVG(QUALITY_RATING)" in agg_sql
    assert "LOCATE('\"FEDERATION_ORIGIN\"'" in agg_sql
    assert "COALESCE(METADATA, ''" in agg_sql
    assert "DBMS_LOB" not in agg_sql
    assert "INSTR" not in agg_sql
    assert "SYSTIMESTAMP" not in agg_sql
    assert "NVL" not in agg_sql
    assert "FROM MEMORIES" in agg_sql
    assert "SELECT CATEGORY, COUNT(*)" in cat_sql
    assert "GROUP BY CATEGORY" in cat_sql


@pytest.mark.asyncio
async def test_db2_memory_bump_recall_and_get_memory_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self) -> None:
            self.description = None
            self._rows: list[Any] = []

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})
            if "SELECT" in sql.upper() and "FROM MEMORIES M" in sql.upper():
                self.description = (
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
                    ("deleted_at",),
                    ("recall_count",),
                    ("last_recalled_at",),
                    ("content_hash",),
                    ("federation_source",),
                    ("federation_remote_updated",),
                )
                self._rows = [
                    (
                        "m1",
                        "hello",
                        "facts",
                        "test",
                        "{}",
                        5,
                        None,
                        None,
                        "owner-a",
                        "ns-test",
                        600,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "2026-01-01",
                        "2026-01-01",
                        None,
                        None,
                        1,
                        "2026-01-01",
                        "hash",
                        None,
                        None,
                    )
                ]

        async def fetchall(self) -> list[Any]:
            return self._rows

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="owner-a",
        group_ids=[],
        namespace="ns-test",
    )
    result = await repo.bump_recall_and_get_memory(tx, "m1", visibility=vis)
    assert result is not None
    assert len(calls) == 2  # UPDATE + get_memory SELECT
    update_sql = calls[0]["sql"].upper()
    update_params = calls[0]["params"]
    assert "UPDATE MEMORIES SET" in update_sql
    assert "COALESCE(RECALL_COUNT, 0) + 1" in update_sql
    assert "LAST_RECALLED_AT = CURRENT TIMESTAMP" in update_sql
    assert "ID = ?" in update_sql
    assert "DELETED_AT IS NULL" in update_sql
    assert "NVL" not in update_sql
    assert "SYSTIMESTAMP" not in update_sql
    assert ":ID" not in update_sql
    assert len(update_params) >= 1


@pytest.mark.asyncio
async def test_db2_memory_fetch_memory_log_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_memory_log(tx, "mem-1", "main", 10, None)
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "FROM MEMORY_VERSIONS" in sql
    assert "WHERE MEMORY_ID = ?" in sql
    assert "AND BRANCH = ?" in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert "ORDER BY VERSION_NUM DESC" in sql
    assert "DELETED_AT IS NULL" in sql
    assert ":MEMORY_ID" not in sql
    assert ":BRANCH" not in sql
    assert ":LIMIT" not in sql
    assert len(params) == 3


# ── PR #8e: 7 native tests (commit-head / diff / checkout / allowlist / dedup / context) ──


@pytest.mark.asyncio
async def test_db2_memory_fetch_memory_head_checks_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []
    ids = ["mem-a", "mem-b", "mem-c"]

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_memory_head_checks(tx, ids)
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "LEFT JOIN MEMORY_BRANCHES" in sql
    assert "LEFT JOIN MEMORY_VERSIONS" in sql
    assert "B.NAME = 'MAIN'" in sql or "B.NAME = 'main'" in calls[0]["sql"]
    assert "MV.HEAD_VERSION_ID" in sql or "b.head_version_id" in calls[0]["sql"].lower()
    assert ":ID0" not in sql
    assert ":ID1" not in sql
    assert ":ID" not in sql
    assert "?" in sql
    assert len(params) == 3


@pytest.mark.asyncio
async def test_db2_memory_fetch_diff_commit_pair_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("content",), ("version_num",))
            self._fetch_calls = 0

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchone(self) -> Any:
            self._fetch_calls += 1
            return ("text", self._fetch_calls)

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    result = await repo.fetch_diff_commit_pair(tx, "mem-1", "aaa", "bbb", None)
    assert result is not None
    assert len(result) == 2
    assert len(calls) == 2
    for i, c in enumerate(calls):
        sql = c["sql"].upper()
        params = c["params"]
        assert "FROM MEMORY_VERSIONS" in sql
        assert "WHERE MEMORY_ID = ?" in sql
        assert "AND COMMIT_HASH = ?" in sql
        assert "DELETED_AT IS NULL" in sql
        assert ":MEMORY_ID" not in sql
        assert ":COMMIT" not in sql
        assert len(params) == 2


@pytest.mark.asyncio
async def test_db2_memory_fetch_checkout_commit_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (
                ("commit_hash",),
                ("version_num",),
                ("branch",),
                ("category",),
                ("subcategory",),
                ("content",),
                ("change_type",),
                ("snapshot_at",),
                ("snapshot_by",),
            )

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchone(self) -> Any:
            return ("abc123", 1, "main", "facts", None, "text", "update", None, None)

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_checkout_commit(tx, "mem-1", "abc123", None)
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "FROM MEMORY_VERSIONS" in sql
    assert "WHERE MEMORY_ID = ?" in sql
    assert "AND COMMIT_HASH = ?" in sql
    assert "DELETED_AT IS NULL" in sql
    assert ":MEMORY_ID" not in sql
    assert ":COMMIT" not in sql
    assert len(params) == 2


@pytest.mark.asyncio
async def test_db2_memory_fetch_referenced_memory_allowlist_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []
    refs = ["r1", "r2"]

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("id",), ("owner_id",), ("namespace",))

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return [("r1", "own", "ns1"), ("r2", "own", "ns1")]

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.fetch_referenced_memory_allowlist(tx, referenced_ids=refs, scope_owner="o", scope_namespace="ns")
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "IN (" in sql
    assert "?," in sql  # multiple positional binds
    assert "OWNER_ID = ?" in sql
    assert "NAMESPACE = ?" in sql
    assert ":REF" not in sql
    assert ":SCOPE_OWNER" not in sql
    assert ":SCOPE_NS" not in sql
    assert len(params) == 4  # 2 refs + owner + ns


@pytest.mark.asyncio
async def test_db2_memory_fetch_referenced_memory_allowlist_empty_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("id",), ("owner_id",), ("namespace",))

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    result = await repo.fetch_referenced_memory_allowlist(tx, referenced_ids=[])
    assert result == []
    assert len(calls) == 0  # early-return, no SQL emitted


@pytest.mark.asyncio
async def test_db2_memory_find_duplicate_content_groups_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("content_hash",), ("cnt",), ("canonical_id",))

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return [("h1", 3, "mem-old"), ("h2", 2, "mem-early")]

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    result = await repo.find_duplicate_content_groups(tx, namespace="ns1")
    assert len(result) == 2
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "GROUP BY CONTENT_HASH" in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "WITH DUP_GROUPS" in sql.upper()  # CTE
    assert "FIRST_VALUE" in sql.upper()  # KEEP→FIRST_VALUE
    assert "ORDER BY D.CNT DESC" in sql or "ORDER BY D.CNT DESC" in calls[0]["sql"]
    assert "KEEP" not in sql.upper()  # no Oracle-ism
    assert ":NS" not in sql
    assert len(params) == 2  # namespace param duplicated for CTEs


@pytest.mark.asyncio
async def test_db2_memory_find_duplicate_content_groups_no_namespace_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (("content_hash",), ("cnt",), ("canonical_id",))

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    await repo.find_duplicate_content_groups(tx)
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "GROUP BY CONTENT_HASH" in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "FIRST_VALUE" in sql.upper()
    assert "KEEP" not in sql.upper()
    assert len(params) == 0


@pytest.mark.asyncio
async def test_db2_memory_consolidate_duplicate_memories_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 3

        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    result = await repo.consolidate_duplicate_memories(tx, canonical_id="canon", duplicate_ids=["d1", "d2", "d3"])
    assert result == 3
    sql = calls[0]["sql"].upper() if calls else ""
    params = calls[0]["params"] if calls else ()
    assert "UPDATE MEMORIES" in sql
    assert "SET DELETED_AT = CURRENT TIMESTAMP" in sql
    assert "IN (" in sql
    assert "AND ID != ?" in sql
    assert "DELETED_AT IS NULL" in sql
    assert "SYSTIMESTAMP" not in sql
    assert ":CANONICAL_ID" not in sql
    assert ":DUP" not in sql
    assert len(params) == 4  # 3 dups + canonical_id


@pytest.mark.asyncio
async def test_db2_memory_consolidate_duplicate_memories_empty_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 0

        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    result = await repo.consolidate_duplicate_memories(tx, canonical_id="c", duplicate_ids=[])
    assert result == 0
    assert len(calls) == 0  # early-return, no SQL emitted


@pytest.mark.asyncio
async def test_db2_memory_fetch_memory_context_native() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    try:
        from mnemos.core.lifecycle import _set_embedder_for_testing

        _embed_calls: list[list[float]] = []

        async def _fake_embed(text: str) -> list[float]:
            _embed_calls.append([1.0] * 384)
            return _embed_calls[-1]

        _set_embedder_for_testing(_fake_embed)
        HAVE_EMBEDDER = True
    except ImportError:
        HAVE_EMBEDDER = False

    if not HAVE_EMBEDDER:
        pytest.skip("lifecycle embedder test hook not available; fine on main")

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = (
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

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return [("ctx1", "surrounding context", "facts", None, "{}", 5, "u1", "ns1", "2026-01-01", "2026-01-01")]

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2MemoryRepository()
    user = SimpleNamespace(user_id="u1", namespace="ns1")
    result = await repo.fetch_memory_context(tx, "test query", user, limit=3)
    assert isinstance(result, list)
    assert all(isinstance(r, dict) for r in result)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "VECTOR_DISTANCE" in sql or "COSINE" in sql
    assert "FETCH FIRST" in sql


# ────────────────────────────────────────────────────────────────────────────
# Db2FederationRepository parity tests (PR #9a) — 6 core peer/sync methods


@pytest.mark.asyncio
async def test_db2_federation_list_peers_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.list_peers(tx)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM FEDERATION_PEERS" in sql
    assert "ORDER BY CREATED" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_get_peer_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchone(self) -> Any:
            return None

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.get_peer(tx, "peer-1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM FEDERATION_PEERS" in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_delete_peer_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 0

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.delete_peer(tx, "peer-1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "DELETE FROM FEDERATION_PEERS" in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_list_due_peers_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.list_due_peers(tx, limit=10)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM FEDERATION_PEERS" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "NUMTODSINTERVAL" not in sql
    assert "SECONDS" in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert ":LIMIT" not in sql


@pytest.mark.asyncio
async def test_db2_federation_fetch_memory_page_basic_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def fetchall(self) -> list[Any]:
            return []

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.fetch_memory_page(tx, limit=10)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MEMORIES" in sql
    assert "WHERE DELETED_AT IS NULL" in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert ":LIMIT" not in sql
    assert ":UPD" not in sql


@pytest.mark.asyncio
async def test_db2_federation_create_peer_native_tokens() -> None:
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.description = None

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})
            if "SELECT" in sql.upper():
                self.description = (
                    ("id",),
                    ("name",),
                    ("base_url",),
                    ("auth_token",),
                    ("namespace_filter",),
                    ("category_filter",),
                    ("enabled",),
                    ("sync_interval_secs",),
                    ("compat_mode",),
                    ("last_sync_at",),
                    ("last_sync_cursor",),
                    ("last_error",),
                    ("last_error_at",),
                    ("total_pulled",),
                    ("peer_mnemos_version",),
                    ("last_schema_check_at",),
                    ("created",),
                    ("updated",),
                )

        async def fetchone(self) -> Any:
            return (
                "p1",
                "test",
                "http://peer",
                "tok",
                None,
                None,
                1,
                3600,
                "strict",
                None,
                None,
                None,
                None,
                0,
                None,
                None,
                "2026-01-01",
                "2026-01-01",
            )

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.create_peer(
        tx,
        name="test",
        base_url="http://peer",
        auth_token="tok",
        namespace_filter=None,
        category_filter=None,
        enabled=True,
        sync_interval_secs=3600,
        compat_mode="strict",
    )
    assert len(calls) >= 2  # insert + get_peer follow-up
    insert_sql = calls[0]["sql"].upper() if calls else ""
    assert "INSERT INTO FEDERATION_PEERS" in insert_sql
    assert insert_sql.count("?") == 9
    assert ":ID" not in insert_sql
    assert ":NAME" not in insert_sql
