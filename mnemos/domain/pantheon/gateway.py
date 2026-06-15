"""Provider forwarding for PANTHEON v0.1."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from mnemos.core.config import get_settings
from mnemos.domain.graeae.api_keys import get_key
from mnemos.domain.graeae.engine import get_graeae_engine
from mnemos.domain.openai_compat.content import _content_text, _flatten_messages_for_prompt
from mnemos.domain.pantheon.router import RouteDecision
from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.fallback import AllDeploymentsFailed
from mnemos.domain.pantheon.cooldown import DEFAULT_TENANT
from mnemos.domain.pantheon.http_bridge import classify, retry_after_seconds
from mnemos.domain.pantheon.runtime import RouterRuntime

logger = logging.getLogger(__name__)
_IDENTITY_BODY_KEY = "_mnemos_upstream_identity"
_REASONING_MODEL_RE = re.compile(r"(reason|thinking|r1\b|\bo[134]\b|gpt-5|grok-4|deepseek)", re.I)
_RESPONSES_MODEL_RE = re.compile(r"(?:^|/)(?:gpt-[0-9.]+.*codex|.*codex.*gpt-[0-9.]|o[134].*-codex|codex)", re.I)
_TOKEN_BUDGET_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_tokens")
_PANTHEON_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "eih": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "api": "openai",
        "key_name": "eih",
        "enabled": True,
    },
    "ngc": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "api": "openai",
        "key_name": "ngc",
        "enabled": True,
    },
    "deepseek-direct": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "api": "openai",
        "key_name": "deepseek-direct",
        "enabled": True,
    },
}


# ── Shared pooled HTTP client. Reused across requests so keep-alive
# connections are recycled instead of paying a fresh TCP+TLS handshake on
# every call — the main source of added latency when fronting providers.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Process-wide pooled httpx client (lazily created, reused)."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(200.0),
            limits=httpx.Limits(
                max_keepalive_connections=50,
                max_connections=200,
                keepalive_expiry=30.0,
            ),
        )
    return _http_client


async def aclose_http_client() -> None:
    """Close the shared client (call on app shutdown)."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


class PantheonGatewayError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class UpstreamIdentity:
    user_id: str
    namespace: str
    session_id: str
    request_id: str

    @property
    def opaque_user(self) -> str:
        digest = hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:16]
        return f"mnemos:{digest}"


def attach_upstream_identity(body: dict[str, Any], identity: UpstreamIdentity) -> dict[str, Any]:
    payload = dict(body)
    payload[_IDENTITY_BODY_KEY] = asdict(identity)
    return payload


def _pop_upstream_identity(payload: dict[str, Any]) -> UpstreamIdentity | None:
    raw = payload.pop(_IDENTITY_BODY_KEY, None)
    if not isinstance(raw, dict):
        return None
    try:
        return UpstreamIdentity(
            user_id=str(raw["user_id"]),
            namespace=str(raw["namespace"]),
            session_id=str(raw["session_id"]),
            request_id=str(raw["request_id"]),
        )
    except KeyError:
        return None


def _identity_headers(identity: UpstreamIdentity | None) -> dict[str, str]:
    if identity is None:
        return {}
    return {
        "X-MNEMOS-User-Id": identity.user_id,
        "X-MNEMOS-Namespace": identity.namespace,
        "X-MNEMOS-Session": identity.session_id,
        "X-MNEMOS-Request-Id": identity.request_id,
    }


def _provider_config(decision: RouteDecision) -> dict[str, Any]:
    engine = get_graeae_engine()
    defaults = _PANTHEON_PROVIDER_DEFAULTS.get(decision.provider, {})
    cfg = {**defaults, **dict(engine.providers.get(decision.provider, {}))}
    if not cfg:
        raise PantheonGatewayError(503, f"provider {decision.provider!r} is not registered")
    if decision.model_id:
        cfg["model"] = decision.model_id
    return cfg


