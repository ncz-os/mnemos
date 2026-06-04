"""Tests for the gated cross-provider chain path in the gateway."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.gateway import PantheonGatewayError, forward_chat_completion
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.runtime import RouterRuntime


async def _noop_sleep(_d):
    return None


@pytest.fixture(autouse=True)
def _runtime():
    gateway.set_runtime(
        RouterRuntime(CooldownManager(InMemoryCooldownStore()), clock=time.time, sleep=_noop_sleep, rng=lambda: 0.0)
    )
    yield
    gateway.set_runtime(None)


def _decision():
    return RouteDecision(
        alias="auto:code",
        provider="openai",
        model_id="gpt-5.4",
        route_type="single",
        reason="r",
        candidates=["gpt-5.4", "deepseek-v4-flash"],
    )


def _enable_fallback(monkeypatch, on):
    monkeypatch.setattr(
        gateway, "get_settings", lambda: SimpleNamespace(pantheon=SimpleNamespace(cross_provider_fallback=on))
    )
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"api": "openai", "url": "http://x"})

    async def _models():
        return [
            {"id": "gpt-5.4", "provider": "openai"},
            {"id": "deepseek-v4-flash", "provider": "deepseek"},
        ]

    import mnemos.domain.pantheon.catalog as catalog

    monkeypatch.setattr(catalog, "list_models", _models)


def test_fallback_off_is_single_provider(monkeypatch):
    _enable_fallback(monkeypatch, on=False)
    seen = []

    async def fake(decision, body):
        seen.append(decision.provider)
        raise PantheonGatewayError(500, "down")  # primary fails, no fallback

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    with pytest.raises(PantheonGatewayError):
        asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert seen == ["openai", "openai", "openai"]  # retried same provider, no cross-provider fallover


def test_fallback_on_crosses_to_candidate(monkeypatch):
    _enable_fallback(monkeypatch, on=True)
    seen = []

    async def fake(decision, body):
        seen.append(decision.provider)
        if decision.provider == "openai":
            raise PantheonGatewayError(500, "down")
        return {"model": decision.model_id, "ok": True}

    monkeypatch.setattr(gateway, "_forward_chat_once", fake)
    out = asyncio.run(forward_chat_completion(_decision(), {"messages": []}))
    assert out["ok"] is True
    assert "deepseek" in seen  # fell over to the cross-provider candidate
