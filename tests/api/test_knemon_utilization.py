from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.knemon_utilization import router as utilization_router


class _SqliteBackend:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE subscription_plans (
              provider TEXT NOT NULL,
              plan_name TEXT NOT NULL,
              auth_method TEXT NOT NULL,
              monthly_usd NUMERIC,
              msg_cap NUMERIC,
              msg_window_seconds NUMERIC,
              token_cap NUMERIC,
              token_window_seconds NUMERIC,
              reset_anchor TEXT,
              overage_pricing_per_mtok_in NUMERIC,
              overage_pricing_per_mtok_out NUMERIC,
              notes TEXT,
              PRIMARY KEY (provider, plan_name)
            );
            CREATE TABLE usage_ledger (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TIMESTAMP NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              task_kind TEXT NOT NULL,
              tokens_in INTEGER NOT NULL,
              tokens_out INTEGER NOT NULL,
              tokens_reasoning INTEGER NOT NULL,
              est_cost_usd NUMERIC NOT NULL,
              latency_ms INTEGER NOT NULL,
              outcome TEXT NOT NULL,
              caller_subsystem TEXT NOT NULL,
              tier TEXT NOT NULL,
              session_id TEXT,
              request_count NUMERIC NOT NULL DEFAULT 1,
              plan_window_id TEXT,
              subscription_amortized INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('openai', 'chatgpt_plus', 'subscription', 20, 40, 10800,
                    NULL, NULL, 'rolling', NULL, NULL, 'test plus')
            """
        )
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('nvidia', 'ngc_inference', 'free', 0, NULL, NULL,
                    NULL, NULL, 'monthly', 0, 0, 'test free')
            """
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-5', 'chat', 100, 40, 0, 0, 250,
                      'ok', 'test', 'chatgpt_plus', 's1', 2, NULL, 1)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, subscription_amortized
            ) VALUES (?, 'nvidia', 'free-model', 'chat', 100, 40, 0, 0, 100,
                      'ok', 'test', 'ngc_inference', 's2', 1, NULL, 0)
            """,
            (now,),
        )
        self.conn.commit()

    @asynccontextmanager
    async def transactional(self):
        yield self.conn


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.mark.asyncio
async def test_knemon_utilization_routes_with_in_memory_sqlite(monkeypatch):
    monkeypatch.setattr(lifecycle, "_persistence_backend", _SqliteBackend())

    app = FastAPI()
    app.include_router(utilization_router)
    app.dependency_overrides[get_current_user] = _root

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        utilization = await client.get("/v1/knemon/utilization")
        projection = await client.get("/v1/knemon/overage_projection?days_ahead=7")
        by_session = await client.get("/v1/knemon/by_session?days=7")
        cost_split = await client.get("/v1/knemon/cost_split?period=monthly")

    assert utilization.status_code == 200
    assert projection.status_code == 200
    assert by_session.status_code == 200
    assert cost_split.status_code == 200

    util_rows = utilization.json()
    plus = next(row for row in util_rows if row["provider"] == "openai")
    assert plus["requests_used"] == 2
    assert plus["msg_cap"] == 40
    assert plus["utilization_pct"] == 5.0

    sessions = by_session.json()
    assert {row["session_id"] for row in sessions} == {"s1", "s2"}

    buckets = {row["cost_bucket"]: row for row in cost_split.json()}
    assert buckets["subscription_amortized"]["requests"] == 2
    assert buckets["free"]["requests"] == 1
