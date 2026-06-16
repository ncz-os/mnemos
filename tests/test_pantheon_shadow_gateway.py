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


def _passthrough_router_settings(*, enabled: bool = True, provider: str = "nvidia") -> SimpleNamespace:
    return SimpleNamespace(
        pantheon=SimpleNamespace(
            default_quality_floor=0.0,
            default_max_cost_usd_per_mtok=None,
            routing_window_minutes=15,
            passthrough_enabled=enabled,
            passthrough_provider=provider,
            passthrough_default_input_cost_per_mtok=5.0,
            passthrough_default_output_cost_per_mtok=30.0,
            passthrough_default_estimated_output_tokens=4096,
        )
    )


def _passthrough_gateway_settings() -> SimpleNamespace:
    return SimpleNamespace(
        pantheon=SimpleNamespace(
            cross_provider_fallback=False,
            upstream_timeout_seconds=60.0,
            reasoning_output_token_budget=8000,
        )
    )


class _NoopCapBucket:
    def check_and_increment(self, **_kwargs):
        raise AssertionError("pass-through models should not consume consultation cap")


def test_passthrough_pricing_backfills_paid_zero_cache_but_keeps_free_tier_zero(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.pantheon import router

    monkeypatch.setattr("mnemos.core.config.GRAEAE_CONFIG", {"providers": {}})
    monkeypatch.setattr("mnemos.domain.graeae.api_keys.get_provider_config", lambda _provider: {})

    paid_settings = _passthrough_router_settings(provider="paid-provider").pantheon
    paid_model = router._passthrough_model(
        "paidvendor/zero-priced-model",
        "paid-provider",
        paid_settings,
        [
            {
                "id": "cached-pricing-row",
                "provider": "paid-provider",
                "registry_provider": "paid-provider",
                "pricing_raw_model_id": "paidvendor/zero-priced-model",
                "input_cost_per_mtok": 0.0,
                "output_cost_per_mtok": 0.0,
                "price_in": 0.0,
                "price_out": 0.0,
                "cost_per_mtok": 0.0,
                "usage_tier": "frontier",
            }
        ],
    )
    paid_decision = _decision(
        provider="paid-provider",
        model_id="paidvendor/zero-priced-model",
        route_type="passthrough",
        model=paid_model,
    )

    assert paid_model["input_cost_per_mtok"] == 5.0
    assert paid_model["output_cost_per_mtok"] == 30.0
    assert paid_model["cost_per_mtok"] == 17.5
    assert (
        pantheon_routes._estimate_cost_usd(
            paid_decision,
            {
                "messages": [{"role": "user", "content": "hi"}],
                "estimated_cost_usd": 0,
            },
        )
        > 0
    )

    monkeypatch.setattr(
        "mnemos.domain.graeae.api_keys.get_provider_config",
        lambda provider: {"tier": "free"} if provider == "custom-free" else {},
    )
    configured_free_model = router._passthrough_model(
        "custom/free-model",
        "custom-free",
        _passthrough_router_settings(provider="custom-free").pantheon,
        [],
    )
    assert configured_free_model["input_cost_per_mtok"] == 0.0
    assert configured_free_model["output_cost_per_mtok"] == 0.0
    assert configured_free_model["cost_per_mtok"] == 0.0

    free_settings = _passthrough_router_settings(provider="nvidia").pantheon
    free_model = router._passthrough_model(
        "nvcf/meta/llama-3.3-70b-instruct",
        "nvidia",
        free_settings,
        [],
    )
    free_decision = _decision(
        provider="nvidia",
        model_id="nvcf/meta/llama-3.3-70b-instruct",
        route_type="passthrough",
        model=free_model,
    )

    assert free_model["input_cost_per_mtok"] == 0.0
    assert free_model["output_cost_per_mtok"] == 0.0
    assert free_model["cost_per_mtok"] == 0.0
    assert (
        pantheon_routes._estimate_cost_usd(
            free_decision,
            {
                "messages": [{"role": "user", "content": "hi"}],
                "estimated_cost_usd": 999,
            },
        )
        == 0.0
    )


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


def test_shadow_app_openai_responses_dispatches_codex_model(monkeypatch):
    from mnemos.api.pantheon_shadow import app
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.core.config import _reset_settings_for_tests

    decision = _decision(
        alias="gpt-5.3-codex",
        provider="openai",
        model_id="gpt-5.3-codex",
        route_type="literal",
        model={"id": "gpt-5.3-codex", "usage_tier": "frontier"},
    )
    seen = {}

    class _PantheonRouter:
        async def route_model(self, model, body):
            seen["routed_model"] = model
            seen["route_body"] = body
            return decision

    class _CapBucket:
        def check_and_increment(self, **_kwargs):
            raise AssertionError("frontier model should not consume consultation cap")

    async def _forward_chat_completion(actual_decision, body):
        seen["forward_decision"] = actual_decision
        seen["forward_body"] = body
        assert actual_decision.model_id == "gpt-5.3-codex"
        assert body["messages"] == [{"role": "user", "content": "Return the word ok."}]
        assert body["_mnemos_upstream_identity"]["user_id"] == "default"
        return {
            "id": "chatcmpl-codex",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-5.3-codex",
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
                "/v1/responses",
                json={"model": "gpt-5.3-codex", "input": "Return the word ok."},
            )

    _reset_settings_for_tests()

    assert response.status_code != 404, response.text
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["object"] == "response"
    assert data["model"] == "gpt-5.3-codex"
    assert data["output"][0]["content"][0]["text"] == "ok"
    assert seen["routed_model"] == "gpt-5.3-codex"
    assert seen["forward_decision"].model_id == "gpt-5.3-codex"


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


def test_endpoint_helpers_append_suffix_to_base_urls():
    decision = _decision(model_id="openai/openai/gpt-5.5")

    assert (
        gateway._chat_url({"url": "https://inference-api.nvidia.com/v1"}, decision)
        == "https://inference-api.nvidia.com/v1/chat/completions"
    )
    assert (
        gateway._chat_url({"url": "https://inference-api.nvidia.com/v1/chat/completions"}, decision)
        == "https://inference-api.nvidia.com/v1/chat/completions"
    )
    assert (
        gateway._responses_url({"url": "https://inference-api.nvidia.com/v1/chat/completions"})
        == "https://inference-api.nvidia.com/v1/responses"
    )
    assert (
        gateway._embeddings_url({"url": "https://inference-api.nvidia.com/v1"})
        == "https://inference-api.nvidia.com/v1/embeddings"
    )


def test_passthrough_provider_prefers_operator_base_url_over_engine_url(monkeypatch):
    posted: list[tuple[str, dict]] = []

    class _Engine:
        providers = {
            "nvidia": {
                "url": "https://integrate.api.nvidia.com/v1/chat/completions",
                "model": "moonshotai/kimi-k2.6",
                "weight": 0.80,
                "api": "openai",
                "key_name": "nvidia",
            }
        }

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "id": "chatcmpl-pass",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/openai/gpt-5.5",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }

    class _Client:
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _Resp()

    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: _Engine())
    monkeypatch.setattr(
        gateway,
        "get_provider_config",
        lambda provider: {"base_url": "https://inference-api.nvidia.com/v1"} if provider == "nvidia" else {},
    )
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})

    asyncio.run(
        gateway._forward_chat_once(
            _decision(provider="nvidia", model_id="openai/openai/gpt-5.5", route_type="passthrough"),
            {"messages": [{"role": "user", "content": "hi"}]},
        )
    )

    assert posted[0][0] == "https://inference-api.nvidia.com/v1/chat/completions"
    assert "integrate.api.nvidia.com" not in posted[0][0]
    assert posted[0][1]["model"] == "openai/openai/gpt-5.5"


