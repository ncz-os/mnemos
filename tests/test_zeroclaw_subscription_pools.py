"""Guards for zeroclaw worker subscription-pool detection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = ROOT / "deploy" / "zeroclaw-fanout" / "zeroclaw_worker.py"


def _load_worker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zeroclaw_worker_for_test", WORKER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openai_plan_aliases_do_not_cross_grant_chatgpt_and_codex() -> None:
    worker = _load_worker()

    chatgpt_pools: set[str] = set()
    worker._add_plan_aliases(chatgpt_pools, "openai", "chatgpt_pro_100", family="chatgpt")

    assert "chatgpt_subscription" in chatgpt_pools
    assert "chatgpt_pro_100" in chatgpt_pools
    assert "codex_subscription" not in chatgpt_pools
    assert "openai_subscription" not in chatgpt_pools

    codex_pools: set[str] = set()
    worker._add_plan_aliases(codex_pools, "openai", "codex_pro_200", family="codex")

    assert "codex_subscription" in codex_pools
    assert "codex_pro_200" in codex_pools
    assert "chatgpt_subscription" not in codex_pools
    assert "openai_subscription" not in codex_pools


def test_openai_auth_file_alone_does_not_grant_subscription_pools(monkeypatch, tmp_path) -> None:
    worker = _load_worker()
    monkeypatch.setattr(worker.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHATGPT_PLAN", raising=False)
    monkeypatch.delenv("CODEX_PLAN", raising=False)
    monkeypatch.delenv("OPENAI_SUBSCRIPTION_POOLS", raising=False)
    monkeypatch.delenv("CLAUDE_SUBSCRIPTION_TIER", raising=False)
    (tmp_path / ".openai").mkdir()
    (tmp_path / ".openai" / "auth.json").write_text("{}", encoding="utf-8")

    assert worker._detect_subscription_pools() == []


def test_openai_subscription_pool_is_only_added_when_explicit(monkeypatch, tmp_path) -> None:
    worker = _load_worker()
    monkeypatch.setattr(worker.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHATGPT_PLAN", raising=False)
    monkeypatch.delenv("CODEX_PLAN", raising=False)
    monkeypatch.delenv("CLAUDE_SUBSCRIPTION_TIER", raising=False)
    monkeypatch.setenv("OPENAI_SUBSCRIPTION_POOLS", "openai_subscription")

    assert worker._detect_subscription_pools() == ["openai_subscription"]
