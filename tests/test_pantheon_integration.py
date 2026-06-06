"""End-to-end integration: RouterRuntime + write-behind cooldown over SQLite.

Proves the full routing stack composes: a rate-limited lead trips the breaker,
the trip persists through the write-behind store to SQLite, the request falls
over to a backup, and after a 'restart' the cooled lead is still skipped
(read-through from the durable tier).
"""

from __future__ import annotations

import asyncio

from mnemos.domain.pantheon.cooldown import CooldownManager
from mnemos.domain.pantheon.cooldown_cache import WriteBehindCooldownStore
from mnemos.domain.pantheon.cooldown_sqlite import SqliteCooldownStore
from mnemos.domain.pantheon.errors import normalize_error
from mnemos.domain.pantheon.runtime import RouterRuntime

NOW = 9000.0


async def _noop_sleep(_d):
    return None


def _classify(exc):
    return normalize_error(status_code=getattr(exc, "status_code", None), body=getattr(exc, "body", None))


class FakeHTTPError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _runtime(path):
    mgr = CooldownManager(WriteBehindCooldownStore(SqliteCooldownStore(path), flush_threshold=1))
    rt = RouterRuntime(mgr, clock=lambda: NOW, sleep=_noop_sleep, rng=lambda: 0.0)
    return rt, mgr


def test_full_stack_trip_persist_fallover_then_restart_recovery(tmp_path):
    path = str(tmp_path / "cd.sqlite")
    rt, mgr = _runtime(path)

    calls = []

    async def call(d):
        calls.append(d)
        if d == "lead":
            raise FakeHTTPError(429)  # rate-limited lead, multi-group -> trips breaker
        return "served-by-backup"

    # 1st request: lead 429 trips cooldown (persisted), falls over to backup
    res = asyncio.run(rt.route(["lead", "backup"], call, classify=_classify))
    assert res.result == "served-by-backup"
    assert res.deployment == "backup"
    assert calls == ["lead", "backup"]  # lead tried once (NOT retried — cooled), then fell over
    assert mgr.is_cooled("lead", NOW) is True

    # 2nd request (same process): lead is pre-filtered out, backup chosen directly
    calls.clear()

    async def call2(d):
        calls.append(d)
        return "ok2"

    res2 = asyncio.run(rt.route(["lead", "backup"], call2, classify=_classify))
    assert res2.result == "ok2"
    assert calls == ["backup"]  # lead skipped (cooled)

    # 3rd: "restart" — fresh runtime + fresh write-behind over the SAME sqlite file.
    rt3, mgr3 = _runtime(path)
    assert mgr3.is_cooled("lead", NOW) is True  # recovered from durable via read-through

    calls.clear()

    async def call3(d):
        calls.append(d)
        return "ok3"

    res3 = asyncio.run(rt3.route(["lead", "backup"], call3, classify=_classify))
    assert res3.result == "ok3"
    assert calls == ["backup"]  # still skips the durably-cooled lead after restart


def test_full_stack_cooldown_expires_lead_returns(tmp_path):
    path = str(tmp_path / "cd.sqlite")
    rt, mgr = _runtime(path)

    async def call(d):
        if d == "lead":
            raise FakeHTTPError(429)
        return "backup"

    asyncio.run(rt.route(["lead", "backup"], call, classify=_classify))
    assert mgr.is_cooled("lead", NOW) is True
    # after the cooldown window, the lead is usable again (logical-TTL expiry)
    assert mgr.is_cooled("lead", NOW + 10.0) is False
