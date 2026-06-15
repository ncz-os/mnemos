from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemos.api import dependencies
from mnemos.domain.pantheon import gateway
from mnemos.domain.pantheon.gateway import (
    _provider_payload,
    _responses_stream_events,
    _responses_to_chat_completion,
    model_uses_responses_api,
    resolved_wire_model,
)
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.routing_log import routing_payload
from mnemos.domain.pantheon.runtime import RouterRuntime


def _decision(**kw):
    base = dict(alias="a", provider="openai", model_id="gpt-5.3-codex", route_type="single", reason="r")
    base.update(kw)
    return RouteDecision(**base)


def test_shadow_app_serves_models_without_auth_startup(monkeypatch):
    from mnemos.api.pantheon_shadow import app
    from mnemos.core.config import _reset_settings_for_tests
    from mnemos.domain.pantheon import catalog

    async def _models_response():
        return {
            "object": "list",
            "data": [
                {
                    "id": "shadow-model",
                    "object": "model",
                    "provider": "openai",
                    "owned_by": "openai",
                    "capabilities": ["chat"],
                    "usage_tier": "frontier",
                    "health": {"state": "cached"},
                }
            ],
        }

    with monkeypatch.context() as m:
        m.setenv("MNEMOS_PROFILE", "server")
        m.setenv("MNEMOS_PANTHEON_ENABLED", "true")
        m.setattr(dependencies, "PERSONAL_SINGLETON", None)
        m.setattr(dependencies, "_auth_enabled", False)
        m.setattr(catalog, "models_response", _models_response)
        _reset_settings_for_tests()

        with TestClient(app) as client:
            response = client.get("/pantheon/v1/models")

    _reset_settings_for_tests()

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "shadow-model"


def test_shadow_app_serves_openai_chat_without_auth_startup(monkeypatch):
    from mnemos.api.pantheon_shadow import app
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.core.config import _reset_settings_for_tests

    decision = _decision(
        alias="shadow-model",
        provider="openai",
        model_id="shadow-model",
        route_type="literal",
        model={"id": "shadow-model", "usage_tier": "frontier"},
    )

    class _PantheonRouter:
        async def route_model(self, model, body):
            return decision

    class _CapBucket:
        def check_and_increment(self, **_kwargs):
            raise AssertionError("frontier model should not consume consultation cap")

    async def _forward_chat_completion(_decision, body):
        assert body["_mnemos_upstream_identity"]["user_id"] == "default"
        return {
            "id": "chatcmpl-shadow",
            "object": "chat.completion",
            "created": 1,
            "model": "shadow-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def _routing_payload(**_kwargs):
        return {}, {}

    with monkeypatch.context() as m:
        m.setenv("MNEMOS_PROFILE", "server")
        m.setenv("MNEMOS_PANTHEON_ENABLED", "true")
        m.setattr(dependencies, "PERSONAL_SINGLETON", None)
        m.setattr(dependencies, "_auth_enabled", False)
        m.setattr(gateway, "forward_chat_completion", _forward_chat_completion)
        m.setattr(
            pantheon_routes,
            "_pantheon_imports",
            lambda: (
                None,
                gateway,
                _PantheonRouter(),
                Exception,
                _CapBucket(),
                _routing_payload,
                lambda _payload, _metadata: None,
            ),
        )
        _reset_settings_for_tests()

        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "shadow-model", "messages": [{"role": "user", "content": "hi"}]},
            )

    _reset_settings_for_tests()

    assert response.status_code == 200
    assert response.json()["model"] == "shadow-model"


def test_endpoint_routing_by_codex_model_uses_responses_url(monkeypatch):
    posted = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "resp_1", "model": "gpt-5.3-codex", "output": [], "usage": {}}

    class _Client:
        async def post(self, url, **kwargs):
            posted["url"] = url
            posted["json"] = kwargs["json"]
            return _Resp()

    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"url": "https://api.openai.com/v1/chat/completions"})
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})

    data = asyncio.run(gateway._forward_chat_once(_decision(), {"messages": [{"role": "user", "content": "hi"}]}))

    assert posted["url"] == "https://api.openai.com/v1/responses"
    assert "input" in posted["json"] and "messages" not in posted["json"]
    assert data["object"] == "chat.completion"
    assert model_uses_responses_api("gpt-5.3-codex") is True


