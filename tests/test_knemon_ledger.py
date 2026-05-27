from __future__ import annotations

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
        self.row = {"id": 42, "est_cost_usd": Decimal("0.000324")}

    async def fetchrow(self, sql: str, *args):
        self.sql = sql
        self.args = args
        return self.row


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
    )
    assert conn.sql is not None
    assert "FROM model_registry" in conn.sql
    assert "INSERT INTO usage_ledger" in conn.sql
    assert "est_cost_usd" in conn.sql
    assert "$1" in conn.sql and "$10" in conn.sql
    assert "resolved_prices" not in conn.sql


@pytest.mark.asyncio
async def test_postgres_record_usage_ledger_fails_closed_for_unknown_model():
    conn = _RecorderConn()
    conn.row = None
    tx = PostgresTransaction(conn, _RawTx())  # type: ignore[arg-type]
    backend = PostgresBackend(pool=SimpleNamespace(), settings=_settings())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="usage_ledger insert returned no row"):
        await backend.record_usage_ledger(
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


@pytest.mark.asyncio
async def test_ledger_endpoint_records_and_validates(monkeypatch):
    backend = _EndpointBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    app = FastAPI()
    app.include_router(ledger_router)
    app.dependency_overrides[get_current_user] = _user

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
    assert backend.records == [
        UsageLedgerRecord(
            provider="openai",
            model="gpt-4o",
            task_kind="code",
            tokens_in=1200,
            tokens_out=340,
            tokens_reasoning=0,
            latency_ms=1240,
            outcome="ok",
            caller_subsystem="pantheon",
            tier="standard",
        )
    ]
    assert invalid_response.status_code == 422


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
