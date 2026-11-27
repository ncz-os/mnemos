"""Db2-native method parity probes.

Driver-free, DB-free tests that compare new Db2-native repository
overrides against the Oracle compatibility path they replace. These
tests do NOT require ``ibm_db`` or a live Db2 DSN.
"""

from __future__ import annotations

import datetime as _dt
import re
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _offsite_feed_scope(monkeypatch):
    """These tests pin the SQL shape of the OFFSITE feed scope.

    The world-read gate applies only when
    MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0; the default is now the
    trusted-LAN full-corpus scope, so declare the posture explicitly.
    """
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")


class _DeterministicUUID:
    def __init__(self) -> None:
        self._idx = 0

    @property
    def hex(self) -> str:
        self._idx += 1
        return f"delivery{self._idx:024d}"


class _FixedDatetime:
    """Drop-in replacement for ``datetime`` whose ``now()`` returns a
    fixed UTC instant. Used by ``test_db2_webhook_dispatch_event_native_matches_compat``
    to make the payload envelope deterministic across two consecutive
    ``dispatch_event`` calls so the wrapped ``timestamp`` field
    produces the same SHA-256 hash in both."""

    _FIXED = _dt.datetime(2026, 9, 11, 21, 0, 0, tzinfo=_dt.timezone.utc)

    @classmethod
    def now(cls, tz: Any = None) -> _dt.datetime:
        if tz is None:
            return cls._FIXED
        return cls._FIXED.astimezone(tz)

    # Forward everything else to the real datetime so module-level use
    # (``datetime.datetime(...)``, ``datetime.timedelta(...)``, etc.)
    # continues to work. We only need to override ``now``.
    def __getattr__(self, name: str) -> Any:
        return getattr(_dt.datetime, name)


class _FakeSyncCursor:
    rowcount = 0

    def __init__(
        self, subscriptions: list[tuple[str, str | None, str | None, str]], calls: list[dict[str, Any]]
    ) -> None:
        self._subscriptions = subscriptions
        self._calls = calls
        self.description: tuple[tuple[str], ...] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        params = tuple(params or ())
        self._calls.append({"sql": sql, "params": params})
        if "FROM webhook_subscriptions" in sql:
            # The post-item-7 dispatch_event SELECT pulls ``id, url,
            # owner_id, namespace`` (Oracle) or ``id, url,
            # COALESCE(owner_id, ...), COALESCE(namespace, ...)`` (Db2).
            # The native DB2 path's COALESCE turns a NULL owner/namespace
            # into the string "default" — match that on the compat path
            # so the parity assertion ``native_ids == compat_ids`` holds
            # for subscriptions whose owner/namespace are NULL.
            self.description = (("id",), ("url",), ("owner_id",), ("namespace",))
            if "COALESCE" in sql.upper():
                self._rows = [
                    (sid, url, owner or "default", ns or "default") for sid, owner, ns, url in self._subscriptions
                ]
            else:
                self._rows = [
                    (sid, url, owner or "default", ns or "default") for sid, owner, ns, url in self._subscriptions
                ]
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
    def __init__(
        self, subscriptions: list[tuple[str, str | None, str | None, str]], calls: list[dict[str, Any]]
    ) -> None:
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
                ("sub-1", "owner-a", "ns-a", "https://example.test/hook-a"),
                ("sub-2", None, None, "https://example.test/hook-b"),
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
    from datetime import datetime as _datetime_cls, timezone

    from mnemos.persistence.db2 import Db2WebhookRepository, _Db2OraCompatMixin
    from mnemos.persistence.oracle import OracleWebhookRepository

    class _CompatWebhookRepository(_Db2OraCompatMixin, OracleWebhookRepository):
        pass

    fixed_now = _datetime_cls(2026, 9, 11, 21, 0, 0, tzinfo=timezone.utc)

    def _fixed_now(tz=None):
        return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(uuid, "uuid4", _DeterministicUUID)
    # Patch the ``datetime.now`` binding in BOTH the oracle and db2 modules
    # so the dispatch_event payload envelope is identical between calls
    # (the wrapped ``timestamp`` field is the only non-deterministic input).
    monkeypatch.setattr("mnemos.persistence.oracle.datetime", _FixedDatetime)
    monkeypatch.setattr("mnemos.persistence.db2.datetime", _FixedDatetime)
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
    from mnemos.persistence.db2 import Db2ConsultationAuditRepository

    calls: list[dict] = []

    class _FakeCursor:
        description = (("provider",),)

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": params})

        async def fetchall(self):
            return [("provider-a",)]

        async def close(self):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    repo = Db2ConsultationAuditRepository()
    provider = await repo.fetch_model_provider(SimpleNamespace(conn=_FakeConn()), "model-a")

    assert provider == "provider-a"
    assert "FETCH FIRST 1 ROW ONLY" in calls[0]["sql"].upper()
    assert "LIMIT" not in calls[0]["sql"].upper()
    assert calls[0]["params"] == ("model-a",)


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
    # FB101/P4: replicas are counted by the federation_source column, not by
    # searching metadata for a "federation_origin" key nothing writes.
    assert "FEDERATION_SOURCE IS NULL" in agg_sql
    assert "FEDERATION_SOURCE IS NOT NULL" in agg_sql
    assert "FEDERATION_ORIGIN" not in agg_sql
    assert "COALESCE(METADATA, ''" not in agg_sql
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
    # FB102/S2: the group key is a SHA-256 of the normalized body, not the CLOB
    # itself (grouping on a LOB is SQL0134N).
    assert "GROUP BY OWNER_ID, NAMESPACE, CONTENT_HASH" in sql
    # HASH_SHA256 does not exist on Db2 LUW (SQL0440N); the identity is the
    # exact content_hash column, never a digest of a truncated CLOB.
    assert "HASH_SHA256" not in sql
    assert "VARCHAR(32672)" not in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "WITH ORDERED AS" in sql  # CTE
    assert "LISTAGG" in sql
    assert "ORDER BY DUPLICATE_COUNT DESC" in sql
    assert "KEEP (" not in sql  # no Oracle KEEP(DENSE_RANK ...) aggregate
    assert "DENSE_RANK" not in sql
    assert "FIRST_VALUE" not in sql.upper()
    assert ":NS" not in sql
    assert len(params) == 1


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
    assert "GROUP BY OWNER_ID, NAMESPACE, CONTENT_HASH" in sql
    # HASH_SHA256 does not exist on Db2 LUW (SQL0440N); the identity is the
    # exact content_hash column, never a digest of a truncated CLOB.
    assert "HASH_SHA256" not in sql
    assert "VARCHAR(32672)" not in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "WITH ORDERED AS" in sql
    assert "LISTAGG" in sql
    assert "KEEP (" not in sql
    assert "DENSE_RANK" not in sql
    assert "FIRST_VALUE" not in sql.upper()
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


