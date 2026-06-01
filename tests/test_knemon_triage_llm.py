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


@pytest.mark.asyncio
async def test_triage_canonical_registry_prices_keep_price_weighting(monkeypatch, caplog):
    from mnemos.domain.pantheon import catalog, triage

    async def fake_models():
        return [
            {
                "id": "expensive",
                "provider": "openai",
                "capabilities": ["chat"],
                "available": True,
                "deprecated": False,
                "arena_rank": 1,
                "input_cost_per_mtok": 20.0,
                "output_cost_per_mtok": 20.0,
                "context_window": 128000,
                "last_synced": 10,
            },
            {
                "id": "cheap-qualified",
                "provider": "xai",
                "capabilities": ["chat"],
                "available": True,
                "deprecated": False,
                "arena_rank": 6,
                "input_cost_per_mtok": 1.0,
                "output_cost_per_mtok": 1.0,
                "context_window": 128000,
                "last_synced": 20,
            },
            {
                "id": "cost-only",
                "provider": "local",
                "capabilities": ["chat"],
                "available": True,
                "deprecated": False,
                "arena_rank": 100,
                "cost_per_mtok": 50.0,
                "context_window": 128000,
                "last_synced": 30,
            },
        ]

    monkeypatch.setattr(catalog, "list_models", fake_models)
    monkeypatch.setattr(triage, "_PRICE_ABSENT_LOGGED", False)

    with caplog.at_level("WARNING"):
        recommended = await triage.recommend("chat", "standard", 1000, "med")

    assert recommended["id"] == "cheap-qualified"
    assert "skipping price weighting" not in caplog.text


class _FailingEngine:
    async def route(self, *args, **kwargs):
        raise RuntimeError("provider exploded")


class _SuccessEngine:
    async def route(self, *args, **kwargs):
        return {
            "status": "success",
            "latency_ms": 10,
            "usage": {"input_tokens": 12, "output_tokens": 5, "reasoning_tokens": 2},
        }


class _LedgerBackend:
    def __init__(self) -> None:
        self.records: list[UsageLedgerRecord] = []

    @asynccontextmanager
    async def transactional(self):
        yield object()

    async def record_usage_ledger(self, tx, record: UsageLedgerRecord):
        self.records.append(record)
        return SimpleNamespace(id=1, est_cost_usd=0)


class _FailingLedgerBackend(_LedgerBackend):
    async def record_usage_ledger(self, tx, record: UsageLedgerRecord):
        self.records.append(record)
        raise NotImplementedError("base recorder")


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
            plan_window_id=backend.records[0].plan_window_id,
        )
    ]
    assert backend.records[0].plan_window_id.startswith("openai-standard-")


@pytest.mark.asyncio
async def test_llm_wrapper_marks_result_degraded_on_ledger_failure(monkeypatch, caplog):
    import mnemos.llm as llm

    backend = _FailingLedgerBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    async def fake_recommend(*args, **kwargs):
        return {"id": "gpt-test", "provider": "openai"}

    monkeypatch.setattr(llm.triage, "recommend", fake_recommend)
    monkeypatch.setattr(llm, "get_graeae_engine", lambda: _SuccessEngine())

    with caplog.at_level("ERROR"):
        result = await llm.call(
            llm.Task(
                prompt="hello",
                task_kind="chat",
                caller_subsystem="unit-test",
                tier="standard",
            )
        )

    assert result["status"] == "success"
    assert result["ledger_status"] == "degraded"
    assert result["ledger_error"]
    assert result["ledger_record_id"] is None
    assert backend.records[0].tokens_in == 12
    assert backend.records[0].tokens_out == 5
    assert backend.records[0].tokens_reasoning == 2
    assert "usage_ledger recording failed" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_llm_wrapper_marks_result_ok_on_successful_ledger_record(monkeypatch):
    import mnemos.llm as llm

    backend = _LedgerBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    async def fake_recommend(*args, **kwargs):
        return {"id": "gpt-test", "provider": "openai"}

    monkeypatch.setattr(llm.triage, "recommend", fake_recommend)
    monkeypatch.setattr(llm, "get_graeae_engine", lambda: _SuccessEngine())

    result = await llm.call(
        llm.Task(
            prompt="hello",
            task_kind="chat",
            caller_subsystem="unit-test",
            tier="standard",
        )
    )

    assert result["status"] == "success"
    assert result["ledger_status"] == "ok"
    assert result["ledger_error"] is None
    assert isinstance(result["ledger_record_id"], int)
    assert backend.records[0].tokens_in == 12
    assert backend.records[0].tokens_out == 5
    assert backend.records[0].tokens_reasoning == 2
