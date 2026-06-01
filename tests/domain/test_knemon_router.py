from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from mnemos.core import config as config_mod
from mnemos.core.config import get_settings
from mnemos.core.plan_windows import compute_plan_window_id
from mnemos.domain.knemon.router import (
    KnemonRouteRequest,
    NoModelAvailable,
    _apply_priority_ceiling,
    _best_plan,
    _fallback_bucket,
    _plan_is_effective,
    _plans_by_provider,
    _subscription_pool_aliases,
    route,
)


class _SqliteKnemonBackend:
    def __init__(
        self,
        utilization_pct: int,
        *,
        burned_session: str | None = None,
        burned_request_count: int = 11,
        burned_age_seconds: int = 0,
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
            burned_age_seconds=burned_age_seconds,
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
              path_kind TEXT NOT NULL DEFAULT 'api',
              monthly_usd NUMERIC,
              msg_cap NUMERIC,
              msg_window_seconds NUMERIC,
              token_cap NUMERIC,
              token_window_seconds NUMERIC,
              reset_anchor TEXT,
              overage_pricing_per_mtok_in NUMERIC,
              overage_pricing_per_mtok_out NUMERIC,
              effective_from DATE NOT NULL DEFAULT '2026-01-01',
              effective_until DATE,
              parent_plan_id TEXT
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
        burned_age_seconds: int,
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
            window_seconds=18000,
        )
        msg_cap = 160
        used = round(msg_cap * utilization_pct / 100)
        self.conn.executescript(
            """
            INSERT INTO model_registry VALUES
              ('openai', 'gpt-sub', 'GPT Sub', '["chat","code"]', 2, 8, 200000, 1400, 0.95, 1, 0),
              ('openai', 'gpt-c-tier', 'GPT C Tier', '["vision"]', 2, 8, 200000, 900, 0.70, 1, 0),
              ('nvidia', 'ngc-free', 'NGC Free', '["chat","code"]', 0, 0, 200000, 1250, 0.88, 1, 0),
              ('xai', 'grok-api', 'Grok API', '["chat","code"]', 3, 15, 200000, 1220, 0.86, 1, 0);
            INSERT INTO subscription_plans VALUES
              ('openai', 'chatgpt_plus', 'subscription', 'interactive', 20, 160, 18000,
               NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, NULL),
              ('nvidia', 'ngc_inference', 'free', 'free', 0, NULL, NULL,
               NULL, NULL, 'monthly', 0, 0, '2026-01-01', NULL, NULL),
              ('xai', 'api', 'api', 'api', NULL, NULL, NULL,
               NULL, NULL, 'monthly', NULL, NULL, '2026-01-01', NULL, NULL);
            """
        )
        self.conn.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                      'test', 'chatgpt_plus', 'usage-fixture', ?, ?, 'api', 1)
            """,
            (now, used, window_id),
        )
        if burned_session:
            burn_ts = now - timedelta(seconds=burned_age_seconds)
            self.conn.execute(
                """
                INSERT INTO usage_ledger (
                  ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
                  est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
                  request_count, plan_window_id, path_kind, subscription_amortized
                ) VALUES (?, 'xai', 'grok-api', 'chat', 100, 50, 0, 0, 100, 'ok',
                          'test', 'api', ?, ?, ?, 'api', 0)
                """,
                (burn_ts, burned_session, burned_request_count, window_id),
            )
        if extra_subscription_utilization_pct is not None:
            extra_window_id = compute_plan_window_id(
                "anthropic",
                "claude_max_200",
                now,
                reset_anchor="rolling",
                window_seconds=18000,
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
                  ('anthropic', 'claude_max_200', 'subscription', 'interactive', 200, 40, 18000,
                   NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, NULL)
                """
            )
            self.conn.execute(
                """
                INSERT INTO usage_ledger (
                  ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
                  est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
                  request_count, plan_window_id, path_kind, subscription_amortized
                ) VALUES (?, 'anthropic', 'claude-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                          'test', 'claude_max_200', 'usage-fixture', ?, ?, 'api', 1)
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


@pytest.mark.asyncio
async def test_single_est_tokens_request_shape_supported():
    req = KnemonRouteRequest(
        task_kind="code-fix",
        priority=14,
        est_tokens=12_000,
        caller_subsystem="pytest",
        require_capability=["code"],
    )
    decision = await route(req, _SqliteKnemonBackend(40))
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"


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
    assert decision.sub_window_utilization_pct > 90


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
async def test_mid_priority_rejects_tier_c_ceiling():
    req = KnemonRouteRequest(
        task_kind="vision",
        priority=10,
        est_tokens_in=10_000,
        est_tokens_out=2_000,
        caller_session_id=None,
        caller_subsystem="pytest",
        require_capability=["vision"],
    )
    with pytest.raises(NoModelAvailable, match="priority tier"):
        await route(req, _SqliteKnemonBackend(40))


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
async def test_subscription_plan_selection_respects_exact_workspace_pool():
    session_id = "workspace-with-plus-only"
    backend = _SqliteKnemonBackend(40, agent_session_id=session_id, subscription_pools=["chatgpt_plus"])
    backend.conn.execute(
        """
        INSERT INTO subscription_plans VALUES
          ('openai', 'chatgpt_pro', 'subscription', 'unmetered', 200, 200, 18000,
           NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, NULL)
        """
    )
    backend.conn.commit()

    decision = await route(_req(14, session_id), backend)
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"
    assert "selected subscription under" in decision.reasoning


@pytest.mark.asyncio
async def test_openai_no_workspace_pool_uses_chatgpt_family_for_generic_gpt_model():
    backend = _SqliteKnemonBackend(40)
    backend.conn.execute(
        """
        INSERT INTO subscription_plans VALUES
          ('openai', 'codex_pro_200_20x', 'subscription', 'interactive', 200, 300, 18000,
           NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, 'codex_pro_200')
        """
    )
    backend.conn.commit()

    decision = await route(_req(14), backend)

    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"
    assert decision.sub_window_utilization_pct == 40.0


@pytest.mark.asyncio
async def test_parallel_same_session_burn_tracking_serializes():
    backend = _SqliteKnemonBackend(92, burned_session="burned-session")
    decisions = await asyncio.gather(*(route(_req(14, "burned-session"), backend) for _ in range(10)))
    assert {decision.provider for decision in decisions} == {"xai"}
    assert backend.max_active == 1


@pytest.mark.asyncio
async def test_burned_g1_session_loses_near_cap_subscription_lane():
    session_id = "burned-near-cap"
    backend = _SqliteKnemonBackend(70, burned_session=session_id, burned_request_count=10)
    decision = await route(_req(14, session_id), backend)

    assert decision.provider == "xai"
    assert decision.auth_method == "api"
    assert "skipped subscription at 70.00% utilization" in decision.reasoning


@pytest.mark.parametrize(
    ("request_count", "expected_provider"),
    [
        (9, "openai"),
        (10, "xai"),
    ],
)
@pytest.mark.asyncio
async def test_session_burn_threshold_defaults_to_ten_requests(request_count, expected_provider):
    session_id = f"burn-boundary-{request_count}"
    backend = _SqliteKnemonBackend(92, burned_session=session_id, burned_request_count=request_count)
    decision = await route(_req(14, session_id), backend)
    assert decision.provider == expected_provider


@pytest.mark.asyncio
async def test_session_burn_ignores_requests_outside_rolling_window():
    session_id = "burn-window-expired"
    backend = _SqliteKnemonBackend(
        92,
        burned_session=session_id,
        burned_request_count=10,
        burned_age_seconds=get_settings().knemon.session_burn_window_seconds + 1,
    )
    decision = await route(_req(14, session_id), backend)
    assert decision.provider == "openai"


@pytest.mark.asyncio
async def test_session_burn_threshold_is_configurable(monkeypatch):
    session_id = "burn-configurable"
    backend = _SqliteKnemonBackend(92, burned_session=session_id, burned_request_count=10)
    monkeypatch.setattr(get_settings().knemon, "session_burn_requests_per_hour", 11)
    decision = await route(_req(14, session_id), backend)
    assert decision.provider == "openai"


@pytest.mark.parametrize(
    ("item", "expected_bucket"),
    [
        ({"auth_method": "free"}, 0),
        ({"auth_method": "subscription", "sub_window_utilization_pct": 69.999}, 1),
        ({"auth_method": "api", "estimated_cost_usd": 0.499999}, 2),
        ({"auth_method": "token", "estimated_cost_usd": 0.499999}, 2),
        ({"auth_method": "api", "estimated_cost_usd": 0.50}, 3),
        ({"auth_method": "subscription", "sub_window_utilization_pct": 70.0}, 4),
        ({"auth_method": "oauth"}, 4),
    ],
)
def test_fallback_bucket_order_and_strict_boundaries(monkeypatch, item, expected_bucket):
    monkeypatch.setattr(get_settings().knemon, "low_priority_api_cost_ceiling_usd", 0.50)
    monkeypatch.setattr(get_settings().knemon, "subscription_preferred_utilization_pct", 70.0)
    assert _fallback_bucket(item) == expected_bucket


@pytest.mark.asyncio
async def test_fallback_chain_checks_subscription_utilization_for_unevaluated_candidates():
    backend = _SqliteKnemonBackend(92)
    now = datetime.now(timezone.utc)
    window_id = compute_plan_window_id(
        "anthropic",
        "claude_max_100",
        now,
        reset_anchor="rolling",
        window_seconds=18000,
    )
    backend.conn.execute(
        """
        INSERT INTO model_registry VALUES
          ('anthropic', 'claude-sub', 'Claude Sub', '["chat","code"]',
           1, 5, 200000, 1200, 0.80, 1, 0)
        """
    )
    backend.conn.execute(
        """
        INSERT INTO subscription_plans VALUES
          ('anthropic', 'claude_max_100', 'subscription', 'interactive', 100, 160, 18000,
           NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, NULL)
        """
    )
    backend.conn.execute(
        """
        INSERT INTO usage_ledger (
          ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
          est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
          request_count, plan_window_id, path_kind, subscription_amortized
        ) VALUES (?, 'anthropic', 'claude-sub', 'chat', 100, 50, 0, 0, 100, 'ok',
                  'test', 'claude_max_100', 'usage-fixture', 147, ?, 'api', 1)
        """,
        (now, window_id),
    )
    backend.conn.commit()

    decision = await route(_req(5), backend)

    assert decision.provider == "nvidia"
    assert decision.fallback_chain[0][0:2] == ("xai", "grok-api")


def test_openai_subscription_aliases_split_chatgpt_and_codex():
    chatgpt_aliases = _subscription_pool_aliases({"provider": "openai", "plan_name": "chatgpt_plus"})
    codex_aliases = _subscription_pool_aliases({"provider": "openai", "plan_name": "codex_plus"})

    assert "openai_subscription" in chatgpt_aliases
    assert "chatgpt_subscription" in chatgpt_aliases
    assert "codex_subscription" not in chatgpt_aliases
    assert "openai_subscription" in codex_aliases
    assert "codex_subscription" in codex_aliases
    assert "chatgpt_subscription" not in codex_aliases


def test_best_plan_honors_chatgpt_and_codex_workspace_pools():
    plans = {
        "openai": [
            {"provider": "openai", "plan_name": "chatgpt_pro", "auth_method": "subscription"},
            {"provider": "openai", "plan_name": "chatgpt_pro_100", "auth_method": "subscription"},
            {
                "provider": "openai",
                "plan_name": "codex_pro_200_20x",
                "auth_method": "subscription",
                "parent_plan_id": "codex_pro_200",
            },
            {"provider": "openai", "plan_name": "codex_plus", "auth_method": "subscription"},
        ]
    }

    assert _best_plan(plans, "openai", {"chatgpt_pro_100"})["plan_name"] == "chatgpt_pro_100"
    assert _best_plan(plans, "openai", {"chatgpt_subscription"})["plan_name"] == "chatgpt_pro"
    assert _best_plan(plans, "openai", {"codex_subscription"})["plan_name"] == "codex_pro_200_20x"
    assert _best_plan(plans, "openai", {"codex_pro_200"})["plan_name"] == "codex_pro_200_20x"
    assert _best_plan(plans, "openai", {"codex_plus"})["plan_name"] == "codex_plus"


def test_best_plan_infers_openai_family_without_workspace_pool():
    plans = {
        "openai": [
            {"provider": "openai", "plan_name": "codex_pro_200_20x", "auth_method": "subscription"},
            {"provider": "openai", "plan_name": "chatgpt_pro", "auth_method": "subscription"},
        ]
    }

    chatgpt_candidate = {"provider": "openai", "model_id": "gpt-5.5", "display_name": "GPT-5.5"}
    codex_candidate = {"provider": "openai", "model_id": "gpt-5.3-codex", "display_name": "GPT-5.3 Codex"}

    assert _best_plan(plans, "openai", candidate=chatgpt_candidate)["plan_name"] == "chatgpt_pro"
    assert _best_plan(plans, "openai", candidate=codex_candidate)["plan_name"] == "codex_pro_200_20x"


def test_g1_priority_quality_floor_boundary(monkeypatch):
    monkeypatch.setattr(get_settings().knemon, "g1_quality_floor", 0.85)
    candidates = [
        {"provider": "cheap", "model_id": "below", "quality": 0.8499, "tier": "A", "graeae_weight": 0.99},
        {"provider": "good", "model_id": "at-floor", "quality": 0.85, "tier": "B", "graeae_weight": 0.75},
    ]
    assert _apply_priority_ceiling(candidates, 14) == [candidates[1]]


def test_burned_g1_session_keeps_requested_quality_floor(monkeypatch):
    monkeypatch.setattr(get_settings().knemon, "g1_quality_floor", 0.85)
    candidates = [
        {"provider": "cheap", "model_id": "below", "quality": 0.80, "tier": "B", "graeae_weight": 0.99},
        {"provider": "good", "model_id": "frontier", "quality": 0.90, "tier": "A", "graeae_weight": 0.75},
    ]
    assert _apply_priority_ceiling(candidates, 13, requested_priority=14) == [candidates[1]]


@pytest.mark.parametrize(
    ("as_of", "expected_plan"),
    [
        (date(2026, 5, 31), "claude_max_200"),
        (date(2026, 6, 1), "claude_max_100"),
    ],
)
@pytest.mark.asyncio
async def test_date_aware_plan_selection_honors_tier_flip_date(as_of: date, expected_plan: str):
    backend = _SqliteKnemonBackend(0)
    backend.conn.executescript(
        """
        DROP TABLE subscription_plans;
        CREATE TABLE subscription_plans (
          provider TEXT NOT NULL,
          plan_name TEXT NOT NULL,
          auth_method TEXT NOT NULL,
          path_kind TEXT NOT NULL,
          monthly_usd NUMERIC,
          msg_cap NUMERIC,
          msg_window_seconds NUMERIC,
          token_cap NUMERIC,
          token_window_seconds NUMERIC,
          reset_anchor TEXT,
          overage_pricing_per_mtok_in NUMERIC,
          overage_pricing_per_mtok_out NUMERIC,
          effective_from DATE NOT NULL,
          effective_until DATE,
          parent_plan_id TEXT
        );
        """
    )
    backend.conn.executemany(
        """
        INSERT INTO subscription_plans VALUES
          ('anthropic', ?, 'subscription', 'interactive', ?, ?, 18000, NULL, NULL,
           'rolling', NULL, NULL, ?, ?, NULL)
        """,
        [
            ("claude_max_200", 200, 900, "2026-04-01", "2026-05-31"),
            ("claude_max_100", 100, 225, "2026-06-01", None),
        ],
    )
    backend.conn.commit()

    plans = await _plans_by_provider(backend, as_of=as_of)

    assert [plan["plan_name"] for plan in plans["anthropic"]] == [expected_plan]


@pytest.mark.asyncio
async def test_session_burn_threshold_honors_env_override(monkeypatch):
    monkeypatch.setenv("MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR", "12")
    config_mod._reset_settings_for_tests()
    try:
        below = _SqliteKnemonBackend(70, burned_session="custom-threshold-below", burned_request_count=10)
        below_decision = await route(_req(14, "custom-threshold-below"), below)
        assert below_decision.provider == "openai"
        assert below_decision.auth_method == "subscription"

        at_threshold = _SqliteKnemonBackend(70, burned_session="custom-threshold-at", burned_request_count=12)
        at_threshold_decision = await route(_req(14, "custom-threshold-at"), at_threshold)
        assert at_threshold_decision.provider == "xai"
        assert at_threshold_decision.auth_method == "api"
    finally:
        config_mod._reset_settings_for_tests()


@pytest.mark.asyncio
async def test_session_below_burn_threshold_keeps_g1_subscription_escalation():
    backend = _SqliteKnemonBackend(70, burned_session="not-yet-burned", burned_request_count=9)
    decision = await route(_req(14, "not-yet-burned"), backend)
    assert decision.provider == "openai"
    assert decision.auth_method == "subscription"