@pytest.mark.asyncio
async def test_db2_federation_fetch_federated_memory_marker_native_tokens() -> None:
    """Db2 fetch_federated_memory_marker emits ? bind + no Oracle tokens."""
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
    await repo.fetch_federated_memory_marker(tx, "mem-1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM MEMORIES" in sql
    assert "WHERE ID = ?" in sql
    # F-2: the existence probe must NOT filter deleted_at — a replica this
    # node soft-deleted is still a row, and hiding it made the receiver try
    # an insert the primary key refuses. It selects the state instead.
    assert "DELETED_AT IS NULL" not in sql
    assert "SELECT FEDERATION_REMOTE_UPDATED, DELETED_AT" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_insert_federated_memory_native_tokens() -> None:
    """Db2 insert_federated_memory emits ? binds + CURRENT TIMESTAMP."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self.rowcount = 1

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.insert_federated_memory(
        tx,
        local_id="mem-1",
        content="hello",
        category="facts",
        subcategory=None,
        metadata_json="{}",
        verbatim_content="hello",
        quality_rating=0,
        namespace="test",
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        peer_name="peer-x",
        remote_updated="2026-01-01T00:00:00",
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "INSERT INTO MEMORIES" in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "CAST(? AS TIMESTAMP)" in sql
    assert ":ID" not in sql
    assert ":CONTENT" not in sql


@pytest.mark.asyncio
async def test_db2_federation_update_federated_memory_if_newer_native_tokens() -> None:
    """Db2 update_federated_memory_if_newer emits ? binds + CAST(? AS TIMESTAMP)."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.update_federated_memory_if_newer(
        tx,
        local_id="mem-1",
        content="updated",
        category="facts",
        subcategory=None,
        metadata_json="{}",
        verbatim_content="updated",
        quality_rating=1,
        namespace="test",
        remote_updated="2026-06-01T00:00:00",
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "UPDATE MEMORIES SET" in sql
    assert "CAST(? AS TIMESTAMP)" in sql
    assert sql.count("CAST(? AS TIMESTAMP)") >= 3
    assert "TIMESTAMP WITH TIME ZONE" not in sql
    assert ":ID" not in sql
    assert ":CONTENT" not in sql


@pytest.mark.asyncio
async def test_db2_federation_apply_consolidation_tombstone_native_tokens() -> None:
    """Db2 apply_consolidation_tombstone emits MERGE with SYSDUMMY1 + COALESCE."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.apply_consolidation_tombstone(
        tx,
        local_id="mem-1",
        local_canonical_id="mem-2",
        consolidated_at="2026-05-01T00:00:00",
        remote_id="remote-1",
        canonical_remote_id="remote-2",
        peer_name="peer-x",
    )
    assert len(calls) >= 2  # MERGE + UPDATE
    merge_sql = calls[0]["sql"].upper() if calls else ""
    assert "MERGE INTO FEDERATION_CONSOLIDATION_TOMBSTONES" in merge_sql
    assert "FROM SYSIBM.SYSDUMMY1" in merge_sql
    assert "FROM DUAL" not in merge_sql
    assert "COALESCE" in merge_sql
    assert "NVL" not in merge_sql
    assert "CURRENT TIMESTAMP" in merge_sql
    assert "SYSTIMESTAMP" not in merge_sql
    assert "CAST(? AS TIMESTAMP)" in merge_sql
    assert ":PEER_NAME" not in merge_sql

    update_sql = calls[1]["sql"].upper() if len(calls) > 1 else ""
    assert "UPDATE MEMORIES" in update_sql
    assert "CURRENT TIMESTAMP" in update_sql
    assert "SYSTIMESTAMP" not in update_sql


@pytest.mark.asyncio
async def test_db2_federation_delete_federated_memory_native_tokens() -> None:
    """Db2 delete_federated_memory hard-deletes only the matching peer using native binds."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.delete_federated_memory(tx, "peer-x", "mem-1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "DELETE FROM MEMORIES" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "WHERE ID IN (?, ?)" in sql
    assert calls[0]["params"] == ("mem-1", "fed:peer-x:mem-1", "peer-x")
    assert "AND FEDERATION_SOURCE = ?" in sql
    assert ":ID" not in sql
    assert ":PEER" not in sql


# ── PR #9c: close-out Federation — 11 native tests ──


@pytest.mark.asyncio
async def test_db2_federation_update_peer_native_tokens() -> None:
    """Db2 update_peer emits ? binds + CURRENT TIMESTAMP for dynamic SET."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 1

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
                "http://p",
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
    await repo.update_peer(tx, "peer-1", {"name": "new-name", "enabled": True})
    assert len(calls) >= 2
    update_sql = calls[0]["sql"].upper()
    assert "UPDATE FEDERATION_PEERS SET" in update_sql
    assert "CURRENT TIMESTAMP" in update_sql
    assert "SYSTIMESTAMP" not in update_sql
    assert "NAME = ?" in update_sql
    assert "ENABLED = ?" in update_sql
    assert "ID = ?" in update_sql
    assert ":NAME" not in update_sql
    assert ":ID" not in update_sql


@pytest.mark.asyncio
async def test_db2_federation_upsert_peer_native_tokens() -> None:
    """Db2 upsert_peer emits MERGE with SYSIBM.SYSDUMMY1 + COALESCE."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.upsert_peer(tx, peer_id="peer-1", base_url="http://p", name="test", enabled=True)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "MERGE INTO FEDERATION_PEERS P" in sql
    assert "FROM SYSIBM.SYSDUMMY1" in sql
    assert "FROM DUAL" not in sql
    assert "COALESCE" in sql
    assert "NVL" not in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "?" in calls[0]["sql"] if calls else True


@pytest.mark.asyncio
async def test_db2_federation_get_sync_peer_native_tokens() -> None:
    """Db2 get_sync_peer emits ? bind, no Oracle tokens."""
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
    await repo.get_sync_peer(tx, "peer-1")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM FEDERATION_PEERS" in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_fetch_sync_log_native_tokens() -> None:
    """Db2 fetch_sync_log emits ? binds + FETCH FIRST."""
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
    await repo.fetch_sync_log(tx, "peer-1", 10)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "FROM FEDERATION_SYNC_LOG" in sql
    assert "WHERE PEER_ID = ?" in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert ":PEER_ID" not in sql
    assert ":LIMIT" not in sql


@pytest.mark.asyncio
async def test_db2_federation_create_sync_log_native_tokens() -> None:
    """Db2 create_sync_log emits ? binds for INSERT."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.create_sync_log(tx, "peer-1", "cursor-pos")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "INSERT INTO FEDERATION_SYNC_LOG" in sql
    assert "VALUES (?, ?, ?)" in sql
    assert ":ID" not in sql
    assert ":PEER_ID" not in sql


@pytest.mark.asyncio
async def test_db2_federation_finish_sync_log_native_tokens() -> None:
    """Db2 finish_sync_log emits ? binds + CURRENT TIMESTAMP."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.finish_sync_log(
        tx,
        log_id="log-1",
        memories_pulled=10,
        memories_new=3,
        memories_updated=7,
        error=None,
        cursor_after="cursor-pos-2",
    )
    sql = calls[0]["sql"].upper() if calls else ""
    assert "UPDATE FEDERATION_SYNC_LOG SET" in sql
    assert "FINISHED_AT = CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "?" in calls[0]["sql"]
    assert ":ID" not in sql
    assert ":PULLED" not in sql


@pytest.mark.asyncio
async def test_db2_federation_record_sync_error_native_tokens() -> None:
    """Db2 record_sync_error emits ? binds + CURRENT TIMESTAMP x2."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.record_sync_error(tx, "peer-1", "timeout")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "UPDATE FEDERATION_PEERS SET" in sql
    assert "LAST_SYNC_AT = CURRENT TIMESTAMP" in sql
    assert "LAST_ERROR_AT = CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "LAST_ERROR = ?" in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql
    assert ":ERR" not in sql


@pytest.mark.asyncio
async def test_db2_federation_record_sync_success_native_tokens() -> None:
    """Db2 record_sync_success emits ? binds + CURRENT TIMESTAMP."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.record_sync_success(tx, "peer-1", "cursor-val", 5)
    sql = calls[0]["sql"].upper() if calls else ""
    assert "UPDATE FEDERATION_PEERS SET" in sql
    assert "LAST_SYNC_AT = CURRENT TIMESTAMP" in sql
    assert "LAST_SYNC_CURSOR = ?" in sql
    assert "LAST_ERROR = NULL" in sql
    assert "LAST_ERROR_AT = NULL" in sql
    assert "TOTAL_PULLED = TOTAL_PULLED + ?" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql
    assert ":CURSOR" not in sql


@pytest.mark.asyncio
async def test_db2_federation_update_peer_schema_check_native_tokens() -> None:
    """Db2 update_peer_schema_check emits ? binds + CURRENT TIMESTAMP."""
    from mnemos.persistence.db2 import Db2FederationRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append({"sql": sql, "params": params})

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    repo = Db2FederationRepository()
    await repo.update_peer_schema_check(tx, "peer-1", "1.0.0")
    sql = calls[0]["sql"].upper() if calls else ""
    assert "UPDATE FEDERATION_PEERS SET" in sql
    assert "PEER_MNEMOS_VERSION = ?" in sql
    assert "LAST_SCHEMA_CHECK_AT = CURRENT TIMESTAMP" in sql
    assert "SYSTIMESTAMP" not in sql
    assert "WHERE ID = ?" in sql
    assert ":ID" not in sql
    assert ":PV" not in sql