def test_codex_fleet_model_is_cataloged_and_resolvable(monkeypatch):
    from mnemos.domain.pantheon import catalog, pricing, router

    class _Engine:
        providers = {
            "openai": {
                "url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-5.5",
                "weight": 0.88,
                "api": "openai",
                "key_name": "openai",
            }
        }

        def provider_status(self):
            return {"circuit_breakers": {"openai": {"state": "closed"}}}

    monkeypatch.setattr(catalog, "get_graeae_engine", lambda: _Engine())
    monkeypatch.setattr(catalog._lc, "_pool", None)
    monkeypatch.setattr(pricing, "read_json_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        router,
        "get_settings",
        lambda: SimpleNamespace(
            pantheon=SimpleNamespace(
                default_quality_floor=0.0,
                default_max_cost_usd_per_mtok=None,
                routing_window_minutes=15,
            )
        ),
    )

    models = asyncio.run(catalog.list_models())
    decision = asyncio.run(router.route_model("gpt-5.3-codex", {"messages": []}))

    assert "gpt-5.3-codex" in {model["id"] for model in models}
    assert decision.provider == "openai"
    assert decision.model_id == "gpt-5.3-codex"
    assert model_uses_responses_api(decision.model_id) is True


def test_codex_fleet_model_skipped_without_registered_openai_provider(monkeypatch):
    from mnemos.domain.pantheon import catalog, pricing

    class _Engine:
        providers = {
            "ngc": {
                "url": "https://integrate.api.nvidia.com/v1/chat/completions",
                "model": "nvidia/llama-3.3-70b-instruct",
                "weight": 0.72,
                "api": "openai",
                "key_name": "ngc",
            }
        }

        def provider_status(self):
            return {}

    cached = {
        "schema": "mnemos.pantheon.catalog.v1",
        "models": [
            {
                "id": "gpt-5.3-codex",
                "provider": "openai",
                "registry_provider": "openai",
                "display_name": "GPT-5.3 Codex",
            }
        ],
    }

    monkeypatch.setattr(catalog, "get_graeae_engine", lambda: _Engine())
    monkeypatch.setattr(catalog._lc, "_pool", None)
    monkeypatch.setattr(pricing, "read_json_cache", lambda *_args, **_kwargs: cached)

    models = asyncio.run(catalog.list_models())
    ids = {model["id"] for model in models}

    assert "nvidia/llama-3.3-70b-instruct" in ids
    assert "gpt-5.3-codex" not in ids


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

    monkeypatch.setattr(gateway, "get_provider_config", lambda _provider: {})
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


