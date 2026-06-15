"""Tests for per-tenant budget pre-gate + BYOK key-resolution seam."""

from __future__ import annotations

import pytest

from mnemos.domain.pantheon.budget import BudgetVerdict, evaluate_budget
from mnemos.domain.pantheon.keyvault import (
    ChainKeyResolver,
    EnvKeyResolver,
    MappedKeyResolver,
)


# ── budget ──
class _BudgetTx:
    conn = None


class _BudgetBackend:
    def __init__(self, spent_usd: float):
        self.spent_usd = spent_usd

    def transactional(self):
        return self

    async def __aenter__(self):
        return _BudgetTx()

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def budget_rows(monkeypatch):
    async def fake_rows(backend, sql, params=None):
        return [{"spent_usd": backend.spent_usd}]

    monkeypatch.setattr("mnemos.domain.knemon.budget._rows", fake_rows)


@pytest.mark.asyncio
async def test_budget_within(budget_rows):
    d = await evaluate_budget(backend=_BudgetBackend(2.0), limit_usd=10.0)
    assert d.allowed and d.verdict is BudgetVerdict.ALLOW and d.remaining_usd == 8.0


@pytest.mark.asyncio
async def test_budget_exhausted(budget_rows):
    d = await evaluate_budget(backend=_BudgetBackend(10.0), limit_usd=10.0)
    assert not d.allowed and "exhausted" in d.reason and d.remaining_usd == 0.0


@pytest.mark.asyncio
async def test_budget_estimate_would_exceed_denies_precall(budget_rows):
    d = await evaluate_budget(backend=_BudgetBackend(9.5), limit_usd=10.0, estimated_cost_usd=1.0)
    assert not d.allowed and "exceed" in d.reason


@pytest.mark.asyncio
async def test_budget_estimate_within_allows(budget_rows):
    d = await evaluate_budget(backend=_BudgetBackend(9.5), limit_usd=10.0, estimated_cost_usd=0.4)
    assert d.allowed


@pytest.mark.asyncio
async def test_budget_unlimited(monkeypatch, budget_rows):
    from mnemos.core.config import _reset_settings_for_tests

    monkeypatch.setenv("MNEMOS_KNEMON_WEEKLY_BUDGET_CAP_USD", "0")
    _reset_settings_for_tests()
    try:
        d = await evaluate_budget(backend=_BudgetBackend(1e9), limit_usd=None)
        assert d.allowed and d.remaining_usd == float("inf")
    finally:
        monkeypatch.delenv("MNEMOS_KNEMON_WEEKLY_BUDGET_CAP_USD", raising=False)
        _reset_settings_for_tests()


# ── keyvault ──
def test_env_resolver_tenant_agnostic():
    r = EnvKeyResolver(env={"OPENAI_API_KEY": "sk-shared"})
    assert r.resolve("tenantA", "openai") == "sk-shared"
    assert r.resolve("tenantB", "openai") == "sk-shared"  # shared across tenants
    assert r.resolve("tenantA", "groq") is None


def test_mapped_resolver_per_tenant():
    r = MappedKeyResolver({("A", "openai"): "sk-A", ("B", "openai"): "sk-B"})
    assert r.resolve("A", "openai") == "sk-A"
    assert r.resolve("B", "openai") == "sk-B"
    assert r.resolve("C", "openai") is None


def test_mapped_resolver_callable_store():
    r = MappedKeyResolver(lambda t, p: f"vault:{t}:{p}" if t == "A" else None)
    assert r.resolve("A", "openai") == "vault:A:openai"
    assert r.resolve("B", "openai") is None


def test_chain_byok_wins_else_shared():
    byok = MappedKeyResolver({("A", "openai"): "sk-A-own"})
    shared = EnvKeyResolver(env={"OPENAI_API_KEY": "sk-platform"})
    chain = ChainKeyResolver(byok, shared)
    assert chain.resolve("A", "openai") == "sk-A-own"  # tenant's own key wins
    assert chain.resolve("B", "openai") == "sk-platform"  # falls back to shared
    assert chain.resolve("A", "groq") is None  # neither has it