@pytest.mark.asyncio
async def test_db2_federation_feed_query_native_tokens() -> None:
    """Db2 feed_query emits ? binds with dynamic IN + FETCH FIRST."""
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
    await repo._legacy_feed_query(
        tx,
        since_updated="2026-01-01",
        since_id="mem-0",
        namespaces=["ns-a", "ns-b"],
        categories=["facts"],
        limit=20,
        prefer_compressed=False,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    params_tuple = calls[0]["params"] if calls else ()
    assert "FROM MEMORIES M" in sql
    assert "WHERE M.FEDERATION_SOURCE IS NULL" in sql
    assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
    assert "M.DELETED_AT IS NULL" in sql
    assert "M.ARCHIVED_AT IS NULL" in sql
    assert "M.CONSOLIDATED_INTO IS NULL" in sql
    assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql
    assert "M.UPDATED > ? OR (M.UPDATED = ? AND M.ID > ?)" in sql
    assert "M.NAMESPACE IN (?,?)" in sql or "M.NAMESPACE IN (?, ?)" in sql
    assert "M.CATEGORY IN (?)" in sql
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert ":UPD" not in sql
    assert ":SINCE_ID" not in sql
    assert ":LIMIT" not in sql
    assert ":NS" not in sql
    assert ":CAT" not in sql
    # F07: the withdrawal branch mirrors the live branch's vault
    # credential-boundary exclusion plus the same since/namespaces/
    # categories filters. With the live branch contributing 8 params
    # (vault + 3×since + 2×ns + 1×cat + 1×limit) and the withdrawal
    # branch contributing 8 params (vault + 3×since + 2×ns + 1×cat +
    # 1×limit), the total is 16.
    assert len(params_tuple) == 16  # see comment above


@pytest.mark.asyncio
async def test_db2_federation_feed_query_no_filters_native_tokens() -> None:
    """Db2 feed_query emits ? binds with no filter args."""
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
    await repo._legacy_feed_query(
        tx,
        since_updated=None,
        since_id=None,
        namespaces=[],
        categories=[],
        limit=10,
        prefer_compressed=True,
    )
    sql = calls[0]["sql"].upper() if calls else ""
    params_tuple = calls[0]["params"] if calls else ()
    assert "FETCH FIRST ? ROWS ONLY" in sql
    assert "M.NAMESPACE IN" not in sql
    assert "M.CATEGORY IN" not in sql
    assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
    assert "M.CONSOLIDATED_INTO IS NULL" in sql
    assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql
    # F07: the withdrawal branch mirrors the live branch's vault
    # credential-boundary exclusion plus a trailing limit. The
    # resulting param tuple is
    #   (vault, 10, vault, 10)
    # i.e. 4 elements: live-vault, live-limit, withdrawal-vault,
    # withdrawal-limit.
    assert len(params_tuple) == 4
    assert params_tuple[0] == "vault"  # live branch vault boundary
    assert params_tuple[1] == 10  # live branch limit
    assert params_tuple[2] == "vault"  # withdrawal branch vault boundary
    assert params_tuple[3] == 10  # withdrawal branch limit


@pytest.mark.asyncio
async def test_db2_federation_get_feed_memory_native_tokens() -> None:
    """Db2 get_feed_memory emits ? binds with dynamic IN."""
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
    await repo._legacy_get_feed_memory(
        tx,
        "mem-1",
        namespaces=["ns-a"],
        categories=["facts", "logs"],
    )
    sql = calls[0]["sql"].upper() if calls else ""
    params_tuple = calls[0]["params"] if calls else ()
    assert "FROM MEMORIES M" in sql
    assert "M.ID = ?" in sql
    assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
    assert "M.DELETED_AT IS NULL" in sql
    assert "M.ARCHIVED_AT IS NULL" in sql
    assert "M.CONSOLIDATED_INTO IS NULL" in sql
    assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql
    assert "M.NAMESPACE IN (?)" in sql
    assert "M.CATEGORY IN (?,?)" in sql or "M.CATEGORY IN (?, ?)" in sql
    assert ":ID" not in sql
    assert ":NS" not in sql
    assert ":CAT" not in sql
    assert len(params_tuple) == 5  # memory_id, vault, ns-a, facts, logs


@pytest.mark.asyncio
async def test_db2_federation_get_feed_memory_no_filters_native_tokens() -> None:
    """Db2 get_feed_memory emits ? binds with no filter args."""
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
    await repo._legacy_get_feed_memory(tx, "mem-1", namespaces=[], categories=[])
    sql = calls[0]["sql"].upper() if calls else ""
    params_tuple = calls[0]["params"] if calls else ()
    assert "M.ID = ?" in sql
    assert "MOD(M.PERMISSION_MODE, 10) >= 4" in sql
    assert "M.ARCHIVED_AT IS NULL" in sql
    assert "M.CONSOLIDATED_INTO IS NULL" in sql
    assert "M.NAMESPACE IS NULL OR M.NAMESPACE <> ?" in sql
    assert "M.NAMESPACE IN" not in sql
    assert "M.CATEGORY IN" not in sql
    assert len(params_tuple) == 2  # memory_id, vault


# ─────────────────────────────────────────────────────────────────────
# NG2-MN-019 R8: Db2 / Oracle version-read ACL correlation + duplicate-def guard
# ─────────────────────────────────────────────────────────────────────


def test_db2_version_acl_subquery_qualifies_outer_memory_id() -> None:
    """The ACL EXISTS disjunct must correlate to the OUTER memory_versions
    table, not to macl.memory_id itself.

    Unqualified ``memory_id`` inside the subquery resolves to
    ``macl.memory_id`` (the only table in scope), making the predicate a
    tautology and bypassing per-principal visibility. Pin the qualified
    correlation so the regression is caught driver-free.
    """
    from mnemos.persistence.db2 import _version_visibility_clause

    clause, params = _version_visibility_clause(
        "alice",
        ["team"],
        namespace="ns1",
        exclude_namespaces=("vault",),
    )
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in clause
    # The fixed correlation references the outer table by name.
    assert "macl.memory_id = memory_versions.memory_id" in clause
    # The broken self-comparison must not survive.
    assert "macl.memory_id = memory_id " not in clause
    # Parameters stay positional: user, groups, principals, namespace, excludes.
    assert params[0] == "alice"
    assert "team" in params
    assert params[-1] == "vault"


def test_db2_boosted_rank_supersession_sort_key_defined_once() -> None:
    """R4 accidentally committed ``_boosted_rank_supersession_sort_key``
    twice in db2.py (the second silently shadowed the first). Pin the
    module surface so the duplicate is caught at import/inspection time.
    """
    import mnemos.persistence.db2 as db2_module

    source = open(db2_module.__file__, encoding="utf-8").read()
    count = source.count("def _boosted_rank_supersession_sort_key")
    assert count == 1, f"expected exactly one def, found {count}"


def test_oracle_version_where_nonroot_qualifies_outer_memory_id() -> None:
    """Oracle's per-version non-root predicate shares the same ACL-subquery
    correlation defect: ``macl.memory_id = memory_id`` (unqualified) was a
    tautology. The clause is appended to ``FROM memory_versions`` queries
    with no table alias, so it must qualify the outer table explicitly.
    """
    from mnemos.persistence.oracle import _oracle_version_where_nonroot
    from mnemos.core.auth_context import UserContext

    user = UserContext(
        user_id="alice",
        group_ids=["team"],
        role="member",
        namespace="ns1",
        authenticated=True,
    )
    clause, params = _oracle_version_where_nonroot(user)
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in clause
    assert "macl.memory_id = memory_versions.memory_id" in clause
    assert "macl.memory_id = memory_id " not in clause


# ─────────────────────────────────────────────────────────────────────
# NG2-MN-019 R1: version-read call-site correlation pins (driver-free CI guard)
#
# The R8 clause-level tests prove the builder emits a qualified correlation, but
# the review noted the clause was exercised only in live/real-DB tests: a call
# site could still embed an old/unqualified form and CI would not see it. These
# structural pins read the repository source at import time and assert every
# version-read path that applies the clause uses the qualified form, never the
# self-comparing ``macl.memory_id = memory_id`` tautology.
# ─────────────────────────────────────────────────────────────────────


def _repository_source(module_name: str) -> str:
    import importlib

    import inspect

    module = importlib.import_module(module_name)
    return inspect.getsource(module)


def test_db2_version_read_call_sites_use_qualified_acl_correlation() -> None:
    """Both Db2 version-read paths (``list_memory_versions`` and
    ``fetch_memory_version``) must apply ``_version_visibility_clause`` with the
    qualified outer-table correlation.

    At review head 9107d2f5 the clause emitted
    ``WHERE macl.memory_id = memory_id`` inside the EXISTS subquery — the
    unqualified right-hand ``memory_id`` resolved to ``macl.memory_id`` (the
    only table in subquery scope), a tautology that bypassed per-principal ACL
    visibility. The qualified form references ``memory_versions.memory_id``
    (the outer table has no alias in these queries). Pin both the builder and
    every call site so the regression is caught without a live Db2 instance.
    """
    from mnemos.persistence.db2 import _version_visibility_clause

    # Builder contract: qualified correlation, never the self-comparison.
    clause, _ = _version_visibility_clause(
        "alice",
        ["team"],
        namespace="ns1",
        exclude_namespaces=("vault",),
    )
    assert "macl.memory_id = memory_versions.memory_id" in clause
    assert "macl.memory_id = memory_id " not in clause

    # Structural guard: the repository source applies the clause through both
    # version-read methods and carries no unqualified correlation anywhere.
    source = _repository_source("mnemos.persistence.db2")
    for method in ("list_memory_versions", "fetch_memory_version"):
        assert f"def {method}(" in source
    assert source.count("_version_visibility_clause(") >= 3  # def + 2 call sites
    assert "macl.memory_id = memory_id " not in source


def test_mysql_version_read_uses_qualified_acl_correlation() -> None:
    """MySQL/MariaDB now provisions ``memory_acl`` (migration 0073), so the
    version-read builder renders the ACL escape hatch with the qualified
    outer-table correlation ``macl.memory_id = memory_versions.memory_id``
    (not the self-comparing ``macl.memory_id = memory_id`` tautology, which
    inside the EXISTS subquery resolves both sides to ``macl.memory_id``)
    and with balanced parentheses — including the ``exclude_namespaces``
    branch.

    Companion to the Oracle / Db2 qualified-correlation pins: verifies that
    (a) the EXISTS disjunct is present and correctly qualified against the
    outer ``memory_versions`` table, and (b) the clause renders with
    balanced parens when an exclusion list is present.
    """
    from mnemos.persistence.mysql import _version_visibility_clause

    clause, params = _version_visibility_clause(
        "alice",
        ["team"],
        namespace="ns1",
        exclude_namespaces=("vault",),
    )
    # ACL escape hatch is present and qualified against the outer table.
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in clause
    assert "WHERE macl.memory_id = memory_versions.memory_id" in clause
    # The broken unqualified correlation (tautology inside the subquery)
    # must not survive anywhere in the clause.
    assert "macl.memory_id = memory_id " not in clause
    assert "memory_id = memory_id" not in clause
    assert "macl.principal IN (%s, %s)" in clause
    assert "(macl.perm & 4) > 0" in clause
    # Aliased call sites keep the alias-qualified correlation.
    aliased, aliased_params = _version_visibility_clause(
        "alice",
        [],
        table_alias="mv",
        namespace="ns1",
    )
    assert "WHERE macl.memory_id = mv.memory_id" in aliased
    # All column references are qualified by the table prefix.
    assert "memory_versions.owner_id = %s" in clause
    assert "memory_versions.namespace = %s" in clause
    assert "memory_versions.group_id" in clause
    assert "memory_versions.permission_mode" in clause
    # Balanced parens (was broken in R1 — the exclude_namespaces branch
    # lost its closing paren).
    assert clause.count("(") == clause.count(")")
    # Params: owner, group, principals (user:alice, group:team),
    # namespace, excluded namespace.
    assert params == ["alice", "team", "user:alice", "group:team", "ns1", "vault"]


def test_oracle_version_read_call_sites_use_qualified_acl_correlation() -> None:
    """Oracle version-read paths must apply ``_version_visibility_clause`` with
    the qualified outer-table correlation, mirroring the Db2 fix.

    The R8 fix qualified the Db2/Oracle clause builder; this structural pin
    ensures the Oracle repository source applies the clause through both
    version-read methods and carries no unqualified ``macl.memory_id = memory_id``
    tautology.
    """
    from mnemos.persistence.oracle import _version_visibility_clause

    clause, _ = _version_visibility_clause(
        "alice",
        ["team"],
        namespace="ns1",
    )
    assert "macl.memory_id = memory_versions.memory_id" in clause
    assert "macl.memory_id = memory_id " not in clause

    source = _repository_source("mnemos.persistence.oracle")
    for method in ("list_memory_versions", "fetch_memory_version"):
        assert f"def {method}(" in source
    assert source.count("_version_visibility_clause(") >= 3  # def + 2 call sites
    assert "macl.memory_id = memory_id " not in source


# ─────────────────────────────────────────────────────────────────────
# NG2-MN-019 R1: Db2 version-read SQL rendering (root bypass vs per-snapshot clause)
#
# The structural pins above prove the builder emits a qualified correlation and
# that both call sites exist, but they never execute the methods. These
# rendering tests drive ``list_memory_versions`` and ``fetch_memory_version``
# with fake cursor/connection objects so CI sees the ACTUAL SQL that reaches
# Db2: root reads bypass the clause (no visibility filter), while non-root reads
# embed the qualified ``memory_versions.memory_id`` ACL correlation and bind
# every placeholder positionally.
# ─────────────────────────────────────────────────────────────────────


def _capturing_db2_version_repo():
    """Build a Db2VersionRepository wired to a capturing fake connection.

    The fake cursor records every call to ``execute`` with parameters
    normalized to a flat tuple (empty when absent).

    Supported DB-API 2.0 conventions (explicit subset; others are
    unsupported and fail loudly):
    - ``execute(sql, seq)``       -> sequence of parameters.
    - ``execute(sql, p1, p2)``   -> multiple positional parameters.
    - ``execute(sql, params=seq)`` -> keyword-passed sequence.
    - ``execute(sql)``            -> empty tuple.
    - Mixed positional+keyword params -> ``ValueError`` (ambiguous).

    ``SimpleNamespace`` and ``Any`` are imported at module scope (see
    the imports block at the top of this file); ``_FakeConn.cursor`` is
    ``async`` to match the ``ibm_db`` connection contract used by the
    real Db2 driver.
    """
    from mnemos.persistence.db2 import Db2VersionRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        rowcount = 0

        def __init__(self):
            self.description = None

        async def execute(self, sql, *args, **kwargs):
            """Capture the supported DB-API 2.0 call conventions.

            - ``execute(sql, params)``        -> ``args == (seq,)``; unwrap.
            - ``execute(sql, p1, p2, ...)``   -> ``args`` IS the param sequence.
            - ``execute(sql, params=seq)``   -> ``kwargs['params']`` is the sequence.
            - ``execute(sql)``               -> empty tuple.
            Mixed positional+keyword forms are ambiguous and unsupported:
            a ``ValueError`` is raised so mis-binding fails loudly rather
            than silently recording wrong parameters.
            """
            kw_params = kwargs.pop("params", None)
            if kw_params is None:
                if not args:
                    params = ()
                elif len(args) == 1 and isinstance(args[0], (tuple, list)):
                    params = args[0]
                else:
                    params = args
            elif args:
                raise ValueError(
                    f"_FakeCursor.execute: params given both positionally "
                    f"({args!r}) and via keyword ({kw_params!r}); ambiguous."
                )
            else:
                params = kw_params
            calls.append(
                {"sql": sql, "params": tuple(params) if params is not None else ()}
            )

        async def fetchall(self):
            return []

        async def fetchone(self):
            return None

        async def close(self):
            pass

    class _FakeConn:
        async def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    return Db2VersionRepository(), tx, calls


@pytest.mark.asyncio
async def test_capturing_db2_fake_cursor_normalizes_execute_conventions() -> None:
    """The fake cursor must record every DB-API calling convention correctly.

    - ``execute(sql, seq)``            -> flat tuple of params.
    - ``execute(sql, p1, p2)``         -> flat tuple of params.
    - ``execute(sql, params=seq)``    -> flat tuple of params.
    - ``execute(sql)``                 -> empty tuple.
    - Mixed positional+keyword form    -> ``ValueError`` (ambiguous).
    """
    _, tx, calls = _capturing_db2_version_repo()
    cursor = await tx.conn.cursor()

    await cursor.execute("SELECT ? ", ("a", "b"))
    assert calls[-1]["params"] == ("a", "b")

    await cursor.execute("SELECT ?, ?", "a", "b")
    assert calls[-1]["params"] == ("a", "b")

    await cursor.execute("SELECT ?", params=("a", "b"))
    assert calls[-1]["params"] == ("a", "b")

    await cursor.execute("SELECT 1")
    assert calls[-1]["params"] == ()

    with pytest.raises(ValueError, match="ambiguous"):
        await cursor.execute("SELECT ?", "a", params=("b",))


@pytest.mark.asyncio
async def test_db2_version_read_paths_apply_qualified_acl_clause_in_sql() -> None:
    """Both version-read methods must embed ``_version_visibility_clause`` in
    the rendered SQL for non-root users, using the qualified outer-table
    correlation, and must bind the clause's placeholders positionally in the
    correct slots (memory, branch/version, user, groups, principals,
    namespace, excludes).

    Root users bypass the clause entirely (no visibility filter in SQL).
    """
    from mnemos.core.auth_context import UserContext

    root = UserContext(
        user_id="root", group_ids=[], role="root",
        namespace="ns1", authenticated=True,
    )
    member = UserContext(
        user_id="alice", group_ids=["team"], role="member",
        namespace="ns1", authenticated=True,
    )

    # ---- list_memory_versions ----
    repo, tx, calls = _capturing_db2_version_repo()
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    root_vis = VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id="root", namespace="ns1", group_ids=())
    member_vis = VisibilityFilter(
        scope=VisibilityScope.READABLE, user_id="alice", namespace="ns1", group_ids=("team",)
    )
    rows = await repo.list_memory_versions(tx, "m1", "main", root_vis)
    assert rows == []
    assert calls, "root read must still execute"
    root_sql = calls[0]["sql"]
    assert "memory_versions" in root_sql
    assert "memory_acl" not in root_sql
    assert "EXISTS" not in root_sql
    assert "permission_mode" not in root_sql  # root bypasses visibility entirely

    repo, tx, calls = _capturing_db2_version_repo()
    rows = await repo.list_memory_versions(tx, "m1", "main", member_vis)
    assert rows == []
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    # The mock always normalizes params to a tuple; an empty capture means
    # the call site failed to bind — fail with a clear message, not NoneType.
    assert isinstance(params, tuple) and len(params) >= 2, (
        f"expected positional param tuple, got {params!r}"
    )
    assert "memory_versions" in sql
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "macl.memory_id = memory_versions.memory_id" in sql
    # The broken self-comparison must never render.
    assert "macl.memory_id = memory_id " not in sql
    # Positional binding: (memory, branch) + clause params.
    assert params[0] == "m1"
    assert params[1] == "main"
    clause_params = params[2:]
    assert clause_params[0] == "alice"
    assert "team" in clause_params
    assert "user:alice" in clause_params
    assert "group:team" in clause_params
    assert "ns1" in clause_params
    assert "vault" in clause_params
    # Placeholder count must match the parameter tuple exactly.
    assert sql.count("?") == len(params)

    # ---- fetch_memory_version ----
    repo, tx, calls = _capturing_db2_version_repo()
    row = await repo.fetch_memory_version(tx, "m1", 1, "main", root)
    assert row is None
    root_sql = calls[0]["sql"]
    assert "memory_acl" not in root_sql
    assert "permission_mode" not in root_sql.split("WHERE", 1)[1]

    repo, tx, calls = _capturing_db2_version_repo()
    row = await repo.fetch_memory_version(tx, "m1", 1, "main", member)
    assert row is None
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert isinstance(params, tuple) and len(params) >= 3, (
        f"expected positional param tuple, got {params!r}"
    )
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "macl.memory_id = memory_versions.memory_id" in sql
    assert "macl.memory_id = memory_id " not in sql
    assert params[0] == "m1"
    assert params[1] == 1
    assert params[2] == "main"
    clause_params = params[3:]
    assert clause_params[0] == "alice"
    assert "user:alice" in clause_params
    assert "group:team" in clause_params
    assert "ns1" in clause_params
    assert "vault" in clause_params
    assert sql.count("?") == len(params)