def test_tool_call_passthrough_payload_and_response_arguments(monkeypatch):
    body = {
        "messages": [
            {"role": "user", "content": "use tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{\"x\":1}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"x\":1}"},
        ],
        "tools": [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}],
    }
    payload = _provider_payload(_decision(model_id="gpt-5.5"), body)
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert payload["messages"][2]["tool_call_id"] == "call_1"
    assert payload["tools"] == body["tools"]

    converted = _responses_to_chat_completion(
        {
            "id": "resp_2",
            "model": "gpt-5.3-codex",
            "output": [
                {"type": "function_call", "call_id": "call_2", "name": "echo", "arguments": {"x": 2}}
            ],
            "usage": {},
        },
        _decision(),
    )
    args = converted["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"x": 2}


def test_responses_stream_tool_call_delta_preserves_arguments():
    state = {}
    events = _responses_stream_events(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "call_3",
            "delta": "{\"x\":3}",
            "output_index": 0,
        },
        stream_id="chatcmpl-x",
        created=1,
        model="gpt-5.3-codex",
        state=state,
    )
    payloads = [json.loads(event.decode().removeprefix("data: ")) for event in events]
    tool_delta = payloads[-1]["choices"][0]["delta"]["tool_calls"][0]
    assert tool_delta["id"] == "call_3"
    assert tool_delta["function"]["arguments"] == '{"x":3}'


def test_reasoning_budget_default_at_least_8000(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(pantheon=SimpleNamespace(reasoning_output_token_budget=4000)),
    )
    payload = _provider_payload(
        _decision(model_id="grok-4.20-0309-reasoning"),
        {"messages": [{"role": "user", "content": "think"}], "max_tokens": 512},
    )
    assert payload["max_completion_tokens"] == 8000
    assert "max_tokens" not in payload


def test_responses_max_output_tokens_floor_applies_to_direct_responses(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(pantheon=SimpleNamespace(reasoning_output_token_budget=4000)),
    )
    payload = _provider_payload(
        _decision(model_id="gpt-5.3-codex"),
        {"messages": [{"role": "user", "content": "think"}], "max_output_tokens": 512},
    )
    assert payload["max_output_tokens"] == 8000
    assert "max_completion_tokens" not in payload
    assert "max_tokens" not in payload


def test_cross_provider_fallback_runs_for_auto_routes(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(pantheon=SimpleNamespace(cross_provider_fallback=True)),
    )
    monkeypatch.setattr(gateway, "_provider_config", lambda d: {"api": "openai", "url": "http://provider.test"})

    async def _models():
        return [
            {"id": "gpt-5.4", "provider": "openai"},
            {"id": "deepseek-v4-flash", "provider": "deepseek-direct"},
        ]

    from mnemos.domain.pantheon import catalog

    monkeypatch.setattr(catalog, "list_models", _models)
    captured: dict[str, list[RouteDecision]] = {}

    class _Runtime:
        async def route(self, chain, call, **kwargs):
            captured["chain"] = list(chain)
            return SimpleNamespace(result={"ok": True})

    monkeypatch.setattr(gateway, "get_runtime", lambda: _Runtime())
    out = asyncio.run(
        gateway.forward_chat_completion(
            _decision(
                alias="auto:code",
                provider="openai",
                model_id="gpt-5.4",
                route_type="auto",
                candidates=["gpt-5.4", "deepseek-v4-flash"],
            ),
            {"messages": [{"role": "user", "content": "hi"}]},
        )
    )

    assert out == {"ok": True}
    assert [(d.provider, d.model_id) for d in captured["chain"]] == [
        ("openai", "gpt-5.4"),
        ("deepseek-direct", "deepseek-v4-flash"),
    ]


def test_upstream_timeout_cools_primary_and_falls_back(monkeypatch):
    from mnemos.domain.pantheon import catalog

    async def _noop_sleep(_seconds):
        return None

    store = InMemoryCooldownStore()
    clock = {"now": 1000.0}
    gateway.set_runtime(
        RouterRuntime(
            CooldownManager(store),
            clock=lambda: clock["now"],
            sleep=_noop_sleep,
            rng=lambda: 0.0,
        )
    )

    async def _models():
        return [
            {"id": "gpt-5.4", "provider": "openai"},
            {"id": "deepseek-v4-flash", "provider": "deepseek-direct"},
        ]

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, model: str):
            self._model = model

        def json(self):
            return {
                "id": "chatcmpl-fallback",
                "model": self._model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }

    seen: list[tuple[str, float]] = []

    class _Client:
        async def post(self, url, **kwargs):
            seen.append((url, kwargs["timeout"]))
            if "openai.test" in url:
                raise httpx.ReadTimeout("slow upstream")
            return _Resp(kwargs["json"]["model"])

    try:
        monkeypatch.setattr(
            gateway,
            "get_settings",
            lambda: SimpleNamespace(
                pantheon=SimpleNamespace(
                    cross_provider_fallback=True,
                    upstream_timeout_seconds=0.05,
                )
            ),
        )
        monkeypatch.setattr(
            gateway,
            "_provider_config",
            lambda d: {
                "api": "openai",
                "url": f"http://{d.provider}.test/v1/chat/completions",
            },
        )
        monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})
        monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
        monkeypatch.setattr(catalog, "list_models", _models)

        out = asyncio.run(
            gateway.forward_chat_completion(
                _decision(
                    alias="auto:code",
                    provider="openai",
                    model_id="gpt-5.4",
                    route_type="auto",
                    candidates=["gpt-5.4", "deepseek-v4-flash"],
                ),
                {"messages": [{"role": "user", "content": "hi"}]},
            )
        )
    finally:
        gateway.set_runtime(None)

    assert out["model"] == "deepseek-v4-flash"
    assert [url for url, _timeout in seen] == [
        "http://openai.test/v1/chat/completions",
        "http://deepseek-direct.test/v1/chat/completions",
    ]
    assert seen[0][1] == 0.05
    assert store.get_cooled_until("_default", "openai:gpt-5.4") == 1005.0


