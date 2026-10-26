"""create_oracle_pool must bound acquire() and keep idle connections warm."""

from __future__ import annotations

import sys
import types

import pytest


def _fake_oracledb(captured: dict) -> types.ModuleType:
    module = types.ModuleType("oracledb")
    module.POOL_GETMODE_WAIT = 0
    module.POOL_GETMODE_NOWAIT = 1
    module.POOL_GETMODE_FORCEGET = 2
    module.POOL_GETMODE_TIMEDWAIT = 3

    def create_pool_async(*, statement_cache_size=None, **kwargs):
        captured.update(kwargs, statement_cache_size=statement_cache_size)
        return object()

    module.create_pool_async = create_pool_async
    module.init_oracle_client = lambda *a, **k: None
    return module


@pytest.mark.asyncio
async def test_pool_acquire_wait_is_bounded_and_idle_connections_are_kept(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "oracledb", _fake_oracledb(captured))
    from mnemos.persistence.oracle import create_oracle_pool

    await create_oracle_pool(
        "oracle://user:pw@127.0.0.1:1521/FREEPDB1",
        min_size=1,
        max_size=4,
        acquire_timeout=60.0,
    )

    # python-oracledb applies wait_timeout (ms) only in TIMEDWAIT mode.
    assert captured["getmode"] == 3
    assert captured["wait_timeout"] == 60_000
    # ``timeout`` is the idle-connection lifetime, not an acquire timeout.
    assert "timeout" not in captured