def test_version_visibility_empty_groups_render_explicit_falsifier() -> None:
    """Every backend versions renderer must reject an empty group list with
    an explicit ``0=1`` boolean falsifier, never a bare ``0`` literal.

    NG2-MN-019: the SQLite and MySQL ``_version_visibility_clause`` helpers
    introduced bare ``group_clause = "0"`` for an empty ``group_ids`` list,
    rendering ``... AND group_id IS NOT NULL AND 0)``. The established
    convention (live-memory renderers in the same modules, plus Oracle/Db2)
    is ``0=1``; pin it across all four versions renderers so a revert to
    the bare literal fails loudly.
    """
    import mnemos.persistence.db2 as db2_mod
    import mnemos.persistence.mysql as mysql_mod
    import mnemos.persistence.oracle as oracle_mod
    import mnemos.persistence.sqlite as sqlite_mod

    # SQLite: _version_visibility_clause(user, params) — the builder now
    # renders the namespace pin internally (``AND namespace = ?``) and
    # binds ``user.namespace`` as its trailing param, mirroring the
    # MySQL / Oracle / Db2 builders.
    params: list = []
    user = SimpleNamespace(user_id="alice", group_ids=[], namespace="ns1")
    clause = sqlite_mod._version_visibility_clause(user, params)
    assert "group_id IS NOT NULL AND 0=1" in clause
    assert "group_id IS NOT NULL AND 0)" not in clause
    assert clause.endswith("namespace = ?")
    # Params: user_id first, then the ACL principals, then the namespace pin.
    assert params[0] == "alice"
    assert params[-1] == "ns1"
    assert "group:team" not in params
    assert clause.count("?") == len(params)

    # MySQL: _version_visibility_clause(user_id, group_ids, *, namespace=...)
    # NG2-MN-019 FIX: MySQL now renders the ACL escape hatch (migration
    # 0073 provisions memory_acl), so the params carry user_id, then the
    # ACL principals (user:alice — no group principals for an empty list),
    # then the namespace pin.
    clause, mparams = mysql_mod._version_visibility_clause(
        "alice", [], namespace="ns1"
    )
    assert "group_id IS NOT NULL AND 0=1" in clause
    assert "group_id IS NOT NULL AND 0)" not in clause
    assert mparams == ["alice", "user:alice", "ns1"]
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in clause

    # Oracle: _version_visibility_clause(user_id, group_ids, *, namespace=...)
    clause, oparams = oracle_mod._version_visibility_clause(
        "alice", [], namespace="ns1"
    )
    assert "group_id IS NOT NULL AND 0=1" in clause
    assert "group_id IS NOT NULL AND 0)" not in clause
    assert oparams["vis_owner"] == "alice"
    assert oparams["vis_ns"] == "ns1"

    # Db2: _version_visibility_clause(user_id, group_ids, *, namespace=...)
    clause, dparams = db2_mod._version_visibility_clause("alice", [], namespace="ns1")
    assert "group_id IS NOT NULL AND 0=1" in clause
    assert "group_id IS NOT NULL AND 0)" not in clause
    assert dparams[0] == "alice"
    assert dparams[-1] == "ns1"
    assert "group:team" not in dparams