def test_eih_and_deepseek_direct_defaults_forward(monkeypatch):
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    posted: list[tuple[str, dict]] = []

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, model: str):
            self._model = model

        def json(self):
            return {
                "id": "chatcmpl-provider",
                "model": self._model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }

    class _Client:
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _Resp(kwargs["json"]["model"])

    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})

    eih = asyncio.run(
        gateway._forward_chat_once(
            _decision(provider="eih", model_id="nvidia/llama-3.3-70b-instruct", route_type="literal"),
            {"messages": [{"role": "user", "content": "hi"}]},
        )
    )
    deepseek = asyncio.run(
        gateway._forward_chat_once(
            _decision(provider="deepseek-direct", model_id="deepseek-v4-flash", route_type="literal"),
            {"messages": [{"role": "user", "content": "hi"}]},
        )
    )

    assert eih["model"] == "nvidia/llama-3.3-70b-instruct"
    assert deepseek["model"] == "deepseek-v4-flash"
    assert posted[0][0] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert posted[0][1]["model"] == "nvidia/llama-3.3-70b-instruct"
    assert posted[1][0] == "https://api.deepseek.com/v1/chat/completions"
    assert posted[1][1]["model"] == "deepseek-v4-flash"


def test_model_label_correctness_uses_response_wire_model():
    dec = _decision(model_id="gpt-5.3-codex")
    response = {"model": "gpt-5.3-codex-2026-06-01", "usage": {}}
    assert resolved_wire_model(response, dec) == "gpt-5.3-codex-2026-06-01"
    payload, metadata = routing_payload(
        request_id="r",
        tenant_user_id="u",
        session_id="s",
        decision=dec,
        outcome="success",
        latency_ms=1,
        response=response,
        resolved_wire_model=resolved_wire_model(response, dec),
    )
    assert payload["resolved_to"] == "gpt-5.3-codex-2026-06-01"
    assert metadata["resolved_to"] == "gpt-5.3-codex-2026-06-01"


def test_streaming_telemetry_logs_after_stream_with_real_wire_model(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes

    decision = _decision(alias="auto:cheap", provider="openai", model_id="cheap-chat", route_type="auto")
    logs: list[dict] = []
    scheduled: list[tuple[dict, dict]] = []

    class _PantheonRouter:
        async def route_model(self, model, body):
            return decision

    async def _stream(_decision, _body):
        yield (
            b'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,'
            b'"model":"cheap-chat-2026-06-14","choices":[]}\n\n'
        )
        yield b"data: [DONE]\n\n"

    def _routing_payload(**kwargs):
        logs.append(kwargs)
        resolved = kwargs.get("resolved_wire_model")
        return {"resolved_to": resolved}, {"resolved_to": resolved}

    def _schedule(payload, metadata):
        scheduled.append((payload, metadata))

    monkeypatch.setattr(gateway, "stream_chat_completion", _stream)
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            None,
            gateway,
            _PantheonRouter(),
            Exception,
            SimpleNamespace(),
            _routing_payload,
            _schedule,
        ),
    )

    request = SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test"))
    user = SimpleNamespace(user_id="u1", namespace="ns1")
    response = asyncio.run(
        pantheon_routes._chat_completions_impl(
            request,
            {"model": "auto:cheap", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            user,
        )
    )
    assert logs == []

    async def _consume():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_consume())
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert logs[0]["outcome"] == "success"
    assert logs[0]["resolved_wire_model"] == "cheap-chat-2026-06-14"
    assert scheduled[0][0]["resolved_to"] == "cheap-chat-2026-06-14"


def test_shadow_smoke_tool_call_check_is_assertive():
    from scripts.pantheon_shadow_smoke import _assert_echo_tool_call, _client_timeout

    with pytest.raises(AssertionError, match="expected at least one tool_call"):
        _assert_echo_tool_call({"choices": [{"message": {"content": "no tool"}}]})

    response_data = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"x":1}'},
                        }
                    ]
                }
            }
        ]
    }
    calls = _assert_echo_tool_call(response_data)
    assert calls[0]["function"]["arguments"] == '{"x":1}'

    response_data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"x":2}'
    with pytest.raises(AssertionError, match="expected echo arguments"):
        _assert_echo_tool_call(response_data)

    assert _client_timeout().read >= 90.0