def test_unknown_explicit_model_routes_through_budget_cooldown_and_audit(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.pantheon import catalog, router

    explicit_model = "nvcf/meta/llama-3.3-70b-instruct"
    budget_calls: list[dict] = []
    scheduled: list[tuple[dict, dict]] = []
    posted: list[tuple[str, dict]] = []
    store = InMemoryCooldownStore()
    runtime = RouterRuntime(CooldownManager(store), clock=lambda: 1000.0, sleep=lambda _seconds: asyncio.sleep(0))

    async def _models():
        return [
            {
                "id": "cheap-chat",
                "provider": "openai",
                "registry_provider": "openai",
                "available": True,
                "cost_per_mtok": 0.1,
            }
        ]

    class _Budget:
        async def evaluate_budget(self, **kwargs):
            budget_calls.append(kwargs)
            return SimpleNamespace(allowed=True)

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, model: str):
            self._model = model

        def json(self):
            return {
                "id": "chatcmpl-pass",
                "object": "chat.completion",
                "created": 1,
                "model": self._model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }

    class _Client:
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _Resp(kwargs["json"]["model"])

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings())
    monkeypatch.setattr(gateway, "get_settings", _passthrough_gateway_settings)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})
    gateway.set_runtime(runtime)
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            catalog,
            gateway,
            router,
            router.PantheonRoutingError,
            _NoopCapBucket(),
            routing_payload,
            lambda payload, metadata: scheduled.append((payload, metadata)),
            _Budget(),
        ),
    )

    try:
        response = asyncio.run(
            pantheon_routes._chat_completions_impl(
                SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test")),
                {"model": explicit_model, "messages": [{"role": "user", "content": "hi"}]},
                SimpleNamespace(user_id="u1", namespace="ns1"),
            )
        )
    finally:
        gateway.set_runtime(None)

    assert response.status_code == 200
    assert budget_calls and budget_calls[0]["caller_subsystem"] == "pantheon"
    assert posted[0][0] == "https://inference-api.nvidia.com/v1/chat/completions"
    assert posted[0][1]["model"] == explicit_model
    assert posted[0][1]["messages"] == [{"role": "user", "content": "hi"}]
    assert posted[0][1]["user"] == "mnemos:bb82030dbc2bcaba"
    assert store.get_counts("ns1:u1", f"nvidia:{explicit_model}", 16) == (1, 0)
    assert scheduled[0][0]["alias_or_model"] == explicit_model
    assert scheduled[0][0]["resolved_to"] == explicit_model
    assert scheduled[0][0]["outcome"] == "success"
    assert scheduled[0][0]["tokens_in"] == 3
    assert scheduled[0][0]["tokens_out"] == 2