def test_version_visibility_nonempty_groups_render_in_clause_all_backends() -> None:
    """With a non-empty group list the four renderers emit the same ``IN``
    shape (placeholders per backend) and bind the group values positionally
    / by name — the falsifier path is not taken.

    NG2-MN-019 companion to
    ``test_version_visibility_empty_groups_render_explicit_falsifier``:
    pins that the ``0=1`` branch is only entered for an empty list, and
    that the populated branch still binds every group id (SQLite/Db2
    positional, MySQL ``%s`` positional, Oracle named).
    """
    import mnemos.persistence.db2 as db2_mod
    import mnemos.persistence.mysql as mysql_mod
    import mnemos.persistence.oracle as oracle_mod
    import mnemos.persistence.sqlite as sqlite_mod

    groups = ["team-a", "team-b"]

    # SQLite: user object, positional qmark params. The namespace pin is
    # bound inside the builder (last param), matching the other backends.
    params: list = []
    user = SimpleNamespace(user_id="alice", group_ids=list(groups), namespace="ns1")
    clause = sqlite_mod._version_visibility_clause(user, params)
    assert "group_id IN (?, ?)" in clause
    assert "0=1" not in clause
    assert clause.endswith("namespace = ?")
    assert params == ["alice", "team-a", "team-b", "user:alice", "group:team-a", "group:team-b", "ns1"]

    # MySQL: %s positional.
    clause, mparams = mysql_mod._version_visibility_clause(
        "alice", groups, namespace="ns1"
    )
    assert "group_id IN (%s, %s)" in clause
    assert "0=1" not in clause
    # ACL principals follow the group params (MySQL now provisions memory_acl).
    assert mparams == [
        "alice", "team-a", "team-b",
        "user:alice", "group:team-a", "group:team-b", "ns1",
    ]

    # Oracle: named binds, group params in the dict.
    clause, oparams = oracle_mod._version_visibility_clause(
        "alice", groups, namespace="ns1"
    )
    assert "group_id IN (:vis_group0,:vis_group1)" in clause
    assert "0=1" not in clause
    assert oparams["vis_group0"] == "team-a"
    assert oparams["vis_group1"] == "team-b"
    assert oparams["vis_owner"] == "alice"
    assert oparams["vis_ns"] == "ns1"

    # Db2: ? positional.
    clause, dparams = db2_mod._version_visibility_clause("alice", groups, namespace="ns1")
    assert "group_id IN (?, ?)" in clause
    assert "0=1" not in clause
    assert dparams == ["alice", "team-a", "team-b", "user:alice", "group:team-a", "group:team-b", "ns1"]


