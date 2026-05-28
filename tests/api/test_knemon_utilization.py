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
              path_kind TEXT NOT NULL DEFAULT 'api',
              monthly_usd NUMERIC,
              msg_cap NUMERIC,
              msg_window_seconds NUMERIC,
              token_cap NUMERIC,
              token_window_seconds NUMERIC,
              reset_anchor TEXT,
              overage_pricing_per_mtok_in NUMERIC,
              overage_pricing_per_mtok_out NUMERIC,
              notes TEXT,
              effective_from DATE NOT NULL DEFAULT '2026-01-01',
              effective_until DATE,
              parent_plan_id TEXT,
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
              path_kind TEXT NOT NULL DEFAULT 'api',
              subscription_amortized INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('openai', 'chatgpt_plus', 'subscription', 'interactive', 20, 160, 10800,
                    NULL, NULL, 'rolling', NULL, NULL, 'test plus', '2026-05-28', NULL, NULL)
            """
        )
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('openai', 'codex_pro_200_25x', 'subscription', 'interactive', 200, 375, 18000,
                    NULL, NULL, 'rolling', NULL, NULL, 'expired promo', '2026-05-01', '2026-05-27', 'codex_plus')
            """
        )
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('nvidia', 'ngc_inference', 'free', 'free', 0, NULL, NULL,
                    NULL, NULL, 'monthly', 0, 0, 'test free', '2026-01-01', NULL, NULL)
            """
        )
        self.conn.execute(
            """
            INSERT INTO subscription_plans
            VALUES ('testai', 'token_plan', 'subscription', 'api', 10, NULL, NULL,
                    1000, 3600, 'rolling', 2, 8, 'token cap', '2026-01-01', NULL, NULL)
            """
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-5', 'chat', 100, 40, 0, 0, 250,
                      'ok', 'test', 'chatgpt_plus', 's1', 2, NULL, 'api', 1)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-5', 'chat', 200, 80, 0, 0, 300,
                      'ok', 'test', 'chatgpt_plus', 's1-legacy', 3, NULL, 'api', 1)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'nvidia', 'free-model', 'chat', 100, 40, 0, 0, 100,
                      'ok', 'test', 'ngc_inference', 's2', 1, NULL, 'free', 0)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'testai', 'token-model', 'chat', 600, 150, 50, 0, 200,
                      'ok', 'test', 'token_plan', 's-token', 3, NULL, 'api', 1)
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
    assert plus["requests_used"] == 5
    assert plus["msg_cap"] == 160
    assert plus["cap_unit"] == "messages"
    assert plus["notes"] == "test plus"
    assert plus["utilization_pct"] == 3.12
    token_plan = next(row for row in util_rows if row["provider"] == "testai")
    assert token_plan["tokens_used"] == 800
    assert token_plan["token_cap"] == 1000
    assert token_plan["cap_unit"] == "tokens"
    assert token_plan["notes"] == "token cap"
    assert token_plan["utilization_pct"] == 80.0
    assert {row["plan_name"] for row in util_rows} == {"chatgpt_plus", "ngc_inference", "token_plan"}

    projection_rows = projection.json()
    projected_plus = next(row for row in projection_rows if row["provider"] == "openai")
    assert projected_plus["notes"] == "test plus"

    sessions = by_session.json()
    assert {row["session_id"] for row in sessions} == {"s1", "s1-legacy", "s2", "s-token"}

    bucket_rows = cost_split.json()
    subscription_requests = sum(
        row["requests"] for row in bucket_rows if row["cost_bucket"] == "subscription_amortized"
    )
    buckets = {row["cost_bucket"]: row for row in bucket_rows}
    assert subscription_requests == 8
    assert buckets["free"]["requests"] == 1