def test_unknown_explicit_embedding_model_uses_runtime_and_audit(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.pantheon import catalog, router

    explicit_model = "nvcf/nvidia/nv-embedqa-e5-v5"
    budget_calls: list[dict] = []
    scheduled: list[tuple[dict, dict]] = []
    posted: list[tuple[str, dict]] = []
    store = InMemoryCooldownStore()

    async def _models():
        return []

    class _Budget:
        async def evaluate_budget(self, **kwargs):
            budget_calls.append(kwargs)
            return SimpleNamespace(allowed=True)

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "object": "list",
                "model": explicit_model,
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }

    class _Client:
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _Resp()

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings())
    monkeypatch.setattr(gateway, "get_settings", _passthrough_gateway_settings)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})
    gateway.set_runtime(RouterRuntime(CooldownManager(store), clock=lambda: 1000.0))
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            catalog,
            gateway,
            router,
            router.PantheonRoutingError,
            _NoopCapBucket(),
            routing_payload,
            lambda payload, metadata: scheduled.append((payload, metadata)),
            _Budget(),
        ),
    )

    try:
        endpoint = pantheon_routes.embeddings.__wrapped__
        response = asyncio.run(
            endpoint(
                SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test")),
                {"model": explicit_model, "input": "hello"},
                None,
                SimpleNamespace(user_id="u1", namespace="ns1"),
            )
        )
    finally:
        gateway.set_runtime(None)

    assert response.status_code == 200
    assert budget_calls
    assert posted[0][0] == "https://inference-api.nvidia.com/v1/embeddings"
    assert posted[0][1]["model"] == explicit_model
    assert posted[0][1]["input"] == "hello"
    assert store.get_counts("ns1:u1", f"nvidia:{explicit_model}", 16) == (1, 0)
    assert scheduled[0][0]["alias_or_model"] == explicit_model
    assert scheduled[0][0]["resolved_to"] == explicit_model
    assert scheduled[0][0]["outcome"] == "success"


def test_paid_unknown_explicit_model_over_cap_denies_before_upstream(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.knemon.budget import BudgetVerdict
    from mnemos.domain.pantheon import catalog, router

    explicit_model = "paidvendor/expensive-model"
    budget_calls: list[dict] = []
    scheduled: list[tuple[dict, dict]] = []
    spent_usd = 199.99
    limit_usd = 200.0

    async def _models():
        return []

    class _Budget:
        async def evaluate_budget(self, **kwargs):
            budget_calls.append(kwargs)
            estimated = float(kwargs["estimated_cost_usd"])
            if spent_usd + estimated <= limit_usd:
                return SimpleNamespace(allowed=True)
            return SimpleNamespace(
                allowed=False,
                verdict=BudgetVerdict.DENY,
                reason=f"estimated ${estimated:.4f} would exceed remaining ${limit_usd - spent_usd:.4f}",
                remaining_usd=limit_usd - spent_usd,
                limit_usd=limit_usd,
                spent_usd=spent_usd,
            )

    class _Client:
        async def post(self, *_args, **_kwargs):
            raise AssertionError("budget denial must happen before upstream dispatch")

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings(provider="paid-provider"))
    monkeypatch.setattr(gateway, "get_settings", _passthrough_gateway_settings)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            catalog,
            gateway,
            router,
            router.PantheonRoutingError,
            _NoopCapBucket(),
            routing_payload,
            lambda payload, metadata: scheduled.append((payload, metadata)),
            _Budget(),
        ),
    )

    response = asyncio.run(
        pantheon_routes._chat_completions_impl(
            SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test")),
            {
                "model": explicit_model,
                "messages": [{"role": "user", "content": "hi"}],
                "estimated_cost_usd": 0,
            },
            SimpleNamespace(user_id="u1", namespace="ns1"),
        )
    )

    assert response.status_code == 402
    assert json.loads(response.body.decode("utf-8"))["error"]["type"] == "pantheon_budget_exceeded"
    assert budget_calls
    assert budget_calls[0]["estimated_cost_usd"] > limit_usd - spent_usd
    assert scheduled
    payload, metadata = scheduled[0]
    assert payload["outcome"] == "budget_denied"
    assert payload["alias_or_model"] == explicit_model
    assert payload["resolved_to"] == explicit_model
    assert payload["cost_usd"] == budget_calls[0]["estimated_cost_usd"]
    assert metadata["estimated_cost_usd"] == budget_calls[0]["estimated_cost_usd"]


