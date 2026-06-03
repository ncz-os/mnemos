"""Regression tests for the 5 Codex adversarial-review findings on the routing core."""

from __future__ import annotations

import asyncio

from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.errors import normalize_error
from mnemos.domain.pantheon.gateway import (
    PantheonGatewayError,
    UpstreamIdentity,
    _decision_cooldown_key,
    _tenant_of,
    attach_upstream_identity,
)
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.runtime import RouterRuntime

NOW = 7000.0


async def _noop_sleep(_d):
    return None


def _classify(exc):
    return normalize_error(status_code=getattr(exc, "status_code", None), body=getattr(exc, "body", None))


class FakeHTTPError(Exception):
    def __init__(self, status_code=None, body=None):
        super().__init__(body or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


def _rt(clock=lambda: NOW):
    mgr = CooldownManager(InMemoryCooldownStore())
    return RouterRuntime(mgr, clock=clock, sleep=_noop_sleep, rng=lambda: 0.0), mgr


def _decision(**kw):
    base = dict(alias="a", provider="openai", model_id="m", route_type="single", reason="r")
    base.update(kw)
    return RouteDecision(**base)


# Finding 1 (HIGH): an unhashable deployment (RouteDecision with dict/list fields)
# must not crash — cooldown is keyed via key_of, not the object itself.
def test_finding1_unhashable_decision_routes_via_key_of():
    rt, mgr = _rt()
    dec = _decision(model={"x": 1}, candidates=["a", "b"])  # dict + list => unhashable
    calls = []

    async def call(d):
        calls.append(d)
        return "ok"

    res = asyncio.run(rt.route([dec], call, classify=_classify, key_of=_decision_cooldown_key))
    assert res.result == "ok"
    assert mgr._store.get_counts("_default", "openai:m", int(NOW // 60)) == (1, 0)  # noqa: SLF001


# Finding 3 (MED): when a 429 trips the breaker mid-request and siblings exist,
# the deployment must NOT be retried — it falls over immediately.
def test_finding3_cooled_deployment_falls_over_not_retried():
    rt, mgr = _rt()
    calls = []

    async def call(d):
        calls.append(d)
        if d == "a":
            raise FakeHTTPError(429)  # multi-group 429 -> trips cooldown for 'a'
        return "ok"

    res = asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    assert res.result == "ok"
    assert calls == ["a", "b"]  # 'a' tried once (NOT retried), then fell over
    assert mgr.is_cooled("a", NOW) is True


# Finding 5 (MED): cooldown timestamp uses the failure-observation time, not the
# request-start time.
def test_finding5_cooldown_uses_observation_time():
    clock = {"v": 1000.0}
    rt, mgr = _rt(clock=lambda: clock["v"])

    async def call(d):
        clock["v"] += 100.0  # time passes during the upstream attempt
        raise FakeHTTPError(429)

    try:
        asyncio.run(rt.route(["a", "b"], call, classify=_classify))
    except Exception:  # noqa: BLE001
        pass
    cooled_until = mgr._store.get_cooled_until("_default", "a")  # noqa: SLF001
    # based on observation time (>= 1100) + 5, not the 1000.0 request start
    assert cooled_until is not None and cooled_until >= 1105.0


# Finding 4 (MED): provider Retry-After is captured onto the gateway error.
def test_finding4_forward_chat_once_sets_retry_after(monkeypatch):
    class _Resp:
        status_code = 429
        text = "rate limited"
        headers = {"retry-after": "7"}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"url": "http://x"})
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})

    try:
        asyncio.run(gateway._forward_chat_once(_decision(), {"messages": []}))  # noqa: SLF001
        raise AssertionError("expected PantheonGatewayError")
    except PantheonGatewayError as e:
        assert e.status_code == 429
        assert e.retry_after == 7.0


# Finding 2 (HIGH): tenant + cooldown-key helpers derive correctly.
def test_finding2_tenant_and_key_helpers():
    assert _decision_cooldown_key(_decision(provider="openai", model_id="gpt")) == "openai:gpt"
    assert _decision_cooldown_key(_decision(provider="openai", model_id=None, alias="al")) == "openai:al"
    assert _tenant_of({}) == "_default"
    body = attach_upstream_identity(
        {"messages": []},
        UpstreamIdentity(user_id="u1", namespace="ns", session_id="s", request_id="r"),
    )
    assert _tenant_of(body) == "ns:u1"