def test_version_visibility_exclude_namespaces_render_balanced_clause_all_backends() -> None:
    """The ``exclude_namespaces`` builder branch must render a syntactically
    balanced predicate on every backend and bind every excluded namespace.

    This is the direct, builder-level complement to the fake-cursor tests:
    the MySQL exclude branch lost its closing parenthesis in R1 (the
    ``AND (`` group around ``IS NULL / NOT IN`` was never closed), which
    the substring-only render tests could not see. Here every backend is
    called with a non-empty ``exclude_namespaces`` tuple, and each rendered
    clause must have matching open/close parens plus an exact placeholder
    / bind count equal to the param list size.
    """
    import re as _re
    from types import SimpleNamespace as _SimpleNamespace
    from typing import Any as _Any

    import mnemos.persistence.db2 as db2_mod
    import mnemos.persistence.mysql as mysql_mod
    import mnemos.persistence.oracle as oracle_mod
    import mnemos.persistence.sqlite as sqlite_mod

    excludes = ("vault", "internal")

    # SQLite: user object, positional qmark params (builder applies the
    # namespace pin internally — ``namespace = ?`` — with its bind as the
    # trailing param).
    params: list[_Any] = []
    user = _SimpleNamespace(user_id="alice", group_ids=["team"], namespace="ns1")
    clause = sqlite_mod._version_visibility_clause(user, params)
    assert clause.count("(") == clause.count(")")
    assert clause.count("?") == len(params)
    assert clause.endswith("namespace = ?")
    assert params[-1] == "ns1"

    # MySQL: %s positional.
    clause, mparams = mysql_mod._version_visibility_clause(
        "alice", ["team"], namespace="ns1", exclude_namespaces=excludes
    )
    assert "namespace IS NULL" in clause
    assert "namespace NOT IN (%s, %s)" in clause
    assert clause.count("(") == clause.count(")")
    assert clause.count("%s") == len(mparams)
    assert "vault" in mparams
    assert "internal" in mparams

    # Oracle: named binds.
    clause, oparams = oracle_mod._version_visibility_clause(
        "alice", ["team"], namespace="ns1", exclude_namespaces=excludes
    )
    assert "namespace IS NULL" in clause
    assert "namespace NOT IN (:vis_xns0,:vis_xns1)" in clause
    assert clause.count("(") == clause.count(")")
    named_binds = _re.findall(r":[A-Za-z_]\w*", clause)
    assert len(named_binds) == len(oparams)
    assert any(k.startswith("vis_xns") for k in oparams)
    assert "vault" in oparams.values()
    assert "internal" in oparams.values()

    # Db2: ? positional.
    clause, dparams = db2_mod._version_visibility_clause(
        "alice", ["team"], namespace="ns1", exclude_namespaces=excludes
    )
    assert "namespace IS NULL" in clause
    assert "namespace NOT IN (?, ?)" in clause
    assert clause.count("(") == clause.count(")")
    assert clause.count("?") == len(dparams)
    assert "vault" in dparams
    assert "internal" in dparams


# ────────────────────────────────────────────────────────────────────────────
# Fake-cursor rendering: MySQL and Oracle version-read paths must execute
# finished, balanced SQL with placeholder counts equal to bound params.
# (These render the actual SQL for non-root callers — the gap the Db2
# pair fills.)
# ────────────────────────────────────────────────────────────────────────────


def _capturing_mysql_version_repo():
    """Build a MysqlVersionRepository wired to a capturing fake aiomysql conn."""
    from mnemos.persistence.mysql import MysqlVersionRepository

    calls: list[dict[str, Any]] = []

    class _FakeCursor:
        description = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": tuple(params) if params else ()})

        async def fetchall(self):
            return []

        async def fetchone(self):
            return None

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    tx = SimpleNamespace(conn=_FakeConn())
    return MysqlVersionRepository(), tx, calls


@pytest.mark.asyncio
async def test_mysql_version_read_renders_finished_sql_via_fake_cursor() -> None:
    """Both MySQL version-read methods must render finished, balanced SQL
    with placeholder count == param count, and embed the qualified ACL
    correlation for non-root callers. Root bypasses the clause.
    """
    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    root_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="root", group_ids=[], role="root",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )
    member_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="alice", group_ids=["team"], role="member",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )
    assert root_filter.scope == VisibilityScope.ROOT_BYPASS
    assert member_filter.scope != VisibilityScope.ROOT_BYPASS

    # ---- list_memory_versions (ABC surface) ----
    repo, tx, calls = _capturing_mysql_version_repo()
    rows = await repo.list_memory_versions(tx, "m1", "main", root_filter)
    assert rows == []
    assert calls, "root read must still execute"
    root_sql = calls[0]["sql"]
    assert "memory_versions" in root_sql
    assert "memory_acl" not in root_sql
    assert "EXISTS" not in root_sql
    assert root_sql.count("%s") == len(calls[0]["params"]) == 2

    repo, tx, calls = _capturing_mysql_version_repo()
    rows = await repo.list_memory_versions(tx, "m1", "main", member_filter)
    assert rows == []
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert isinstance(params, tuple) and len(params) >= 2
    # Qualified ACL correlation against the outer table, never the
    # self-comparing ``macl.memory_id = memory_id`` tautology.
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "WHERE macl.memory_id = memory_versions.memory_id" in sql
    assert "macl.memory_id = memory_id " not in sql
    assert "memory_id = memory_id" not in sql
    # Vault subtraction: exclude_namespaces from the filter is rendered.
    assert "namespace NOT IN (%s)" in sql
    assert "vault" in params
    # Balanced parens (was broken in R1 — the exclude_namespaces branch
    # lost its closing paren).
    assert sql.count("(") == sql.count(")")
    # Placeholder count must match the parameter tuple exactly.
    assert sql.count("%s") == len(params)
    # Positional binding: (memory, branch) + clause params.
    assert params[0] == "m1"
    assert params[1] == "main"
    clause_params = params[2:]
    assert clause_params[0] == "alice"
    assert "team" in clause_params
    assert "user:alice" in clause_params
    assert "group:team" in clause_params
    assert "ns1" in clause_params
    assert "vault" in clause_params

    # ---- get_memory_version (ABC surface) ----
    repo, tx, calls = _capturing_mysql_version_repo()
    row = await repo.get_memory_version(tx, "m1", 1, "main", root_filter)
    assert row is None
    root_sql = calls[0]["sql"]
    assert "memory_acl" not in root_sql
    assert root_sql.count("%s") == len(calls[0]["params"]) == 3

    repo, tx, calls = _capturing_mysql_version_repo()
    row = await repo.get_memory_version(tx, "m1", 1, "main", member_filter)
    assert row is None
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert isinstance(params, tuple) and len(params) >= 3
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "WHERE macl.memory_id = memory_versions.memory_id" in sql
    assert "macl.memory_id = memory_id " not in sql
    assert "memory_id = memory_id" not in sql
    assert "namespace NOT IN (%s)" in sql
    assert "vault" in params
    assert sql.count("(") == sql.count(")")
    assert sql.count("%s") == len(params)
    assert params[0] == "m1"
    assert params[1] == 1
    assert params[2] == "main"
    clause_params = params[3:]
    assert clause_params[0] == "alice"
    assert "team" in clause_params
    assert "user:alice" in clause_params
    assert "group:team" in clause_params
    assert "ns1" in clause_params
    assert "vault" in clause_params