def test_free_nvidia_passthrough_zero_estimate_is_not_pre_denied(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.knemon.budget import BudgetVerdict
    from mnemos.domain.pantheon import catalog, router

    explicit_model = "nvcf/meta/llama-3.3-70b-instruct"
    budget_calls: list[dict] = []
    posted: list[tuple[str, dict]] = []
    spent_usd = 199.99
    limit_usd = 200.0

    async def _models():
        return []

    class _Budget:
        async def evaluate_budget(self, **kwargs):
            budget_calls.append(kwargs)
            estimated = float(kwargs["estimated_cost_usd"])
            if spent_usd + estimated <= limit_usd:
                return SimpleNamespace(allowed=True)
            return SimpleNamespace(
                allowed=False,
                verdict=BudgetVerdict.DENY,
                reason=f"estimated ${estimated:.4f} would exceed remaining ${limit_usd - spent_usd:.4f}",
                remaining_usd=limit_usd - spent_usd,
                limit_usd=limit_usd,
                spent_usd=spent_usd,
            )

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "id": "chatcmpl-free-pass",
                "object": "chat.completion",
                "created": 1,
                "model": explicit_model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }

    class _Client:
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _Resp()

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings(provider="nvidia"))
    monkeypatch.setattr(gateway, "get_settings", _passthrough_gateway_settings)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            catalog,
            gateway,
            router,
            router.PantheonRoutingError,
            _NoopCapBucket(),
            routing_payload,
            lambda _payload, _metadata: None,
            _Budget(),
        ),
    )

    response = asyncio.run(
        pantheon_routes._chat_completions_impl(
            SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test")),
            {
                "model": explicit_model,
                "messages": [{"role": "user", "content": "hi"}],
                "estimated_cost_usd": 999,
            },
            SimpleNamespace(user_id="u1", namespace="ns1"),
        )
    )

    assert response.status_code == 200
    assert budget_calls[0]["estimated_cost_usd"] == 0.0
    assert posted[0][0] == "https://inference-api.nvidia.com/v1/chat/completions"
    assert posted[0][1]["model"] == explicit_model


def test_unknown_explicit_model_streaming_passthrough(monkeypatch):
    from mnemos.api.routes import pantheon as pantheon_routes
    from mnemos.domain.pantheon import catalog, router

    explicit_model = "nvcf/meta/llama-3.3-70b-instruct"
    posted: list[tuple[str, dict]] = []
    scheduled: list[tuple[dict, dict]] = []

    async def _models():
        return []

    class _Budget:
        async def evaluate_budget(self, **_kwargs):
            return SimpleNamespace(allowed=True)

    class _StreamResp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_bytes(self):
            yield (
                b'data: {"id":"chatcmpl-pass","object":"chat.completion.chunk","created":1,'
                + f'"model":"{explicit_model}","choices":[{{"index":0,"delta":{{"content":"ok"}}}}]}}\n\n'.encode()
            )
            yield (
                b'data: {"id":"chatcmpl-pass","object":"chat.completion.chunk","created":1,'
                + f'"model":"{explicit_model}","choices":[],"usage":'.encode()
                + b'{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n'
            )
            yield b"data: [DONE]\n\n"

    class _StreamManager:
        async def __aenter__(self):
            return _StreamResp()

        async def __aexit__(self, *_args):
            return None

    class _Client:
        def stream(self, _method, url, **kwargs):
            posted.append((url, kwargs["json"]))
            return _StreamManager()

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings())
    monkeypatch.setattr(gateway, "get_settings", _passthrough_gateway_settings)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: SimpleNamespace(providers={}))
    monkeypatch.setattr(gateway, "get_http_client", lambda: _Client())
    monkeypatch.setattr(gateway, "_auth_headers", lambda cfg, identity=None: {})
    gateway.set_runtime(RouterRuntime(CooldownManager(InMemoryCooldownStore()), clock=lambda: 1000.0))
    monkeypatch.setattr(
        pantheon_routes,
        "_pantheon_imports",
        lambda: (
            catalog,
            gateway,
            router,
            router.PantheonRoutingError,
            _NoopCapBucket(),
            routing_payload,
            lambda payload, metadata: scheduled.append((payload, metadata)),
            _Budget(),
        ),
    )

    try:
        response = asyncio.run(
            pantheon_routes._chat_completions_impl(
                SimpleNamespace(headers={}, query_params={}, state=SimpleNamespace(), client=SimpleNamespace(host="test")),
                {"model": explicit_model, "messages": [{"role": "user", "content": "hi"}], "stream": True},
                SimpleNamespace(user_id="u1", namespace="ns1"),
            )
        )

        async def _consume():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_consume())
    finally:
        gateway.set_runtime(None)

    assert chunks[-1] == b"data: [DONE]\n\n"
    assert posted[0][0] == "https://inference-api.nvidia.com/v1/chat/completions"
    assert posted[0][1]["model"] == explicit_model
    assert posted[0][1]["stream_options"] == {"include_usage": True}
    assert scheduled[0][0]["resolved_to"] == explicit_model
    assert scheduled[0][0]["outcome"] == "success"
    assert scheduled[0][0]["tokens_in"] == 11
    assert scheduled[0][0]["tokens_out"] == 7
    assert scheduled[0][0]["cost_usd"] == 0.0


