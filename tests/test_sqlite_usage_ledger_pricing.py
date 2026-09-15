from decimal import Decimal
from types import SimpleNamespace

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.base import UsageLedgerRecord
from mnemos.persistence.sqlite import _execute


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [None, Decimal("1.25")])
async def test_fresh_schema_ledger_uses_registry_metadata_or_explicit_price(tmp_path, explicit):
    backend = SqliteBackend(tmp_path / "ledger.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await _execute(
                tx.conn,
                "INSERT INTO model_registry(provider,model_id,input_cost_per_mtok,"
                "output_cost_per_mtok,metadata) VALUES ('test','model',2,3,"
                "'{\"reasoning_cost_per_mtok\":4}')",
            )
            result = await backend.record_usage_ledger(
                tx,
                UsageLedgerRecord(
                    provider="test",
                    model="model",
                    task_kind="test",
                    tokens_in=1000,
                    tokens_out=2000,
                    tokens_reasoning=3000,
                    latency_ms=1,
                    outcome="ok",
                    caller_subsystem="test",
                    tier="api",
                    est_cost_usd=explicit,
                ),
            )
            assert result.est_cost_usd == (explicit if explicit is not None else Decimal("0.020"))
    finally:
        await backend.close()