def _auth_headers(cfg: dict[str, Any], identity: UpstreamIdentity | None = None) -> dict[str, str]:
    key_name = cfg.get("key_name")
    api_key = get_key(str(key_name or ""))
    if not api_key:
        raise PantheonGatewayError(503, f"missing api_key for provider key_name={key_name!r}")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if cfg.get("stream") else "application/json",
        **_identity_headers(identity),
    }


def _chat_payload(decision: RouteDecision, body: dict[str, Any], *, stream: bool | None = None) -> dict[str, Any]:
    payload = dict(body)
    identity = _pop_upstream_identity(payload)
    if decision.model_id:
        payload["model"] = decision.model_id
    if stream is not None:
        payload["stream"] = stream
    if identity is not None:
        supplied_user = payload.get("user")
        if supplied_user is not None and supplied_user != identity.opaque_user:
            logger.warning(
                "[PANTHEON] client-supplied OpenAI user field overridden for request_id=%s",
                identity.request_id,
            )
        payload["user"] = identity.opaque_user
    return payload


def model_uses_responses_api(model_id: str | None) -> bool:
    """Return True for Codex/Responses-only OpenAI models.

    gpt-5.x-codex models reject /v1/chat/completions with 400; the gateway
    routes those wire model IDs to /v1/responses instead and converts the OpenAI
    Responses object back into chat-completion shape for downstream clients.
    """
    return bool(model_id and _RESPONSES_MODEL_RE.search(model_id))


def model_needs_reasoning_budget(model_id: str | None) -> bool:
    return bool(model_id and _REASONING_MODEL_RE.search(model_id))


def _base_v1_url(url: str) -> str:
    for suffix in ("/chat/completions", "/responses", "/embeddings"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url.rstrip("/")


def _chat_url(cfg: dict[str, Any], decision: RouteDecision) -> str:
    if model_uses_responses_api(decision.model_id):
        return _responses_url(cfg)
    url = str(cfg.get("chat_url") or cfg.get("url") or "")
    if url.endswith("/responses"):
        return _base_v1_url(url) + "/chat/completions"
    return url


def _responses_url(cfg: dict[str, Any]) -> str:
    if cfg.get("responses_url"):
        return str(cfg["responses_url"])
    return _base_v1_url(str(cfg.get("url") or "")) + "/responses"


def _embeddings_url(cfg: dict[str, Any]) -> str:
    if cfg.get("embeddings_url"):
        return str(cfg["embeddings_url"])
    return _base_v1_url(str(cfg.get("url") or "")) + "/embeddings"


def _reasoning_budget() -> int:
    try:
        configured = int(get_settings().pantheon.reasoning_output_token_budget)
    except Exception:
        configured = 8000
    return max(8000, configured)


def _apply_reasoning_budget(
    payload: dict[str, Any],
    model_id: str | None,
    *,
    budget_field: str = "max_completion_tokens",
) -> dict[str, Any]:
    if not model_needs_reasoning_budget(model_id):
        return payload
    budget = _reasoning_budget()
    raw = None
    for field in _TOKEN_BUDGET_FIELDS:
        if field in payload:
            raw = payload.get(field)
            break
    try:
        current = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        current = None
    if current is None or current < budget:
        payload[budget_field] = budget
        for field in _TOKEN_BUDGET_FIELDS:
            if field != budget_field:
                payload.pop(field, None)
    return payload


def _responses_input_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        # Responses API accepts tool_call outputs as function_call_output items;
        # retain IDs/content without flattening function-call arguments.
        if item.get("role") == "tool":
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": item.get("tool_call_id") or item.get("call_id") or "",
                    "output": item.get("content") if item.get("content") is not None else "",
                }
            )
        else:
            out.append(item)
    return out