def test_auto_cheap_still_uses_policy_selection(monkeypatch):
    from mnemos.domain.pantheon import catalog, router

    calls: list[tuple[str, list[str]]] = []
    cheap = {
        "id": "cheap-chat",
        "provider": "openai",
        "available": True,
        "deprecated": False,
        "cost_per_mtok": 0.1,
        "quality_score": 0.1,
    }

    async def _models():
        return [cheap]

    async def _resolve_with_policy(_pool, alias, candidates, *, window_minutes):
        calls.append((alias, [candidate["id"] for candidate in candidates]))
        return SimpleNamespace(
            selected=cheap,
            candidates=["cheap-chat"],
            scores={"cheap-chat": {"total": 1.0}},
            selection_reason=f"policy window {window_minutes}",
        )

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings())
    monkeypatch.setattr(router, "resolve_with_policy", _resolve_with_policy)

    decision = asyncio.run(router.route_model("auto:cheap", {"messages": [{"role": "user", "content": "hi"}]}))

    assert calls == [("auto:cheap", ["cheap-chat"])]
    assert decision.route_type == "auto"
    assert decision.model_id == "cheap-chat"
    assert decision.selection_reason == "policy window 15"


def test_auto_alias_selection_failure_does_not_passthrough(monkeypatch):
    from mnemos.domain.pantheon import catalog, router

    async def _models():
        return []

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings())

    with pytest.raises(router.PantheonRoutingError) as exc:
        asyncio.run(router.route_model("auto:cheap", {"messages": [{"role": "user", "content": "hi"}]}))

    assert exc.value.status_code == 404
    assert "cost ceiling" in exc.value.message


def test_passthrough_disabled_preserves_unknown_model_404(monkeypatch):
    from mnemos.domain.pantheon import catalog, router

    async def _models():
        return []

    monkeypatch.setattr(catalog, "list_models", _models)
    monkeypatch.setattr(router, "get_settings", lambda: _passthrough_router_settings(enabled=False))

    with pytest.raises(router.PantheonRoutingError) as exc:
        asyncio.run(router.route_model("nvcf/meta/llama-3.3-70b-instruct", {}))

    assert exc.value.status_code == 404


def test_shadow_smoke_tool_call_check_is_assertive():
    from scripts.pantheon_shadow_smoke import _assert_echo_tool_call, _client_timeout, _codex_host_skip_reason

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
    assert _codex_host_skip_reason(503, '{"detail":"provider \\"openai\\" is not registered"}') == (
        "provider_not_registered"
    )
    assert _codex_host_skip_reason(503, '{"detail":"missing api_key for provider key_name=\\"openai\\""}') == (
        "missing_api_key"
    )
    assert _codex_host_skip_reason(404, '{"detail":"not found"}') is None
