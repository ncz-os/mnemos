"""Tests for the PANTHEON RouterRuntime composition."""

from __future__ import annotations

import asyncio

import pytest

from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.errors import normalize_error
from mnemos.domain.pantheon.fallback import AllDeploymentsFailed
from mnemos.domain.pantheon.runtime import RouterRuntime

NOW = 5000.0


class FakeHTTPError(Exception):
    def __init__(self, status_code=None, body=None):
        super().__init__(body or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


def _classify(exc):
    return normalize_error(status_code=getattr(exc, "status_code", None), body=getattr(exc, "body", None))


def _scripted(script):
    state = {k: list(v) for k, v in script.items()}
    calls = []

    async def call(dep):
        calls.append(dep)
        outcome = state[dep].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return call, calls


def _runtime(clock_value=NOW):
    mgr = CooldownManager(InMemoryCooldownStore())
    rt = RouterRuntime(
        mgr,
        clock=lambda: clock_value,
        sleep=_noop_sleep,
        rng=lambda: 0.0,
    )
    return rt, mgr


async def _noop_sleep(_d):
    return None


def test_success_records_success_and_returns():
    rt, mgr = _runtime()
    call, calls = _scripted({"a": ["ok"]})
    res = asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    assert res.result == "ok"
    assert calls == ["a"]
    # success counter incremented for the chosen deployment
    assert mgr._store.get_counts("_default", "a", int(NOW // 60)) == (1, 0)  # noqa: SLF001


def test_429_trips_cooldown_then_prefiltered_next_route():
    rt, mgr = _runtime()
    # first route: a 429s (multi-group -> trips), b serves
    call, calls = _scripted({"a": [FakeHTTPError(429)], "b": ["ok"]})
    res = asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    assert res.deployment == "b"
    assert mgr.is_cooled("a", NOW) is True

    # second route over the same group: a is pre-filtered out, b chosen directly
    call2, calls2 = _scripted({"b": ["ok2"]})
    res2 = asyncio.run(rt.route(["a", "b"], call2, classify=_classify))
    assert res2.result == "ok2"
    assert calls2 == ["b"]  # 'a' never attempted — cooled


def test_single_deployment_group_not_cooled():
    rt, mgr = _runtime()
    call, _ = _scripted({"only": [FakeHTTPError(429), "recovered"]})
    res = asyncio.run(rt.route(["only"], call, classify=_classify))
    assert res.result == "recovered"
    assert mgr.is_cooled("only", NOW) is False  # never cool the only model


def test_all_cooled_falls_back_to_full_chain():
    rt, mgr = _runtime()
    # pre-cool both deployments
    mgr.record_failure("a", normalize_error(status_code=429), NOW, is_single_deployment_group=False)
    mgr.record_failure("b", normalize_error(status_code=429), NOW, is_single_deployment_group=False)
    assert mgr.filter_available(["a", "b"], NOW) == []
    # route still tries the full chain rather than failing outright
    call, calls = _scripted({"a": ["served-anyway"]})
    res = asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    assert res.result == "served-anyway"
    assert calls == ["a"]


def test_non_retryable_400_falls_over_records_failure():
    rt, mgr = _runtime()
    call, calls = _scripted({"a": [FakeHTTPError(400, "bad")], "b": ["ok"]})
    res = asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    assert res.deployment == "b"
    assert calls == ["a", "b"]
    # a 400 records a failure but does NOT cool (not cooldownable)
    assert mgr._store.get_counts("_default", "a", int(NOW // 60)) == (0, 1)  # noqa: SLF001
    assert mgr.is_cooled("a", NOW) is False


def test_all_fail_raises():
    rt, _ = _runtime()
    call, _ = _scripted({"a": [FakeHTTPError(400)], "b": [FakeHTTPError(404)]})
    with pytest.raises(AllDeploymentsFailed):
        asyncio.run(rt.route(["a", "b"], call, classify=_classify))


def test_tenant_scoped_cooldown():
    rt, mgr = _runtime()
    call, _ = _scripted({"a": [FakeHTTPError(429)], "b": ["ok"]})
    asyncio.run(rt.route(["a", "b"], call, classify=_classify, tenant="A"))
    assert mgr.is_cooled("a", NOW, tenant="A") is True
    assert mgr.is_cooled("a", NOW, tenant="B") is False


def test_empty_group_raises():
    rt, _ = _runtime()
    call, _ = _scripted({})
    with pytest.raises(ValueError):
        asyncio.run(rt.route([], call, classify=_classify))
