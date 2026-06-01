from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.ledger import router as ledger_router
from mnemos.persistence.base import UsageLedgerRecord, UsageLedgerResult
from mnemos.persistence.postgres import PostgresBackend, PostgresTransaction


class _RawTx:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _RecorderConn:
    def __init__(self) -> None:
        self.sql: str | None = None
        self.args: tuple | None = None
        self.row = {
            "id": 42,
            "est_cost_usd": Decimal("0.000324"),
            "registry_match": True,
            "auth_method": "api",
        }

    async def fetchrow(self, sql: str, *args):
        self.sql = sql
        self.args = args
        return self.row


class _OracleVar:
    def __init__(self) -> None:
        self.value = None

    def getvalue(self):
        return self.value

    def setvalue(self, value):
        self.value = value


class _OracleLedgerCursor:
    def __init__(self) -> None:
        self.sql_calls: list[str] = []

    def var(self, _kind):
        return _OracleVar()

    async def execute(self, sql: str, params: dict):
        self.sql_calls.append(sql)
        if "FROM subscription_plans" in sql:
            raise Exception("ORA-00942: table or view does not exist")
        if "FROM model_registry" in sql:
            raise Exception("ORA-00942: table or view does not exist")
        params["rid"].setvalue(77)
        params["rcost"].setvalue(Decimal("0"))

    async def close(self) -> None:
        return None


class _OracleLedgerConn:
    def __init__(self) -> None:
        self.cursor_obj = _OracleLedgerCursor()

    def cursor(self):
        return self.cursor_obj


class _Db2MissingTableError(Exception):
    sqlstate = "42704"
    sqlcode = "-204"


class _Db2LedgerCursor:
    def __init__(self) -> None:
        self.sql_calls: list[str] = []
        self.params_calls: list[tuple] = []
        self.row = (88, Decimal("0"))

    async def execute(self, sql: str, params: tuple):
        self.sql_calls.append(sql)
        self.params_calls.append(params)
        if "subscription_plans" in sql or "model_registry" in sql:
            raise _Db2MissingTableError("SQL0204N model_registry is an undefined name. SQLSTATE=42704")

    async def fetchone(self):
        return self.row

    async def close(self) -> None:
        return None


class _Db2LedgerConn:
    def __init__(self) -> None:
        self.cursor_obj = _Db2LedgerCursor()

    def cursor(self):
        return self.cursor_obj


def _settings():
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=768))


@pytest.mark.asyncio
async def test_postgres_record_usage_ledger_inserts_with_registry_prices():
    conn = _RecorderConn()
    tx = PostgresTransaction(conn, _RawTx())  # type: ignore[arg-type]
    backend = PostgresBackend(pool=SimpleNamespace(), settings=_settings())  # type: ignore[arg-type]

    result = await backend.record_usage_ledger(
        tx,
        UsageLedgerRecord(
            provider="openai",
            model="gpt-4o",
            task_kind="code",
            tokens_in=1200,
            tokens_out=340,
            tokens_reasoning=10,
            latency_ms=1240,
            outcome="ok",
            caller_subsystem="pantheon",
            tier="standard",
        ),
    )

    assert result == UsageLedgerResult(id=42, est_cost_usd=Decimal("0.000324"))
    assert conn.args == (
        "openai",
        "gpt-4o",
        "code",
        1200,
        340,
        10,
        1240,
        "ok",
        "pantheon",
        "standard",
        None,
        1,
        None,
        "api",
    )
    assert conn.sql is not None
    assert "FROM model_registry" in conn.sql
    assert "INSERT INTO usage_ledger" in conn.sql
    assert "est_cost_usd" in conn.sql
    assert "$1" in conn.sql and "$14" in conn.sql
    assert "resolved_prices" in conn.sql
    assert "resolved_plan" in conn.sql


@pytest.mark.asyncio
async def test_postgres_record_usage_ledger_records_and_warns_for_unknown_model(caplog):
    conn = _RecorderConn()
    conn.row = {"id": 43, "est_cost_usd": Decimal("0"), "registry_match": False, "auth_method": "api"}
    tx = PostgresTransaction(conn, _RawTx())  # type: ignore[arg-type]
    backend = PostgresBackend(pool=SimpleNamespace(), settings=_settings())  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        result = await backend.record_usage_ledger(
            tx,
            UsageLedgerRecord(
                provider="unknown",
                model="missing",
                task_kind="code",
                tokens_in=1,
                tokens_out=1,
                tokens_reasoning=0,
                latency_ms=1,
                outcome="ok",
                caller_subsystem="pantheon",
                tier="standard",
            ),
        )

    assert result == UsageLedgerResult(id=43, est_cost_usd=Decimal("0"))
    assert "model_registry price missing" in caplog.text


@pytest.mark.asyncio
async def test_oracle_record_usage_ledger_records_when_model_registry_missing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_NUMBER=object()))

    from mnemos.persistence.oracle import OracleBackend

    conn = _OracleLedgerConn()
    backend = OracleBackend(pool=SimpleNamespace(), settings=SimpleNamespace())  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        result = await backend.record_usage_ledger(
            SimpleNamespace(conn=conn),
            UsageLedgerRecord(
                provider="unknown",
                model="missing",
                task_kind="code",
                tokens_in=1,
                tokens_out=1,
                tokens_reasoning=0,
                latency_ms=1,
                outcome="ok",
                caller_subsystem="pantheon",
                tier="standard",
            ),
        )

    assert result == UsageLedgerResult(id=77, est_cost_usd=Decimal("0"))
    assert len(conn.cursor_obj.sql_calls) == 3
    assert "FROM subscription_plans" in conn.cursor_obj.sql_calls[0]
    assert "FROM model_registry" in conn.cursor_obj.sql_calls[1]
    assert "FROM model_registry" not in conn.cursor_obj.sql_calls[2]
    assert "model_registry table missing" in caplog.text