def _responses_payload(decision: RouteDecision, body: dict[str, Any], *, stream: bool | None = None) -> dict[str, Any]:
    payload = _chat_payload(decision, body, stream=None)
    messages = payload.pop("messages", [])
    if isinstance(messages, list):
        payload["input"] = _responses_input_from_messages(messages)
    payload.pop("stream", None)
    if stream is not None:
        payload["stream"] = stream
    if "max_output_tokens" in payload:
        payload.pop("max_tokens", None)
        payload.pop("max_completion_tokens", None)
    elif "max_tokens" in payload:
        payload["max_output_tokens"] = payload.pop("max_tokens")
    elif "max_completion_tokens" in payload:
        payload["max_output_tokens"] = payload.pop("max_completion_tokens")
    return _apply_reasoning_budget(payload, decision.model_id, budget_field="max_output_tokens")


def _provider_payload(decision: RouteDecision, body: dict[str, Any], *, stream: bool | None = None) -> dict[str, Any]:
    if model_uses_responses_api(decision.model_id):
        return _responses_payload(decision, body, stream=stream)
    payload = _chat_payload(decision, body, stream=stream)
    return _apply_reasoning_budget(payload, decision.model_id)


def _message_from_responses_output(data: dict[str, Any]) -> dict[str, Any]:
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text") or part.get("output_text")
                if text is not None:
                    content_parts.append(str(text))
        elif item_type in {"function_call", "tool_call"}:
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = item.get("name") or function.get("name")
            arguments = item.get("arguments") if "arguments" in item else function.get("arguments")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, separators=(",", ":"))
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {"name": name or "", "arguments": arguments or ""},
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _responses_to_chat_completion(data: dict[str, Any], decision: RouteDecision) -> dict[str, Any]:
    created = int(data.get("created_at") or time.time())
    message = _message_from_responses_output(data)
    finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    completion_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return {
        "id": data.get("id") or f"chatcmpl-pantheon-{created}",
        "object": "chat.completion",
        "created": created,
        "model": data.get("model") or decision.model_id or decision.alias,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int((prompt_tokens or 0) + (completion_tokens or 0)),
        },
    }


def resolved_wire_model(response: dict[str, Any] | None, decision: RouteDecision) -> str:
    if isinstance(response, dict) and response.get("model"):
        return str(response["model"])
    return str(decision.model_id or decision.alias)


# ── Resilient routing runtime (retry + cooldown over the single provider call) ──
_RUNTIME: RouterRuntime | None = None


