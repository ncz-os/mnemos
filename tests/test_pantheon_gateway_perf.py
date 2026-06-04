"""Tests for the gateway shared/pooled HTTP client (connection-reuse perf fix)."""

from __future__ import annotations

import asyncio

import httpx

from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.router import RouteDecision


def test_get_http_client_is_pooled_singleton():
    gateway._http_client = None  # noqa: SLF001 — reset for a clean assertion
    c1 = gateway.get_http_client()
    c2 = gateway.get_http_client()
    assert c1 is c2  # reused, not recreated per call
    assert isinstance(c1, httpx.AsyncClient)
    assert c1.is_closed is False
    asyncio.run(gateway.aclose_http_client())
    assert gateway._http_client is None  # noqa: SLF001


def test_aclose_then_recreate():
    gateway._http_client = None  # noqa: SLF001
    c1 = gateway.get_http_client()
    asyncio.run(gateway.aclose_http_client())
    c2 = gateway.get_http_client()
    assert c1 is not c2  # a closed client is replaced
    asyncio.run(gateway.aclose_http_client())


class _FakeResp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self):
        self.post_calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "timeout": timeout})
        return _FakeResp(200, {"model": "m", "ok": True})


def test_forward_chat_once_uses_shared_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(gateway, "get_http_client", lambda: fake)
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"url": "http://x", "timeout": 42})
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})

    dec = RouteDecision(alias="a", provider="p", model_id="m", route_type="single", reason="r")
    out1 = asyncio.run(gateway._forward_chat_once(dec, {"messages": []}))  # noqa: SLF001
    out2 = asyncio.run(gateway._forward_chat_once(dec, {"messages": []}))  # noqa: SLF001

    assert out1["ok"] is True and out2["ok"] is True
    # both calls went through the SAME pooled client (no per-request client)
    assert len(fake.post_calls) == 2
    # per-request timeout from cfg is honored on the shared client
    assert fake.post_calls[0]["timeout"] == 42