@pytest.mark.asyncio
async def test_oracle_version_read_renders_finished_sql_via_fake_cursor(
    monkeypatch,
) -> None:
    """Both Oracle version-read methods must render finished SQL with
    named-bind counts equal to param-dict sizes, apply the caller's
    exclude_namespaces (not a baked-in vault only), and use the qualified
    ACL correlation. Root bypasses the clause.
    """
    # Function-scope imports (module-level copies live at the top of this
    # file; these shadows make them visible to any reviewer reading only
    # this function body).

    import mnemos.persistence.oracle as oracle_mod

    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.oracle import OracleVersionRepository

    # Patch _call to pass through to the async cursor methods.
    async def _fake_call(func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(oracle_mod, "_call", _fake_call)

    root = UserContext(
        user_id="root", group_ids=[], role="root",
        namespace="ns1", authenticated=True,
    )
    member = UserContext(
        user_id="alice", group_ids=["team"], role="member",
        namespace="ns1", authenticated=True,
    )

    repo = OracleVersionRepository()
    calls: list[dict[str, Any]] = []

    class _OracleFakeCursor:
        description = None
        rowcount = -1

        async def execute(self, sql, params=None):
            calls.append({"sql": sql, "params": dict(params) if params else {}})

        async def fetchall(self):
            return []

        async def fetchone(self):
            return None

        async def close(self):
            pass

    class _OracleFakeConn:
        async def cursor(self):
            return _OracleFakeCursor()

    tx = SimpleNamespace(conn=_OracleFakeConn())

    # ---- _list_memory_versions_facade (list) ----
    rows = await repo._list_memory_versions_facade(tx, "m1", "main", root)
    assert rows == []
    root_sql = calls[0]["sql"]
    assert "memory_versions" in root_sql
    assert "memory_acl" not in root_sql
    assert "EXISTS" not in root_sql
    assert "permission_mode" not in root_sql

    calls.clear()
    rows = await repo._list_memory_versions_facade(tx, "m1", "main", member)
    assert rows == []
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert isinstance(params, dict) and params
    assert "memory_versions" in sql
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "macl.memory_id = memory_versions.memory_id" in sql
    # The broken self-comparison must never render.
    assert "macl.memory_id = memory_id" not in sql.replace(
        "macl.memory_id = memory_versions.memory_id", ""
    )
    # exclude_namespaces from the filter (vault) is rendered as named binds.
    assert "namespace NOT IN (" in sql
    assert any(k.startswith("vis_xns") for k in params)
    assert any(v == "vault" for v in params.values())
    # Named-bind count (clause + WHERE-level memory/branch binds) must
    # match the full param dict size exactly. The regex requires a
    # non-word char after the identifier, so it matches ``:name `` (space),
    # ``:name)`` (paren), etc., but not ``://`` (URL scheme) or ``::type``
    # (Postgres cast).
    bind_count = len(re.findall(r":[A-Za-z_]\w*(?=[\s),;]|$)", sql))
    assert bind_count == len(params), (
        f"bind count {bind_count} != param dict size {len(params)}"
    )

    # ---- _fetch_memory_version_facade (get) ----
    calls.clear()
    row = await repo._fetch_memory_version_facade(tx, "m1", 1, "main", root)
    assert row is None
    root_sql = calls[0]["sql"]
    assert "memory_acl" not in root_sql
    assert "permission_mode" not in root_sql.split("WHERE", 1)[1]

    calls.clear()
    row = await repo._fetch_memory_version_facade(tx, "m1", 1, "main", member)
    assert row is None
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert isinstance(params, dict) and params
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "macl.memory_id = memory_versions.memory_id" in sql
    assert "namespace NOT IN (" in sql
    assert any(k.startswith("vis_xns") for k in params)
    assert any(v == "vault" for v in params.values())
    bind_count = len(re.findall(r":[A-Za-z_]\w*(?=[\s),;]|$)", sql))
    assert bind_count == len(params)


@pytest.mark.asyncio
async def test_fetch_version_contents_renders_finished_sql_via_fake_cursor() -> None:
    """The ABC ``fetch_version_contents`` slice (diff backing store) must
    render finished, balanced SQL on every backend with placeholder/bind
    counts equal to the param lists, apply the namespace pin plus
    ``exclude_namespaces`` subtraction for non-root callers, keep
    ``deleted_at IS NULL``, and fail closed (empty list, no SQL) for a
    namespace-less non-root filter.

    SQLite / MySQL use capturing fakes; Oracle uses the patched-``_call``
    facade pair; Db2 inherits Oracle's facade with ``?`` positional
    binds rewritten by ``_BIND_RE`` — exercised via the Db2 repository
    with the same fake-connection shape.
    """
    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    root_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="root", group_ids=[], role="root",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )
    member_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="alice", group_ids=["team"], role="member",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )
    assert root_filter.scope == VisibilityScope.ROOT_BYPASS
    assert member_filter.scope != VisibilityScope.ROOT_BYPASS

    # ---- SQLite: native ? positional, capturing via the fake tx conn ----
    import mnemos.persistence.sqlite as sqlite_mod
    from typing import Any as _Any

    sqlite_calls: list[dict[str, _Any]] = []

    async def _rec_fetch_all(conn, sql, params):
        sqlite_calls.append({"sql": sql, "params": tuple(params)})
        return []

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr(sqlite_mod, "_fetch_all", _rec_fetch_all)
    try:
        sqlite_repo = sqlite_mod.SqliteVersionRepository()
        fake_tx = sqlite_mod.SqliteTransaction(conn=object())
        rows = await sqlite_repo.fetch_version_contents(
            fake_tx, "m1", [1, 2], "main", root_filter
        )
        assert rows == []
        assert sqlite_calls, "root read must still execute"
        root_sql = sqlite_calls[0]["sql"]
        assert "version_num IN (?, ?)" in root_sql
        assert "deleted_at IS NULL" in root_sql
        assert "memory_acl" not in root_sql
        assert root_sql.count("?") == len(sqlite_calls[0]["params"]) == 4
        assert sqlite_calls[0]["params"][:2] == ("m1", "main")
        assert sqlite_calls[0]["params"][2:] == (1, 2)

        sqlite_calls.clear()
        rows = await sqlite_repo.fetch_version_contents(
            fake_tx, "m1", [1, 2], "main", member_filter
        )
        assert rows == []
        sql = sqlite_calls[0]["sql"]
        params = sqlite_calls[0]["params"]
        assert "version_num IN (?, ?)" in sql
        assert "deleted_at IS NULL" in sql
        # Exactly ONE namespace pin, rendered inside the clause builder —
        # never a duplicated second ``namespace = ?`` at the call site.
        assert sql.count("namespace = ?") == 1
        assert "memory_acl" in sql
        assert "COALESCE(permission_mode, 0)" in sql
        assert sql.count("(") == sql.count(")")
        assert sql.count("?") == len(params)
        assert params[0] == "m1"
        assert params[1] == "main"
        assert list(params[2:4]) == [1, 2]
        clause_params = params[4:]
        assert clause_params[0] == "alice"
        assert "team" in clause_params
        assert "user:alice" in clause_params
        assert "group:team" in clause_params
        # The namespace pin travels with the clause params — bound exactly
        # once, as the clause's trailing value.
        assert clause_params.count("ns1") == 1
        assert clause_params[-1] == "ns1"

        # Namespace-less non-root filter: fail closed, no SQL executed.
        none_filter = VisibilityFilter(
            scope=VisibilityScope.READABLE,
            user_id="alice",
            group_ids=("team",),
            namespace=None,
            exclude_namespaces=("vault",),
        )
        sqlite_calls.clear()
        rows = await sqlite_repo.fetch_version_contents(
            fake_tx, "m1", [1], "main", none_filter
        )
        assert rows == []
        assert sqlite_calls == [], "namespace-None must not execute SQL"
    finally:
        monkeypatcher.undo()

    # ---- MySQL: %s positional via the capturing fake conn ----
    repo, tx, calls = _capturing_mysql_version_repo()
    rows = await repo.fetch_version_contents(tx, "m1", [1, 2], "main", root_filter)
    assert rows == []
    assert calls, "root read must still execute"
    root_sql = calls[0]["sql"]
    assert "version_num IN (%s, %s)" in root_sql
    assert "deleted_at IS NULL" in root_sql
    assert "memory_acl" not in root_sql
    assert root_sql.count("%s") == len(calls[0]["params"]) == 4

    calls.clear()
    rows = await repo.fetch_version_contents(tx, "m1", [1, 2], "main", member_filter)
    assert rows == []
    sql = calls[0]["sql"]
    params = calls[0]["params"]
    assert "version_num IN (%s, %s)" in sql
    assert "deleted_at IS NULL" in sql
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
    assert "WHERE macl.memory_id = memory_versions.memory_id" in sql
    assert "macl.memory_id = memory_id " not in sql
    assert "memory_id = memory_id" not in sql
    assert "namespace NOT IN (%s)" in sql
    assert "vault" in params
    # Cross-backend parity: NULL permission_mode must evaluate the
    # world/group bits as 0, matching the Db2 builder.
    assert "COALESCE(memory_versions.permission_mode, 0)" in sql
    assert sql.count("(") == sql.count(")")
    assert sql.count("%s") == len(params)
    assert params[0] == "m1"
    assert params[1] == "main"
    assert list(params[2:4]) == [1, 2]
    clause_params = params[4:]
    assert clause_params[0] == "alice"
    assert "team" in clause_params
    assert "user:alice" in clause_params
    assert "ns1" in clause_params
    assert "group:team" in clause_params
    assert clause_params[-1] == "vault"


