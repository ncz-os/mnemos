"""Tests for gateway.forward_chat_completion routed through RouterRuntime.

Behavior-preserving wiring: success returns data, transient 5xx is retried then
succeeds, a non-retryable 400 still surfaces as the original PantheonGatewayError.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.gateway import PantheonGatewayError, forward_chat_completion
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.runtime import RouterRuntime


async def _noop_sleep(_d):
    return None


@pytest.fixture(autouse=True)
def _no_sleep_runtime():
    gateway.set_runtime(
        RouterRuntime(
            CooldownManager(InMemoryCooldownStore()),
            clock=time.time,
            sleep=_noop_sleep,
            rng=lambda: 0.0,
        )
    )
    yield
    gateway.set_runtime(None)


def _decision():
    return RouteDecision(alias="a", provider="p", model_id="m", route_type="single", reason="r")


def _force_openai(monkeypatch):
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"api": "openai", "url": "http://x"})


def test_success_returns_data(monkeypatch):
    _force_openai(monkeypatch)

    async def fake(decision, body):
        return {"model": "m", "ok": True}

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    out = asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert out == {"model": "m", "ok": True}


def test_transient_5xx_is_retried_then_succeeds(monkeypatch):
    _force_openai(monkeypatch)
    calls = {"n": 0}

    async def fake(decision, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PantheonGatewayError(503, "upstream down")
        return {"model": "m", "recovered": True}

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    out = asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert out["recovered"] is True
    assert calls["n"] == 2  # one failure + one retry on the same provider


def test_400_surfaces_unchanged(monkeypatch):
    _force_openai(monkeypatch)

    async def fake(decision, body):
        raise PantheonGatewayError(400, "bad request")

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    with pytest.raises(PantheonGatewayError) as ei:
        asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert ei.value.status_code == 400
    assert ei.value.message == "bad request"


def test_400_is_not_retried(monkeypatch):
    _force_openai(monkeypatch)
    calls = {"n": 0}

    async def fake(decision, body):
        calls["n"] += 1
        raise PantheonGatewayError(400, "bad")

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    with pytest.raises(PantheonGatewayError):
        asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert calls["n"] == 1  # non-retryable: single attempt, no retry


def test_consensus_still_delegates(monkeypatch):
    captured = {}

    async def fake_consensus(decision, body):
        captured["hit"] = True
        return {"consensus": True}

    monkeypatch.setattr(gateway, "consensus_chat_completion", fake_consensus)
    dec = RouteDecision(alias="c", provider="p", model_id=None, route_type="consensus", reason="r")
    out = asyncio.run(forward_chat_completion(dec, {"messages": []}))
    assert out == {"consensus": True}
    assert captured["hit"] is True
