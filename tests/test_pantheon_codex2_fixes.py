"""Regression tests for the 5 Codex pre-merge findings (new branch work)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.cooldown_cache import WriteBehindCooldownStore
from mnemos.domain.pantheon.cooldown_oracle import OracleCooldownStore
from mnemos.domain.pantheon.gateway import PantheonGatewayError, forward_chat_completion
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.runtime import RouterRuntime


# Finding 2: flush() must not lose ops if a durable write fails mid-flush.
def test_flush_requeues_remainder_on_failure():
    class _FailOnSecondIncr(InMemoryCooldownStore):
        def __init__(self):
            super().__init__()
            self.n = 0

        def incr(self, *a, **k):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("durable down")
            super().incr(*a, **k)

    durable = _FailOnSecondIncr()
    s = WriteBehindCooldownStore(durable, flush_threshold=100)
    s.incr("t", "d", 1, success=True)
    s.incr("t", "d", 1, success=False)
    s.incr("t", "d", 1, success=True)
    with pytest.raises(RuntimeError):
        s.flush()
    assert s.pending == 2  # the failed op + the remaining one were requeued, not lost


# Finding 3: Oracle MERGE retries once on a concurrent-first-insert ORA-00001.
def test_oracle_write_retries_on_unique_violation():
    class _Cur:
        def __init__(self, pool):
            self._pool = pool

        def execute(self, sql, params=None):
            self._pool.exec_calls += 1
            if self._pool.exec_calls == 1:
                raise RuntimeError("ORA-00001: unique constraint violated")

        def close(self):
            pass

    class _Conn:
        def __init__(self, pool):
            self._pool = pool

        def cursor(self):
            return _Cur(self._pool)

        def commit(self):
            pass

    class _Pool:
        def __init__(self):
            self.exec_calls = 0

        def acquire(self):
            return _Conn(self)

        def release(self, _c):
            pass

    pool = _Pool()
    OracleCooldownStore(pool).set_cooldown("t", "d", 5.0)
    assert pool.exec_calls == 2  # first ORA-00001, retry succeeded


# Finding 1: a non-OpenAI candidate is filtered out of the chain (graeae-only).
def test_non_openai_candidate_filtered_from_chain(monkeypatch):
    gateway.set_runtime(
        RouterRuntime(
            CooldownManager(InMemoryCooldownStore()),
            clock=time.time,
            sleep=lambda _d: asyncio.sleep(0),
            rng=lambda: 0.0,
        )
    )
    monkeypatch.setattr(
        gateway, "get_settings", lambda: SimpleNamespace(pantheon=SimpleNamespace(cross_provider_fallback=True))
    )
    monkeypatch.setattr(
        gateway,
        "_provider_config",
        lambda d: {"api": "openai", "url": "http://x"} if d.provider == "openai" else {"api": "gemini"},
    )

    async def _models():
        return [
            {"id": "gpt-5.4", "provider": "openai"},
            {"id": "gemini-3-pro", "provider": "gemini"},
        ]

    import mnemos.domain.pantheon.catalog as catalog

    monkeypatch.setattr(catalog, "list_models", _models)

    seen = []

    async def fake(decision, body):
        seen.append(decision.provider)
        raise PantheonGatewayError(500, "down")

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    dec = RouteDecision(
        alias="auto:code",
        provider="openai",
        model_id="gpt-5.4",
        route_type="single",
        reason="r",
        candidates=["gpt-5.4", "gemini-3-pro"],
    )
    with pytest.raises(PantheonGatewayError):
        asyncio.run(forward_chat_completion(dec, {"messages": []}))
    assert "gemini" not in seen  # gemini (non-openai api) was filtered out of the chain
    gateway.set_runtime(None)
