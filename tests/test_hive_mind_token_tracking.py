from __future__ import annotations

import importlib
import sqlite3

from fastapi.testclient import TestClient


def _fresh_hive_service(monkeypatch, tmp_path):
    db_path = tmp_path / "agents.db"
    monkeypatch.setenv("AGENT_BUS_DB", str(db_path))
    import mnemos.hive_mind.service as service

    return importlib.reload(service), db_path


def test_job_result_token_fields_round_trip(monkeypatch, tmp_path):
    service, db_path = _fresh_hive_service(monkeypatch, tmp_path)

    with TestClient(service.app) as client:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "INSERT INTO jobs (id, submitter_urn, kind, description, priority, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "job-token-roundtrip",
                    "urn:agent:human:test",
                    "doctor:codex-fix:test",
                    "token result round trip",
                    0,
                    "running",
                    1.0,
                ),
            )

        result = {
            "tokens_in": 100,
            "tokens_out": 50,
            "provider": "openai",
            "model": "gpt-5.5",
            "cost_usd_est": 0.0025,
        }
        patch_resp = client.patch(
            "/v1/jobs/job-token-roundtrip",
            json={"status": "done", "result": result},
        )
        assert patch_resp.status_code == 200, patch_resp.text

        get_resp = client.get("/v1/jobs/job-token-roundtrip")
        assert get_resp.status_code == 200, get_resp.text
        got_result = get_resp.json()["result"]
        for key, value in result.items():
            assert got_result[key] == value


def test_hive_sqlite_token_tracking_migration_is_idempotent(monkeypatch, tmp_path):
    service, db_path = _fresh_hive_service(monkeypatch, tmp_path)

    with TestClient(service.app):
        pass
    with TestClient(service.app):
        pass

    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}

    assert {
        "tokens_in",
        "tokens_out",
        "tokens_reasoning",
        "provider",
        "model",
        "cost_usd_est",
    }.issubset(columns)
