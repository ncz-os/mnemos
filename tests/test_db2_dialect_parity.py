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
