"""Regression test: the standalone PERSEPHONE worker entry point
(`python -m mnemos.workers.persephone_archival_worker`) must route through
the real PersistenceBackend interface, not a bare asyncpg pool.

Before this fix, `main()` built a raw asyncpg pool and passed it straight
to `persephone_archival_worker_loop`, which forwards it to
`sweep_for_archival` (mnemos/domain/persephone/runner.py). That function's
own dispatch — `if hasattr(pool, "transactional") and not
hasattr(pool, "acquire")` — only takes the portable
`mnemos.persistence.worker_lifecycle.sweep_for_archival` path (already
tested against SQLite in test_worker_lifecycle_backends.py) when handed a
real backend object. A raw asyncpg pool has `.acquire()` and no
`.transactional()`, so it fell through to a legacy branch instead.

This test doesn't need a live Postgres: it mocks `asyncpg.create_pool` and
the archival loop itself (which runs forever otherwise), and asserts
`main()` wraps the pool in a `PostgresBackend` before handing it to the
loop — proving the wiring, not re-testing the already-covered archival
logic itself.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mnemos.persistence.postgres import PostgresBackend
from mnemos.workers import persephone_archival_worker


@pytest.mark.asyncio
async def test_standalone_entrypoint_passes_a_backend_not_a_raw_pool():
    fake_raw_pool = SimpleNamespace(close=AsyncMock())
    fake_wrapped_pool = SimpleNamespace(close=AsyncMock())

    captured: dict = {}

    async def fake_loop(pool, **_kwargs):
        captured["pool"] = pool

    with (
        patch(
            "asyncpg.create_pool", new=AsyncMock(return_value=fake_raw_pool)
        ),
        patch.object(
            persephone_archival_worker,
            "persephone_archival_worker_loop",
            new=fake_loop,
        ),
        patch(
            "mnemos.core.pool.wrap_pool_with_timeout",
            return_value=fake_wrapped_pool,
        ),
    ):
        await persephone_archival_worker.main()

    assert "pool" in captured, "main() never called the archival loop"
    passed = captured["pool"]
    assert isinstance(passed, PostgresBackend), (
        f"main() passed a {type(passed).__name__} to the archival loop, "
        "not a PostgresBackend — this is exactly the bug the fix closes: "
        "sweep_for_archival's hasattr(pool, 'transactional') dispatch "
        "needs a real backend, not a raw pool, to take the portable path"
    )
    # The backend must wrap the SAME pool wrap_pool_with_timeout returned,
    # not the raw asyncpg pool directly (command-timeout wrapping would
    # otherwise be silently lost).
    assert passed._pool is fake_wrapped_pool
    fake_wrapped_pool.close.assert_awaited_once()
