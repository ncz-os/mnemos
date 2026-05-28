from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pytest

from mnemos.api.routes.ledger import compute_plan_window_id
from mnemos.domain.knemon.router import (
    KnemonRouteRequest,
    _apply_priority_ceiling,
    _best_plan,
    _fallback_bucket,
    _plan_is_effective,
    route,
)


class _SqliteKnemonBackend:
    def __init__(
        self,
        utilization_pct: int,
        *,
        burned_session: str | None = None,
        burned_request_count: int = 11,
        agent_session_id: str | None = None,
        subscription_pools: list[str] | None = None,
        extra_subscription_utilization_pct: int | None = None,
    ) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.active = 0
        self.max_active = 0
        self._create_schema()
        self._seed(
            utilization_pct,
            burned_session=burned_session,
            burned_request_count=burned_request_count,
            agent_session_id=agent_session_id,
            subscription_pools=subscription_pools,
            extra_subscription_utilization_pct=extra_subscription_utilization_pct,
        )

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE model_registry (
              provider TEXT NOT NULL,
              model_id TEXT NOT NULL,
              display_name TEXT,
              capabilities TEXT NOT NULL DEFAULT '[]',
              input_cost_per_mtok REAL NOT NULL DEFAULT 0,
              output_cost_per_mtok REAL NOT NULL DEFAULT 0,
              context_window INTEGER,
              arena_score REAL,
              graeae_weight REAL NOT NULL DEFAULT 0,
              available INTEGER NOT NULL DEFAULT 1,
              deprecated INTEGER NOT NULL DEFAULT 0
            );
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
              overage_pricing_per_mtok_out NUMERIC
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
            CREATE TABLE hive_agents (
              urn TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              host TEXT NOT NULL,
              session_id TEXT NOT NULL,
              last_heartbeat REAL NOT NULL,
              status TEXT NOT NULL,
              subscription_pools TEXT
            );
            """
        )

    def _seed(
        self,
        utilization_pct: int,
        *,
        burned_session: str | None,
        burned_request_count: int,
        agent_session_id: str | None,
        subscription_pools: list[str] | None,
        extra_subscription_utilization_pct: int | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        window_id = compute_plan_window_id(
            "openai",
            "chatgpt_plus",
            now,
            reset_anchor="rolling",
            window_seconds=10800,
        )
        used = round(40 * utilization_pct / 100)
        self.conn.executescript(
            """
            INSERT INTO model_registry VALUES
              ('openai', 'gpt-sub', 'GPT Sub', '["chat","code"]', 2, 8, 200000, 1400, 0.95, 1, 0),
              ('nvidia', 'ngc-free', 'NGC Free', '["chat","code"]', 0, 0, 200000, 1250, 0.88, 1, 0),
              ('xai', 'grok-api', 'Grok API', '["chat","code"]', 3, 15, 200000, 1220, 0.86, 1, 0);
            INSERT INTO subscription_plans VALUES
              ('openai', 'chatgpt_plus', 'subscription', 20, 40, 10800, NULL, NULL, 'rolling', NULL, NULL),
              ('nvidia', 'ngc_inference', 'free', 0, NULL, NULL, NULL, NULL, 'monthly', 0, 0),
              ('xai', 'api', 'api', NULL, NULL, NULL, NULL, NULL, 'monthly', NULL, NULL);
            """
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                      'test', 'chatgpt_plus', 'usage-fixture', ?, ?, 1)
            """,
            (now, used, window_id),
        )
        if burned_session:
            self.conn.execute(
                """
                INSERT INTO usage_ledger (
                  ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
                  est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
                  request_count, plan_window_id, subscription_amortized
                ) VALUES (?, 'openai', 'gpt-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                          'test', 'chatgpt_plus', ?, ?, ?, 1)
                """,
                (now, burned_session, burned_request_count, window_id),
            )
        if extra_subscription_utilization_pct is not None:
            extra_window_id = compute_plan_window_id(
                "anthropic",
                "claude_max_200",
                now,
                reset_anchor="rolling",
                window_seconds=10800,
            )
            extra_used = round(40 * extra_subscription_utilization_pct / 100)
            self.conn.execute(
                """
                INSERT INTO model_registry VALUES
                  ('anthropic', 'claude-sub', 'Claude Sub', '["chat","code"]', 3, 15,
                   200000, 1300, 0.87, 1, 0)
                """
            )
            self.conn.execute(
                """
                INSERT INTO subscription_plans VALUES
                  ('anthropic', 'claude_max_200', 'subscription', 200, 40, 10800,
                   NULL, NULL, 'rolling', NULL, NULL)
                """
            )
            self.conn.execute(
                """
                INSERT INTO usage_ledger (
                  ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
                  est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
                  request_count, plan_window_id, subscription_amortized
                ) VALUES (?, 'anthropic', 'claude-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                          'test', 'claude_max_200', 'usage-fixture', ?, ?, 1)
                """,
                (now, extra_used, extra_window_id),
            )
        if agent_session_id:
            self.conn.execute(
                """
                INSERT INTO hive_agents (
                  urn, kind, host, session_id, last_heartbeat, status, subscription_pools
                ) VALUES ('urn:agent:test:1', 'codex', 'pytest', ?, ?, 'online', ?)
                """,
                (agent_session_id, now.timestamp(), json.dumps(subscription_pools or [])),
            )
        self.conn.commit()

    @asynccontextmanager
    async def transactional(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        try:
            yield self.conn
        finally:
            self.active -= 1


def _req(priority: int, session_id: str | None = None) -> KnemonRouteRequest:
    return KnemonRouteRequest(
        task_kind="code-fix",
        priority=priority,
        est_tokens_in=10_000,
        est_tokens_out=2_000,
        caller_session_id=session_id,
        caller_subsystem="pytest",
        require_capability=["code"],
    )


@pytest.mark.parametrize("utilization_pct", [40, 70, 92, 100])
@pytest.mark.asyncio
async def test_synthetic_utilization_fixtures_route(utilization_pct):
    decision = await route(_req(14), _SqliteKnemonBackend(utilization_pct))
    assert decision.provider in {"openai", "xai"}


@pytest.mark.asyncio
async def test_low_priority_92_pct_subscription_routes_to_free_ngc():
    decision = await route(_req(5), _SqliteKnemonBackend(92))
    assert decision.provider == "nvidia"
    assert decision.model_id == "ngc-free"
    assert decision.auth_method == "free"


@pytest.mark.asyncio
async def test_high_priority_92_pct_subscription_still_routes_to_subscription():
    decision = await route(_req(14), _SqliteKnemonBackend(92))
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"
    assert decision.sub_window_utilization_pct == 92.5


@pytest.mark.asyncio
async def test_g1_at_100_pct_subscription_routes_to_api_fallback():
    decision = await route(_req(14), _SqliteKnemonBackend(100))
    assert decision.provider == "xai"
    assert decision.auth_method == "api"


@pytest.mark.asyncio
async def test_fallback_chain_uses_actual_utilization_for_unvisited_subscription():
    backend = _SqliteKnemonBackend(92, extra_subscription_utilization_pct=92)
    decision = await route(_req(5), backend)
    assert decision.provider == "nvidia"
    assert decision.fallback_chain[0][:2] == ("xai", "grok-api")


def test_fallback_bucket_boundaries():
    assert _fallback_bucket({"auth_method": "free"}) == 0
    assert _fallback_bucket({"auth_method": "subscription", "sub_window_utilization_pct": 69.99}) == 1
    assert _fallback_bucket({"auth_method": "subscription", "sub_window_utilization_pct": 70.0}) == 4
    assert _fallback_bucket({"auth_method": "api", "estimated_cost_usd": 0.499999}) == 2
    assert _fallback_bucket({"auth_method": "api", "estimated_cost_usd": 0.50}) == 3


def test_g1_quality_floor_survives_burn_downgrade():
    candidates = [
        {"quality": 0.80, "tier": "B", "graeae_weight": 0.99},
        {"quality": 0.86, "tier": "A", "graeae_weight": 0.80},
    ]

    assert _apply_priority_ceiling(candidates, 13, requested_priority=14) == [candidates[1]]


def test_best_plan_prefers_matching_workspace_pool_over_highest_price():
    plans = {
        "openai": [
            {
                "provider": "openai",
                "plan_name": "chatgpt_pro_200_codex",
                "parent_plan_id": "chatgpt_pro",
                "auth_method": "subscription",
                "monthly_usd": 200,
                "msg_cap": 300,
            },
            {
                "provider": "openai",
                "plan_name": "chatgpt_plus",
                "auth_method": "subscription",
                "monthly_usd": 20,
                "msg_cap": 15,
            },
        ]
    }

    assert _best_plan(plans, "openai", {"chatgpt_plus"})["plan_name"] == "chatgpt_plus"
    assert _best_plan(plans, "openai", {"chatgpt_pro"})["plan_name"] == "chatgpt_pro_200_codex"
    assert _best_plan(plans, "openai")["plan_name"] == "chatgpt_pro_200_codex"


def test_plan_effective_dates_filter_expired_and_future_rows():
    today = date(2026, 6, 1)

    assert not _plan_is_effective(
        {"effective_from": "2026-05-01", "effective_until": "2026-05-31"},
        today,
    )
    assert _plan_is_effective(
        {"effective_from": "2026-06-01", "effective_until": None},
        today,
    )
    assert not _plan_is_effective(
        {"effective_from": date(2026, 6, 2), "effective_until": None},
        today,
    )
    assert _plan_is_effective({"provider": "legacy"}, today)


@pytest.mark.asyncio
async def test_subscription_requires_caller_workspace_pool():
    session_id = "workspace-without-openai"
    backend = _SqliteKnemonBackend(40, agent_session_id=session_id, subscription_pools=["anthropic_subscription"])
    decision = await route(_req(14, session_id), backend)
    assert decision.provider == "xai"
    assert decision.auth_method == "api"
    assert "lacks pool" in decision.reasoning


@pytest.mark.asyncio
async def test_subscription_allows_matching_caller_workspace_pool():
    session_id = "workspace-with-openai"
    backend = _SqliteKnemonBackend(40, agent_session_id=session_id, subscription_pools=["chatgpt_plus"])
    decision = await route(_req(14, session_id), backend)
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"


@pytest.mark.asyncio
async def test_parallel_same_session_burn_tracking_serializes():
    backend = _SqliteKnemonBackend(92, burned_session="burned-session")
    decisions = await asyncio.gather(*(route(_req(14, "burned-session"), backend) for _ in range(10)))
    assert {decision.provider for decision in decisions} == {"xai"}
    assert backend.max_active == 1


@pytest.mark.asyncio
async def test_session_burns_on_exact_threshold():
    backend = _SqliteKnemonBackend(70, burned_session="burned-session", burned_request_count=10)
    decision = await route(_req(14, "burned-session"), backend)
    assert decision.provider == "xai"
    assert decision.auth_method == "api"


@pytest.mark.asyncio
async def test_session_below_burn_threshold_keeps_g1_subscription_escalation():
    backend = _SqliteKnemonBackend(70, burned_session="not-yet-burned", burned_request_count=9)
    decision = await route(_req(14, "not-yet-burned"), backend)
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"
