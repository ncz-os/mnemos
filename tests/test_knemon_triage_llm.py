from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.persistence.base import UsageLedgerRecord


@pytest.mark.asyncio
async def test_triage_prefers_cost_per_quality_best_value(monkeypatch):
    from mnemos.domain.pantheon import catalog, triage

    async def fake_models():
        return [
            {
                "id": "expensive",
                "provider": "openai",
                "capabilities": ["chat", "reasoning"],
                "available": True,
                "deprecated": False,
                "arena_rank": 1,
                "price_in": 10.0,
                "price_out": 10.0,
                "model_max_ctx": 128000,
                "last_synced": 10,
            },
            {
                "id": "balanced",
                "provider": "xai",
                "capabilities": ["chat", "reasoning"],
                "available": True,
                "deprecated": False,
                "arena_rank": 5,
                "price_in": 1.0,
                "price_out": 1.0,
                "model_max_ctx": 128000,
                "last_synced": 20,
            },
            {
                "id": "too-small",
                "provider": "tiny",
                "capabilities": ["chat", "reasoning"],
                "available": True,
                "deprecated": False,
                "arena_rank": 2,
                "price_in": 0.01,
                "price_out": 0.01,
                "model_max_ctx": 100,
                "last_synced": 30,
            },
        ]

    monkeypatch.setattr(catalog, "list_models", fake_models)

    recommended = await triage.recommend("reason", "standard", 1000, "med")

    assert recommended["id"] == "balanced"
    assert recommended["triage_score"] > 0


@pytest.mark.asyncio
async def test_triage_price_absent_fallback_skips_price_weighting(monkeypatch, caplog):
    from mnemos.domain.pantheon import catalog, triage

    async def fake_models():
        return [
            {
                "id": "older",
                "provider": "openai",
                "capabilities": ["chat"],
                "available": True,
                "deprecated": False,
                "arena_rank": 20,
                "model_max_ctx": 128000,
                "last_synced": 10,
            },
            {
                "id": "newer",
                "provider": "xai",
                "capabilities": ["chat"],
                "available": True,
                "deprecated": False,
                "arena_rank": 10,
                "model_max_ctx": 128000,
                "last_synced": 20,
            },
        ]

    monkeypatch.setattr(catalog, "list_models", fake_models)
    monkeypatch.setattr(triage, "_PRICE_ABSENT_LOGGED", False)

    with caplog.at_level("WARNING"):
        recommended = await triage.recommend("chat", "standard", 1000, "high")

    assert recommended["id"] == "newer"
    assert "skipping price weighting" in caplog.text


class _FailingEngine:
    async def route(self, *args, **kwargs):
        raise RuntimeError("provider exploded")


class _LedgerBackend:
    def __init__(self) -> None:
        self.records: list[UsageLedgerRecord] = []

    @asynccontextmanager
    async def transactional(self):
        yield object()

    async def record_usage_ledger(self, tx, record: UsageLedgerRecord):
        self.records.append(record)
        return SimpleNamespace(id=1, est_cost_usd=0)


@pytest.mark.asyncio
async def test_llm_wrapper_records_in_finally_on_exception(monkeypatch):
    import mnemos.llm as llm

    backend = _LedgerBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    async def fake_recommend(*args, **kwargs):
        return {"id": "gpt-test", "provider": "openai"}

    monkeypatch.setattr(llm.triage, "recommend", fake_recommend)
    monkeypatch.setattr(llm, "get_graeae_engine", lambda: _FailingEngine())

    with pytest.raises(RuntimeError, match="provider exploded"):
        await llm.call(
            llm.Task(
                prompt="hello",
                task_kind="chat",
                caller_subsystem="unit-test",
                tier="standard",
            )
        )

    assert backend.records == [
        UsageLedgerRecord(
            provider="openai",
            model="gpt-test",
            task_kind="chat",
            tokens_in=0,
            tokens_out=0,
            tokens_reasoning=0,
            latency_ms=backend.records[0].latency_ms,
            outcome="err",
            caller_subsystem="unit-test",
            tier="standard",
        )
    ]