@pytest.mark.asyncio
async def test_db2_record_usage_ledger_records_when_model_registry_missing(caplog):
    from mnemos.persistence.db2 import Db2Backend

    conn = _Db2LedgerConn()
    backend = Db2Backend(pool=SimpleNamespace(), settings=SimpleNamespace())  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        result = await backend.record_usage_ledger(
            SimpleNamespace(conn=conn),
            UsageLedgerRecord(
                provider="unknown",
                model="missing",
                task_kind="code",
                tokens_in=1,
                tokens_out=1,
                tokens_reasoning=0,
                latency_ms=1,
                outcome="ok",
                caller_subsystem="pantheon",
                tier="standard",
            ),
        )

    assert result == UsageLedgerResult(id=88, est_cost_usd=Decimal("0"))
    assert len(conn.cursor_obj.sql_calls) == 3
    assert "subscription_plans" in conn.cursor_obj.sql_calls[0]
    assert "FROM model_registry" in conn.cursor_obj.sql_calls[1]
    assert "FROM model_registry" not in conn.cursor_obj.sql_calls[2]
    assert conn.cursor_obj.params_calls[2] == (
        "unknown",
        "missing",
        "code",
        1,
        1,
        0,
        1,
        "ok",
        "pantheon",
        "standard",
        None,
        1,
        None,
        "api",
        0,
    )
    assert "model_registry table missing" in caplog.text


class _EndpointBackend:
    def __init__(self) -> None:
        self.records: list[UsageLedgerRecord] = []

    @asynccontextmanager
    async def transactional(self):
        yield object()

    async def record_usage_ledger(self, tx, record: UsageLedgerRecord) -> UsageLedgerResult:
        self.records.append(record)
        return UsageLedgerResult(id=7, est_cost_usd=Decimal("0.000324"))


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.mark.asyncio
async def test_ledger_endpoint_requires_root_and_records(monkeypatch):
    backend = _EndpointBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    app = FastAPI()
    app.include_router(ledger_router)
    app.dependency_overrides[get_current_user] = _user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_response = await client.post(
            "/v1/ledger",
            json={
                "provider": "openai",
                "model": "gpt-4o",
                "task_kind": "code",
                "tokens_in": 1200,
                "tokens_out": 340,
                "tokens_reasoning": 0,
                "latency_ms": 1240,
                "outcome": "ok",
                "caller_subsystem": "pantheon",
                "tier": "standard",
            },
        )
    assert forbidden_response.status_code == 403
    assert backend.records == []

    app.dependency_overrides[get_current_user] = _root
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/ledger",
            json={
                "provider": "openai",
                "model": "gpt-4o",
                "task_kind": "code",
                "tokens_in": 1200,
                "tokens_out": 340,
                "tokens_reasoning": 0,
                "latency_ms": 1240,
                "outcome": "ok",
                "caller_subsystem": "pantheon",
                "tier": "standard",
            },
        )
        invalid_response = await client.post(
            "/v1/ledger",
            json={
                "provider": "openai",
                "model": "gpt-4o",
                "task_kind": "code",
                "tokens_in": -1,
                "tokens_out": 340,
                "latency_ms": 1240,
                "outcome": "bad",
                "caller_subsystem": "pantheon",
                "tier": "standard",
            },
        )

    assert response.status_code == 200
    assert response.json()["id"] == 7
    assert Decimal(str(response.json()["est_cost_usd"])) == Decimal("0.000324")
    assert len(backend.records) == 1
    recorded = backend.records[0]
    assert recorded.provider == "openai"
    assert recorded.model == "gpt-4o"
    assert recorded.session_id is None
    assert recorded.request_count == 1
    assert recorded.path_kind == "api"
    assert recorded.plan_window_id is not None
    assert recorded.plan_window_id.startswith("openai-standard-")
    assert invalid_response.status_code == 422


@pytest.mark.asyncio
async def test_ledger_endpoint_infers_known_subscription_path_kind(monkeypatch):
    backend = _EndpointBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    app = FastAPI()
    app.include_router(ledger_router)
    app.dependency_overrides[get_current_user] = _root

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/ledger",
            json={
                "provider": "openai",
                "model": "gpt-5.5",
                "task_kind": "code",
                "tokens_in": 1200,
                "tokens_out": 340,
                "tokens_reasoning": 0,
                "latency_ms": 1240,
                "outcome": "ok",
                "caller_subsystem": "pantheon",
                "tier": "chatgpt_plus",
            },
        )

    assert response.status_code == 200
    assert len(backend.records) == 1
    recorded = backend.records[0]
    assert recorded.path_kind == "interactive"
    assert recorded.plan_window_id is not None
    assert recorded.plan_window_id.startswith("openai-chatgpt_plus-")


def test_usage_ledger_migrations_preserve_constraint_parity():
    root = Path(__file__).resolve().parent.parent
    pg = (root / "db/migrations/0032_usage_ledger.sql").read_text()
    oracle = (root / "db/migrations_oracle/0032_usage_ledger.sql").read_text()
    db2 = (root / "db/migrations_db2/0032_usage_ledger.sql").read_text()

    for sql in (pg, oracle, db2):
        normalized = " ".join(sql.lower().split())
        assert "tokens_reasoning" in normalized and "not null" in normalized
        assert "est_cost_usd" in normalized and "check (est_cost_usd >= 0)" in normalized
        assert "check (outcome in ('ok','err','timeout'))" in normalized
        assert "check (latency_ms >= 0)" in normalized
