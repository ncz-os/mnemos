from __future__ import annotations

from fastapi.testclient import TestClient

from mnemos.hive_mind import service
from mnemos.hive_mind.repository import SqliteHiveMindRepository


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