def get_runtime() -> RouterRuntime:
    """Process-local RouterRuntime singleton (cooldown breaker + retry/fall-over)."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = RouterRuntime(CooldownManager(InMemoryCooldownStore()), clock=time.time)
    return _RUNTIME


def set_runtime(runtime: RouterRuntime | None) -> None:
    """Override the runtime (tests inject a no-sleep runtime)."""
    global _RUNTIME
    _RUNTIME = runtime


def _is_openai_api(decision: RouteDecision) -> bool:
    try:
        return _provider_config(decision).get("api", "openai") == "openai"
    except Exception:
        return False


async def _forward_chat_once(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    """One provider attempt: POST to the resolved provider, raise on >= 400."""
    cfg = _provider_config(decision)
    client = get_http_client()
    payload = _provider_payload(decision, body, stream=False)
    url = _responses_url(cfg) if model_uses_responses_api(decision.model_id) else _chat_url(cfg, decision)
    response = await client.post(
        url,
        json=payload,
        headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        timeout=cfg.get("timeout", 200),
    )
    if response.status_code >= 400:
        raise PantheonGatewayError(response.status_code, response.text[:500], retry_after_seconds(response))
    data = response.json()
    if model_uses_responses_api(decision.model_id):
        data = _responses_to_chat_completion(data, decision)
    data.setdefault("model", decision.model_id)
    return data


def _decision_cooldown_key(decision: RouteDecision) -> str:
    """Stable cooldown key for a routed provider (avoids hashing RouteDecision)."""
    return f"{decision.provider}:{decision.model_id or decision.alias}"


def _tenant_of(body: dict[str, Any]) -> str:
    """Per-tenant cooldown scope from the upstream identity (or the default)."""
    identity = _pop_upstream_identity(dict(body))
    if identity is None:
        return DEFAULT_TENANT
    return f"{identity.namespace}:{identity.user_id}"


async def forward_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    if decision.route_type == "consensus":
        return await consensus_chat_completion(decision, body)

    cfg = _provider_config(decision)
    if cfg.get("api", "openai") != "openai":
        return await _graeae_chat_completion(decision, body)

    # Behavior-preserving resilience: route the single resolved provider through
    # the runtime so transient failures (5xx / 429 / timeout / connection) are
    # retried with backoff and every outcome feeds the cooldown breaker. A
    # single-element chain keeps routing semantics unchanged; a non-retryable
    # error (e.g. 400) still surfaces as the original PantheonGatewayError.
    runtime = get_runtime()
    chain = [decision]
    if get_settings().pantheon.cross_provider_fallback:
        from mnemos.domain.pantheon import catalog, router

        built = router.build_fallback_chain(decision, await catalog.list_models())
        # only providers served by this openai-compatible path can run in the
        # chain; drop graeae-only providers (anthropic/gemini) — keep primary.
        chain = [d for d in built if _is_openai_api(d)] or [decision]
    try:
        result = await runtime.route(
            chain,
            lambda d: _forward_chat_once(d, body),
            classify=classify,
            tenant=_tenant_of(body),
            key_of=_decision_cooldown_key,
        )
    except AllDeploymentsFailed as exc:
        last = exc.last_exception
        if isinstance(last, PantheonGatewayError):
            raise last
        raise PantheonGatewayError(503, str(last) if last else str(exc)) from exc
    return result.result


async def stream_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    if decision.route_type == "consensus":
        async for event in consensus_chat_completion_stream(decision, body):
            yield event
        return

    cfg = _provider_config(decision)
    if cfg.get("api", "openai") != "openai":
        async for event in _graeae_chat_completion_stream(decision, body):
            yield event
        return

    client = get_http_client()
    payload = _provider_payload(decision, body, stream=True)
    url = _responses_url(cfg) if model_uses_responses_api(decision.model_id) else _chat_url(cfg, decision)
    async with client.stream(
        "POST",
        url,
        json=payload,
        headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        timeout=None,
    ) as response:
        if response.status_code >= 400:
            body_bytes = await response.aread()
            raise PantheonGatewayError(response.status_code, body_bytes[:500].decode("utf-8", "replace"))
        if model_uses_responses_api(decision.model_id):
            stream_id = f"chatcmpl-pantheon-{int(time.time())}"
            created = int(time.time())
            model = decision.model_id or decision.alias
            state: dict[str, Any] = {}
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    yield (line + "\n\n").encode("utf-8")
                    continue
                response_obj = data.get("response") if isinstance(data.get("response"), dict) else None
                if response_obj and response_obj.get("model"):
                    model = str(response_obj["model"])
                for event in _responses_stream_events(data, stream_id=stream_id, created=created, model=model, state=state):
                    yield event
            if not state.get("finished"):
                yield _stream_event(
                    {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
            yield b"data: [DONE]\n\n"
            return
        async for chunk in response.aiter_bytes():
            yield chunk


async def forward_embeddings(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    if decision.route_type == "consensus":
        raise PantheonGatewayError(400, "consensus aliases are not valid for embeddings")
    cfg = _provider_config(decision)
    client = get_http_client()
    payload = _provider_payload(decision, body, stream=None)
    response = await client.post(
        _embeddings_url(cfg),
        json=payload,
        headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        timeout=cfg.get("timeout", 200),
    )
    if response.status_code >= 400:
        raise PantheonGatewayError(response.status_code, response.text[:500])
    data = response.json()
    data.setdefault("model", decision.model_id)
    return data


async def _graeae_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    payload = _provider_payload(decision, body, stream=None)
    messages = payload.get("messages") or body.get("messages") or []
    prompt = _flatten_messages_for_prompt(messages)
    engine = get_graeae_engine()
    result = await engine.route(
        decision.provider,
        decision.model_id or "",
        prompt,
        task_type="reasoning",
        timeout=30,
        generation_params=_generation_params(payload),
        request_params=_request_params(payload),
        messages=messages,
    )
    if result.get("status") != "success":
        raise PantheonGatewayError(503, result.get("error") or "provider unavailable")
    return _openai_chat_response(
        decision.model_id or decision.alias, result.get("choices"), result.get("response_text", ""), messages
    )


async def _graeae_chat_completion_stream(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    response = await _graeae_chat_completion(decision, body)
    yield _stream_event(
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
    )
    content = response["choices"][0]["message"].get("content") or ""
    if content:
        yield _stream_event(
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [{"index": 0, "delta": {"content": content}}],
            }
        )
    yield _stream_event(
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield b"data: [DONE]\n\n"


async def consensus_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    prompt = _flatten_messages_for_prompt(messages)
    engine = get_graeae_engine()
    result = await engine.consult(
        prompt,
        task_type=decision.task_type or "reasoning",
        timeout=body.get("timeout", 180),
        mode="auto",
    )
    content = result.get("consensus_response") or ""
    return _openai_chat_response(decision.alias, None, content, messages)


async def consensus_chat_completion_stream(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    response = await consensus_chat_completion(decision, body)
    yield _stream_event(
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
    )
    content = response["choices"][0]["message"].get("content") or ""
    if content:
        yield _stream_event(
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [{"index": 0, "delta": {"content": content}}],
            }
        )
    yield _stream_event(
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield b"data: [DONE]\n\n"


def _generation_params(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in ("temperature", "max_tokens", "top_p") if body.get(key) is not None}


def _request_params(body: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "stop",
        "n",
        "presence_penalty",
        "frequency_penalty",
        "user",
    )
    return {key: body[key] for key in fields if body.get(key) is not None}


def _openai_chat_response(
    model: str,
    choices: list[dict[str, Any]] | None,
    content: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    created = int(time.time())
    normalized_choices = choices or [
        {
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }
    ]
    prompt_tokens = sum(len(_content_text(message.get("content")).split()) for message in messages)
    completion_tokens = len(content.split())
    return {
        "id": f"chatcmpl-pantheon-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": normalized_choices,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _responses_stream_events(data: dict[str, Any], *, stream_id: str, created: int, model: str, state: dict[str, Any]) -> list[bytes]:
    """Translate Responses API SSE objects into chat.completion.chunk frames."""
    events: list[bytes] = []
    if not state.get("started"):
        state["started"] = True
        events.append(
            _stream_event(
                {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}}],
                }
            )
        )
    event_type = str(data.get("type") or data.get("event") or "")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = data.get("delta")
        if delta is not None:
            events.append(
                _stream_event(
                    {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": str(delta)}}],
                    }
                )
            )
    elif event_type == "response.function_call_arguments.delta":
        call_id = data.get("call_id") or data.get("item_id") or "call_0"
        events.append(
            _stream_event(
                {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": int(data.get("output_index") or 0),
                                        "id": call_id,
                                        "type": "function",
                                        "function": {"arguments": data.get("delta") or ""},
                                    }
                                ]
                            },
                        }
                    ],
                }
            )
        )
    elif event_type == "response.output_item.done":
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if item.get("type") in {"function_call", "tool_call"}:
            arguments = item.get("arguments") or ""
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, separators=(",", ":"))
            events.append(
                _stream_event(
                    {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": int(data.get("output_index") or 0),
                                            "id": item.get("call_id") or item.get("id") or "call_0",
                                            "type": "function",
                                            "function": {"name": item.get("name") or "", "arguments": arguments},
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                )
            )
    elif event_type in {"response.completed", "response.failed", "response.incomplete"}:
        if not state.get("finished"):
            state["finished"] = True
            reason = "stop" if event_type == "response.completed" else "length"
            events.append(
                _stream_event(
                    {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                    }
                )
            )
    return events


def _stream_event(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")
