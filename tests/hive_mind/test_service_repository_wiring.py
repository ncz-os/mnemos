from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from mnemos.core.plan_windows import compute_plan_window_id
from mnemos.domain.knemon.router import KnemonRouteRequest, route
from mnemos.hive_mind import service
from mnemos.hive_mind.repository import SqliteHiveMindRepository


class _ServiceDbKnemonBackend:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)

    @asynccontextmanager
    async def transactional(self):
        yield self.conn


def test_hive_service_register_create_and_claim_use_repository_contract(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        orchestrator = client.post(
            "/v1/agents/register",
            json={
                "runtime": "claude-code",
                "host": "studio",
                "provider": "anthropic",
                "model": "claude",
                "capabilities": ["code"],
            },
        )
        assert orchestrator.status_code == 200, orchestrator.text
        submitter_urn = orchestrator.json()["urn"]

        worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "codex",
                "host": "worker",
                "provider": "local",
                "model": "native",
                "capabilities": ["code"],
            },
        )
        assert worker.status_code == 200, worker.text
        worker_urn = worker.json()["urn"]

        created = client.post(
            "/v1/jobs",
            json={
                "submitter_urn": submitter_urn,
                "kind": "code-edit",
                "description": "fix the thing",
                "priority": 10,
                "required_capabilities": ["code"],
                "eligible_kinds": ["codex"],
                "max_cost_tier": "A",
                "project": "mnemos-test",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        queued = client.get("/v1/jobs", params={"status": "queued"})
        assert queued.status_code == 200, queued.text
        assert [job["id"] for job in queued.json()["jobs"]] == [job_id]

        claimed = client.post("/v1/jobs/next", params={"agent_urn": worker_urn})
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        assert body["id"] == job_id
        assert body["claimed_by"] == worker_urn
        assert body["claimed_provider"] == "local"


def test_hive_service_claim_respects_required_capabilities(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-caps.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        orchestrator = client.post(
            "/v1/agents/register",
            json={"runtime": "claude-code", "host": "studio", "capabilities": ["code"]},
        )
        assert orchestrator.status_code == 200, orchestrator.text
        worker = client.post(
            "/v1/agents/register",
            json={"runtime": "codex", "host": "worker", "provider": "local", "capabilities": ["code"]},
        )
        assert worker.status_code == 200, worker.text

        created = client.post(
            "/v1/jobs",
            json={
                "submitter_urn": orchestrator.json()["urn"],
                "kind": "gpu-build",
                "required_capabilities": ["gpu"],
                "eligible_kinds": ["codex"],
                "max_cost_tier": "A",
            },
        )
        assert created.status_code == 200, created.text

        claimed = client.post("/v1/jobs/next", params={"agent_urn": worker.json()["urn"]})
        assert claimed.status_code == 204, claimed.text


def test_hive_routing_patch_enforces_zeroclaw_model_affinity(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-routing.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        orchestrator = client.post(
            "/v1/agents/register",
            json={"runtime": "claude-code", "host": "studio", "capabilities": ["coding"]},
        )
        assert orchestrator.status_code == 200, orchestrator.text

        wrong_worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "zeroclaw",
                "host": "wrong",
                "provider": "groq",
                "model": "llama-3.1-8b-instant",
                "capabilities": ["coding", "model:groq_llama_3_1_8b_instant"],
            },
        )
        assert wrong_worker.status_code == 200, wrong_worker.text

        right_worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "zeroclaw",
                "host": "right",
                "provider": "groq",
                "model": "qwen3-32b",
                "capabilities": ["coding", "model:groq_qwen3_32b"],
            },
        )
        assert right_worker.status_code == 200, right_worker.text

        created = client.post(
            "/v1/jobs",
            json={
                "submitter_urn": orchestrator.json()["urn"],
                "kind": "code-fix",
                "description": "fix the router",
                "priority": 10,
                "max_cost_tier": "B",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        routed = client.patch(
            f"/v1/jobs/{job_id}/routing",
            json={
                "required_capabilities": ["coding", "model:groq_qwen3_32b"],
                "eligible_kinds": ["zeroclaw"],
                "preferred_providers": ["groq"],
                "preferred_models": ["qwen3-32b"],
                "max_cost_tier": "B",
                "routing_metadata": {"router": "knemon"},
            },
        )
        assert routed.status_code == 200, routed.text

        wrong_claim = client.post("/v1/jobs/next", params={"agent_urn": wrong_worker.json()["urn"]})
        assert wrong_claim.status_code == 204, wrong_claim.text

        right_claim = client.post("/v1/jobs/next", params={"agent_urn": right_worker.json()["urn"]})
        assert right_claim.status_code == 200, right_claim.text
        assert right_claim.json()["id"] == job_id
        assert right_claim.json()["preferred_models"] == ["qwen3-32b"]


def test_default_cap_zeroclaw_route_to_tier_b_is_claimable(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-default-cap.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        orchestrator = client.post(
            "/v1/agents/register",
            json={"runtime": "claude-code", "host": "studio", "capabilities": ["coding"]},
        )
        assert orchestrator.status_code == 200, orchestrator.text
        worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "zeroclaw",
                "host": "worker",
                "provider": "groq",
                "model": "qwen3-32b",
                "capabilities": ["coding", "model:groq_qwen3_32b"],
            },
        )
        assert worker.status_code == 200, worker.text
        created = client.post(
            "/v1/jobs",
            json={
                "submitter_urn": orchestrator.json()["urn"],
                "kind": "code-fix",
                "description": "default cap should route to cheap provider",
                "priority": 10,
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        listed = client.get("/v1/jobs", params={"status": "queued"})
        job = listed.json()["jobs"][0]
        assert job["max_cost_tier"] == "A"
        assert job["routing_metadata"]["submitter_max_cost_tier_explicit"] is False

        routed = client.patch(
            f"/v1/jobs/{job_id}/routing",
            json={
                "required_capabilities": ["coding", "model:groq_qwen3_32b"],
                "eligible_kinds": ["zeroclaw"],
                "preferred_providers": ["groq"],
                "preferred_models": ["qwen3-32b"],
                "max_cost_tier": "B",
                "routing_metadata": {"router": "knemon", "submitter_max_cost_tier_explicit": False},
            },
        )
        assert routed.status_code == 200, routed.text

        claimed = client.post("/v1/jobs/next", params={"agent_urn": worker.json()["urn"]})
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["id"] == job_id
        assert claimed.json()["claimed_cost_tier"] == "B"


def test_explicit_tier_a_cap_rejects_tier_b_worker_after_routing(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-explicit-cap.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        orchestrator = client.post("/v1/agents/register", json={"runtime": "claude-code", "host": "studio"})
        worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "zeroclaw",
                "host": "worker",
                "provider": "groq",
                "model": "qwen3-32b",
                "capabilities": ["coding", "model:groq_qwen3_32b"],
            },
        )
        assert orchestrator.status_code == 200, orchestrator.text
        assert worker.status_code == 200, worker.text

        created = client.post(
            "/v1/jobs",
            json={
                "submitter_urn": orchestrator.json()["urn"],
                "kind": "code-fix",
                "description": "explicit free-only cap",
                "priority": 10,
                "max_cost_tier": "A",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        listed = client.get("/v1/jobs", params={"status": "queued"})
        assert listed.json()["jobs"][0]["routing_metadata"]["submitter_max_cost_tier_explicit"] is True

        routed = client.patch(
            f"/v1/jobs/{job_id}/routing",
            json={
                "required_capabilities": ["coding", "model:groq_qwen3_32b"],
                "eligible_kinds": ["zeroclaw"],
                "preferred_providers": ["groq"],
                "preferred_models": ["qwen3-32b"],
                "max_cost_tier": "A",
                "routing_metadata": {"router": "knemon", "submitter_max_cost_tier_explicit": True},
            },
        )
        assert routed.status_code == 200, routed.text

        claimed = client.post("/v1/jobs/next", params={"agent_urn": worker.json()["urn"]})
        assert claimed.status_code == 204, claimed.text


@pytest.mark.asyncio
async def test_registered_worker_subscription_pools_feed_knemon_routing(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-pools.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        worker = client.post(
            "/v1/agents/register",
            json={
                "runtime": "codex",
                "host": "worker",
                "provider": "openai",
                "model": "gpt-sub",
                "subscription_pools": ["anthropic_subscription"],
            },
        )
        assert worker.status_code == 200, worker.text
        session_id = worker.json()["session_id"]
        agents = client.get("/v1/agents")
        assert agents.json()["agents"][0]["subscription_pools"] == ["anthropic_subscription"]

    now = datetime.now(timezone.utc)
    window_id = compute_plan_window_id("openai", "chatgpt_plus", now, reset_anchor="rolling", window_seconds=18000)
    with sqlite3.connect(db_path) as db:
        db.executescript(
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
            """
        )
        db.execute(
            "INSERT INTO model_registry VALUES "
            "('openai', 'gpt-sub', 'GPT Sub', ?, 2, 8, 200000, 1400, 0.95, 1, 0), "
            "('xai', 'grok-api', 'Grok API', ?, 3, 15, 200000, 1220, 0.86, 1, 0)",
            (json.dumps(["code"]), json.dumps(["code"])),
        )
        db.execute(
            "INSERT INTO subscription_plans VALUES "
            "('openai', 'chatgpt_plus', 'subscription', 'interactive', 20, 160, 18000, "
            "NULL, NULL, 'rolling', NULL, NULL, '2026-01-01', NULL, NULL), "
            "('xai', 'api', 'api', 'api', NULL, NULL, NULL, "
            "NULL, NULL, 'monthly', NULL, NULL, '2026-01-01', NULL, NULL)"
        )
        db.execute(
            """
            INSERT INTO usage_ledger (
              ts, provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
              est_cost_usd, latency_ms, outcome, caller_subsystem, tier, session_id,
              request_count, plan_window_id, path_kind, subscription_amortized
            ) VALUES (?, 'openai', 'gpt-sub', 'code-fix', 100, 50, 0, 0, 100, 'ok',
                      'test', 'chatgpt_plus', 'usage-fixture', 1, ?, 'api', 1)
            """,
            (now, window_id),
        )

    decision = await route(
        KnemonRouteRequest(
            task_kind="code-fix",
            priority=14,
            est_tokens_in=10_000,
            est_tokens_out=2_000,
            caller_session_id=session_id,
            caller_subsystem="pytest",
            require_capability=["code"],
        ),
        _ServiceDbKnemonBackend(db_path),
    )

    assert decision.provider == "xai"
    assert "lacks pool" in decision.reasoning


def test_zeroclaw_worker_registration_must_declare_runtime(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "hive-service-zeroclaw-runtime.sqlite3")
    monkeypatch.setattr(service, "DB_PATH", db_path)
    monkeypatch.setattr(service, "_REPO", SqliteHiveMindRepository(db_path))

    with TestClient(service.app) as client:
        missing_runtime = client.post(
            "/v1/agents/register",
            json={"kind": "zeroclaw", "host": "worker", "provider": "groq", "model": "qwen3-32b"},
        )
        assert missing_runtime.status_code == 422, missing_runtime.text

        registered = client.post(
            "/v1/agents/register",
            json={"kind": "zeroclaw", "runtime": "zeroclaw", "host": "worker", "provider": "groq"},
        )
        assert registered.status_code == 200, registered.text

        create = client.post(
            "/v1/jobs",
            json={"submitter_urn": registered.json()["urn"], "kind": "code-fix", "description": "should reject"},
        )
        assert create.status_code == 403, create.text