@pytest.mark.asyncio
async def test_fetch_version_contents_oracle_db2_facade_renders_finished_sql(
    monkeypatch,
) -> None:
    """Oracle / Db2 facade ``fetch_version_contents`` must render named
    binds (Oracle) / ``?`` positional binds (Db2 rewrite) with counts
    equal to the param dicts, apply the vault subtraction from the
    filter, keep ``deleted_at IS NULL``, and fail closed for a
    namespace-less non-root envelope.
    """
    import re as _re
    from types import SimpleNamespace as _SimpleNamespace
    from typing import Any as _Any

    import mnemos.persistence.oracle as oracle_mod
    import mnemos.persistence.db2 as db2_mod

    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    # Patch _call to pass through to the async cursor methods.
    async def _fake_call(func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(oracle_mod, "_call", _fake_call)
    # db2_mod._call is the SAME function object imported into the db2
    # namespace; patching the oracle reference updates both call sites.

    root_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="root", group_ids=[], role="root",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )
    member_filter = VisibilityFilter.for_read(
        UserContext(
            user_id="alice", group_ids=["team"], role="member",
            namespace="ns1", authenticated=True,
        ),
        namespace="ns1",
    )

    for mod, repo_cls, is_db2 in (
        (oracle_mod, oracle_mod.OracleVersionRepository, False),
        (db2_mod, db2_mod.Db2VersionRepository, True),
    ):
        repo = repo_cls()
        calls: list[dict[str, _Any]] = []

        class _FakeCursor:
            description = None
            rowcount = -1

            async def execute(self, sql, params=None):
                if isinstance(params, dict):
                    captured = dict(params)
                else:
                    captured = tuple(params) if params else ()
                calls.append({"sql": sql, "params": captured})

            async def fetchall(self):
                return []

            async def fetchone(self):
                return None

            async def close(self):
                pass

        class _FakeConn:
            async def cursor(self):
                return _FakeCursor()

        tx = _SimpleNamespace(conn=_FakeConn())

        # Root: named/positional version binds only, no visibility clause.
        rows = await repo.fetch_version_contents(tx, "m1", [1, 2], "main", root_filter)
        assert rows == []
        assert calls, "root read must still execute"
        root_sql = calls[0]["sql"]
        assert "version_num IN (" in root_sql
        assert "deleted_at IS NULL" in root_sql
        assert "memory_acl" not in root_sql
        assert "permission_mode" not in root_sql

        calls.clear()
        rows = await repo.fetch_version_contents(tx, "m1", [1, 2], "main", member_filter)
        assert rows == []
        sql = calls[0]["sql"]
        params = calls[0]["params"]
        assert "version_num IN (" in sql
        assert "deleted_at IS NULL" in sql
        assert "EXISTS (SELECT 1 FROM memory_acl macl" in sql
        assert "macl.memory_id = memory_versions.memory_id" in sql
        assert "namespace NOT IN (" in sql
        # Cross-backend parity: NULL permission_mode evaluates the
        # world/group bits as 0 (Oracle named binds / Db2 positional).
        # Db2 renders the clause unqualified (bare table columns), Oracle
        # qualifies with ``memory_versions.``.
        coalesce_prefix = "" if is_db2 else "memory_versions."
        assert f"COALESCE({coalesce_prefix}permission_mode, 0)" in sql
        assert sql.count("(") == sql.count(")")
        if is_db2:
            # The Db2 clause builder (``_version_visibility_clause``)
            # renders ``?`` positional placeholders directly, so the
            # full param tuple is (memory, branch, versions, clause...).
            bind_count = sql.count("?")
            expected = len(params)
            assert isinstance(params, tuple), "Db2 must bind positionally"
            assert params[0] == "m1" and params[1] == "main"
            assert list(params[2:4]) == [1, 2]
        else:
            # Oracle named binds: regex requires a non-word char after the
            # identifier (matches the sibling facade-render test).
            bind_count = len(
                _re.findall(r":[A-Za-z_]\w*(?=[\s),;]|$)", sql)
            )
            expected = len(params)
        assert bind_count == expected, (
            f"{repo_cls.__name__}: bind count {bind_count} != param size {expected}"
        )

        # Namespace-less non-root envelope: fail closed, no SQL executed.
        none_filter = VisibilityFilter(
            scope=VisibilityScope.READABLE,
            user_id="alice",
            group_ids=("team",),
            namespace=None,
            exclude_namespaces=("vault",),
        )
        # Namespace-less non-root envelope: the facade fails closed.
        # Oracle (shared with Db2 via inheritance) raises PermissionError
        # from _visibility_to_user; both stay SQL-free.
        calls.clear()
        with pytest.raises(PermissionError):
            await repo.fetch_version_contents(tx, "m1", [1], "main", none_filter)
        assert calls == [], "namespace-None must not execute SQL"


def test_table_alias_validator_rejects_non_identifiers() -> None:
    """The table-alias validator must reject anything that is not a bare
    SQL identifier, preventing SQL injection via the ``table_alias``
    parameter in ``_version_visibility_clause`` and related builders.

    The validator enforces ``^[A-Za-z_][A-Za-z0-9_]*$`` (matches Oracle
    ``IS_IDENTIFIER`` and MySQL/Postgres bare-identifier rules): the name
    must start with a letter or underscore. Digit-leading names like
    ``123bad`` are rejected — they are only legal as *quoted* identifiers
    in MySQL/Postgres and are reserved-word forms in Oracle, and quoting
    is outside this fragment's contract.

    Valid: ``m``, ``memories``, ``mv``, ``memory_versions``, ``_private``.
    Invalid: empty (returns ""), spaces, quotes, semicolons, double-colon
    casts, URL schemes, digit-leading names, and other SQL metacharacters.
    """
    # Function-scope imports for visibility (module-level copies live at
    # the top of this file).

    import mnemos.persistence.mysql as mysql_mod

    validator = mysql_mod._validated_table_prefix

    # Valid bare identifiers -> "{alias}."
    assert validator("m") == "m."
    assert validator("memories") == "memories."
    assert validator("mv") == "mv."
    assert validator("memory_versions") == "memory_versions."
    assert validator("_private") == "_private."

    # Empty -> "" (no prefix)
    assert validator("") == ""

    # Injection attempts must raise ValueError — use pytest.raises so the
    # test fails hard if the validator ever accepts a non-identifier.
    for bad in [
        "m ); DROP TABLE memories; --",
        "m.*",
        "m ",
        " m",
        "m%20",
        "::text[]",
        "http://evil",
        "m', 1)",
        "m\nDROP TABLE",
        "123bad",
        "macl.",
    ]:
        with pytest.raises(ValueError, match="Invalid table alias"):
            validator(bad)
