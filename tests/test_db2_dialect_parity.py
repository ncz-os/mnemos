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
