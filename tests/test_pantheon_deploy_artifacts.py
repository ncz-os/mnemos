from __future__ import annotations

from pathlib import Path

from mnemos.core import config

ROOT = Path(__file__).resolve().parents[1]
PANTHEON_DEPLOY = ROOT / "deploy" / "pantheon"


def test_pantheon_production_deploy_artifacts_exist() -> None:
    expected = {
        "README.md",
        "pantheon-gunicorn.sh",
        "pantheon-gunicorn.env.example",
        "pantheon-gateway.service",
        "Caddyfile.pantheon-vip-4100.snippet",
    }

    assert expected <= {path.name for path in PANTHEON_DEPLOY.iterdir()}


def test_pantheon_gunicorn_launcher_is_multi_worker_and_loopback_only() -> None:
    launcher = (PANTHEON_DEPLOY / "pantheon-gunicorn.sh").read_text(encoding="utf-8")

    assert "gunicorn" in launcher
    assert "mnemos.api.pantheon_shadow:app" in launcher
    assert "uvicorn.workers.UvicornWorker" in launcher
    assert '-w "${WEB_CONCURRENCY}"' in launcher
    assert '127.0.0.1:4110' in launcher
    assert "MNEMOS_NATS_URL" in launcher
    assert "MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT" in launcher


def test_pantheon_caddy_snippet_keeps_vip_and_documents_rollback() -> None:
    snippet = (PANTHEON_DEPLOY / "Caddyfile.pantheon-vip-4100.snippet").read_text(encoding="utf-8")

    assert ":4100" in snippet
    assert "reverse_proxy 127.0.0.1:4110" in snippet
    assert "health_uri /health" in snippet
    assert "Rollback one-liner" in snippet
    assert "inference-api" in snippet


def test_pantheon_gateway_rate_limit_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT", "777/minute")
    config.reload_settings()
    try:
        assert config.get_settings().rate_limit.pantheon_gateway == "777/minute"
    finally:
        monkeypatch.delenv("MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT", raising=False)
        config.reload_settings()
