"""PANTHEON OpenAI-compatible facade routes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core.config import get_settings
from mnemos.core.services import service_enabled
from mnemos.core.extras import is_extra_installed, missing_extra_detail
from mnemos.core.plan_windows import compute_plan_window_id
from mnemos.core.rate_limit import limiter
from mnemos.persistence.base import UsageLedgerRecord

router = APIRouter(prefix="/pantheon/v1", tags=["pantheon"])
openai_router = APIRouter(prefix="/v1", tags=["pantheon-openai-shadow"])
logger = logging.getLogger(__name__)


async def _pantheon_user(
    request: Request,
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    request.state.mnemos_pantheon_user_id = user.user_id
    return user


def _pantheon_gateway_rate_limit() -> str:
    return get_settings().rate_limit.pantheon_gateway


def _pantheon_rate_key(request: Request) -> str:
    user_id = getattr(request.state, "mnemos_pantheon_user_id", None)
    session_id = (
        request.headers.get("x-pantheon-session")
        or request.headers.get("x-mnemos-session-id")
        or request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "default"
    )
    if user_id:
        return f"pantheon:{user_id}:{session_id}"
    client = request.client.host if request.client else "unknown"
    return f"pantheon:{client}:{session_id}"


def _require_enabled() -> None:
    if not is_extra_installed("pantheon"):
        raise HTTPException(
            status_code=503,
            detail=missing_extra_detail("pantheon", label="PANTHEON"),
        )
    settings = get_settings()
    if not service_enabled(settings, "pantheon"):
        raise HTTPException(status_code=503, detail="PANTHEON disabled in this profile")


def _pantheon_imports() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    _require_enabled()
    from mnemos.domain.pantheon import budget, catalog, gateway, router as pantheon_router
    from mnemos.domain.pantheon.aliases import PantheonRoutingError
    from mnemos.domain.pantheon.caps import consultation_cap_bucket
    from mnemos.domain.pantheon.routing_log import routing_payload, schedule_routing_audit

    return (
        catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        consultation_cap_bucket,
        routing_payload,
        schedule_routing_audit,
        budget,
    )


def _body_model(body: dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model is required")
    return model.strip()


def _to_http_exception(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status_code", 500),
        detail=getattr(exc, "message", str(exc)),
    )


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _collect_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_collect_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_collect_text(item) for item in value)
    return ""


def _approx_text_tokens(value: Any) -> int:
    text = _collect_text(value)
    return max(0, int((len(text) + 3) / 4))


def _approx_prompt_tokens(body: dict[str, Any]) -> int:
    if isinstance(body.get("messages"), list):
        return _approx_text_tokens(body["messages"])
    if "input" in body:
        return _approx_text_tokens(body.get("input"))
    return 0


def _pantheon_session_id(request: Request, user: UserContext) -> str:
    return str(
        getattr(user, "session_id", None)
        or request.headers.get("x-pantheon-session")
        or getattr(request.state, "mnemos_session_id", None)
        or request.headers.get("x-mnemos-session-id")
        or request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "default"
    )


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _upstream_identity(
    gateway_module: Any,
    request: Request,
    user: UserContext,
    *,
    session_id: str,
    request_id: str,
) -> Any:
    identity = gateway_module.UpstreamIdentity(
        user_id=user.user_id,
        namespace=user.namespace,
        session_id=session_id,
        request_id=request_id,
    )
    expected = {
        "x-mnemos-user-id": identity.user_id,
        "x-mnemos-namespace": identity.namespace,
        "x-mnemos-session": identity.session_id,
        "x-mnemos-request-id": identity.request_id,
    }
    for header, value in expected.items():
        supplied = request.headers.get(header)
        if supplied is not None and supplied != value:
            logger.warning(
                "[PANTHEON] stripped spoofed %s header for request_id=%s",
                header,
                request_id,
            )
    return identity


def _consultation_cap_exceeded(result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": "0"},
        content={
            "error": {
                "type": "pantheon_usage_tier_cap_exceeded",
                "message": (
                    "usage_tier=consultation_only is capped per user session; "
                    "start a new session or choose an agentic_ok model for agent workflows"
                ),
                "usage_tier": "consultation_only",
                "cap": result.cap,
                "used": result.used,
                "retry_after": None,
            }
        },
    )


def _check_consultation_cap(
    consultation_cap_bucket: Any,
    decision: Any,
    *,
    user_id: str,
    session_id: str,
) -> Any | None:
    model = decision.model or {}
    if model.get("usage_tier") != "consultation_only":
        return None
    cap = get_settings().pantheon.consultation_cap
    return consultation_cap_bucket.check_and_increment(
        user_id=user_id,
        session_id=session_id,
        cap=cap,
    )


def _audit_payload_for_response(
    *,
    routing_payload: Any,
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: Any,
    outcome: str,
    started_at: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
    namespace: str | None = None,
    forwarded_user: str | None = None,
    resolved_wire_model: str | None = None,
    estimated_cost_usd: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, metadata = routing_payload(
        request_id=request_id,
        tenant_user_id=tenant_user_id,
        session_id=session_id,
        decision=decision,
        outcome=outcome,
        latency_ms=round((time.perf_counter() - started_at) * 1000.0, 3),
        response=response,
        error_class=error_class,
        namespace=namespace,
        forwarded_user=forwarded_user,
        resolved_wire_model=resolved_wire_model,
    )
    if estimated_cost_usd is not None:
        payload["cost_usd"] = estimated_cost_usd
        metadata["cost_usd"] = estimated_cost_usd
        metadata["estimated_cost_usd"] = estimated_cost_usd
    return payload, metadata


def _estimate_cost_usd(decision: Any, body: dict[str, Any]) -> float:
    pantheon = body.get("pantheon") if isinstance(body.get("pantheon"), dict) else {}
    is_passthrough = getattr(decision, "route_type", None) == "passthrough"
    raw = body.get("estimated_cost_usd", pantheon.get("estimated_cost_usd"))
    if raw is not None and not is_passthrough:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0
    model = decision.model or {}
    max_tokens = (
        body.get("max_tokens")
        or body.get("max_completion_tokens")
        or body.get("max_output_tokens")
        or pantheon.get("max_tokens")
        or pantheon.get("max_completion_tokens")
        or pantheon.get("max_output_tokens")
        or 0
    )
    try:
        out_tokens = max(0, int(max_tokens or 0))
    except (TypeError, ValueError):
        out_tokens = 0
    in_cost = _safe_float(model.get("input_cost_per_mtok") or model.get("cost_per_mtok") or 0.0)
    out_cost = _safe_float(model.get("output_cost_per_mtok") or model.get("cost_per_mtok") or 0.0)
    if is_passthrough:
        if out_tokens <= 0:
            try:
                out_tokens = max(1, int(get_settings().pantheon.passthrough_default_estimated_output_tokens))
            except Exception:
                out_tokens = 4096
        in_tokens = max(1, _approx_prompt_tokens(body))
        return ((in_tokens * in_cost) + (out_tokens * out_cost)) / 1_000_000.0
    return (out_tokens * out_cost) / 1_000_000.0 if out_tokens else (in_cost + out_cost) / 1_000_000.0


def _budget_denied_response(decision: Any) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content={
            "error": {
                "type": "pantheon_budget_exceeded",
                "message": decision.reason,
                "remaining_usd": decision.remaining_usd,
                "limit_usd": decision.limit_usd,
                "spent_usd": decision.spent_usd,
            }
        },
    )


async def _check_budget_or_deny(
    budget_module: Any,
    decision: Any,
    body: dict[str, Any],
) -> tuple[JSONResponse | None, float]:
    import mnemos.core.lifecycle as lc

    estimated_cost_usd = _estimate_cost_usd(decision, body)
    budget_decision = await budget_module.evaluate_budget(
        backend=lc._persistence_backend,
        estimated_cost_usd=estimated_cost_usd,
        caller_subsystem="pantheon",
    )
    return (None if budget_decision.allowed else _budget_denied_response(budget_decision), estimated_cost_usd)


async def _record_pantheon_ledger(payload: dict[str, Any], metadata: dict[str, Any], decision: Any) -> None:
    import mnemos.core.lifecycle as lc

    backend = lc._persistence_backend
    recorder = getattr(backend, "record_usage_ledger", None) if backend is not None else None
    if recorder is None:
        return
    cost_override = None
    if getattr(decision, "route_type", None) == "passthrough" and payload.get("cost_usd") is not None:
        cost_override = Decimal(str(payload["cost_usd"]))
    record = UsageLedgerRecord(
        provider=str(decision.provider or "pantheon"),
        model=str(decision.model_id or decision.alias),
        task_kind=str(decision.task_type or payload.get("alias_or_model") or "pantheon"),
        tokens_in=int(payload.get("tokens_in") or 0),
        tokens_out=int(payload.get("tokens_out") or 0),
        tokens_reasoning=0,
        latency_ms=int(round(float(payload.get("latency_ms") or 0))),
        outcome="ok" if payload.get("outcome") == "success" else "err",
        caller_subsystem="pantheon",
        tier="api",
        session_id=str(metadata.get("session_id") or "") or None,
        request_count=1,
        plan_window_id=compute_plan_window_id(str(decision.provider or "pantheon"), "api"),
        path_kind="api",
        est_cost_usd=cost_override,
    )
    try:
        async with backend.transactional() as tx:
            result = await recorder(tx, record)
        if payload.get("cost_usd") is None:
            payload["cost_usd"] = float(result.est_cost_usd)
            metadata["cost_usd"] = payload["cost_usd"]
    except Exception as exc:
        logger.debug("[PANTHEON] usage_ledger record failed: %s", exc)


async def _log_route_outcome(
    *,
    routing_payload: Any,
    schedule_routing_audit: Any,
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: Any,
    outcome: str,
    started_at: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
    namespace: str | None = None,
    forwarded_user: str | None = None,
    resolved_wire_model: str | None = None,
    estimated_cost_usd: float | None = None,
) -> None:
    payload, metadata = _audit_payload_for_response(
        routing_payload=routing_payload,
        request_id=request_id,
        tenant_user_id=tenant_user_id,
        session_id=session_id,
        decision=decision,
        outcome=outcome,
        started_at=started_at,
        response=response,
        error_class=error_class,
        namespace=namespace,
        forwarded_user=forwarded_user,
        resolved_wire_model=resolved_wire_model,
        estimated_cost_usd=estimated_cost_usd,
    )
    if outcome != "budget_denied":
        await _record_pantheon_ledger(payload, metadata, decision)
    schedule_routing_audit(payload, metadata)


def _stream_event_payloads(event: str) -> list[dict[str, Any]]:
    raw_payloads = []
    for line in event.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        raw_payloads.append(raw)
    if not raw_payloads:
        return []
    joined = "\n".join(raw_payloads)
    try:
        payload = json.loads(joined)
        return [payload] if isinstance(payload, dict) else []
    except json.JSONDecodeError:
        payloads: list[dict[str, Any]] = []
        for raw in raw_payloads:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads


def _stream_event_wire_model(event: str) -> str | None:
    wire_model: str | None = None
    for payload in _stream_event_payloads(event):
        model = payload.get("model")
        if isinstance(model, str) and model:
            wire_model = model
    return wire_model


def _normalize_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None

    def _int_value(*keys: str) -> int | None:
        for key in keys:
            if usage.get(key) is None:
                continue
            try:
                return max(0, int(usage[key]))
            except (TypeError, ValueError):
                continue
        return None

    prompt_tokens = _int_value("prompt_tokens", "input_tokens")
    completion_tokens = _int_value("completion_tokens", "output_tokens")
    total_tokens = _int_value("total_tokens")
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None
    prompt = prompt_tokens or 0
    completion = completion_tokens or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_tokens if total_tokens is not None else prompt + completion,
    }


def _stream_event_usage(event: str) -> dict[str, int] | None:
    usage: dict[str, int] | None = None
    for payload in _stream_event_payloads(event):
        event_usage = _normalize_usage(payload.get("usage"))
        if event_usage is not None:
            usage = event_usage
    return usage


def _stream_event_completion_text(event: str) -> str:
    fragments: list[str] = []
    for payload in _stream_event_payloads(event):
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if content is not None:
                fragments.append(str(content))
    return "".join(fragments)


def _stream_usage_response(
    *,
    model: str,
    usage: dict[str, int] | None,
    fallback_prompt_tokens: int,
    completion_text: str,
) -> dict[str, Any] | None:
    if usage is None and not completion_text:
        return None
    response_usage = usage or {
        "prompt_tokens": fallback_prompt_tokens,
        "completion_tokens": _approx_text_tokens(completion_text),
    }
    response_usage.setdefault("total_tokens", response_usage["prompt_tokens"] + response_usage["completion_tokens"])
    return {"model": model, "usage": response_usage}


def _first_chat_message(chat: dict[str, Any]) -> dict[str, Any]:
    choices = chat.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    choice = choices[0]
    if not isinstance(choice, dict):
        return {}
    message = choice.get("message")
    return message if isinstance(message, dict) else {}


async def _list_models_impl() -> dict[str, Any]:
    catalog, *_ = _pantheon_imports()
    return await catalog.models_response()


@router.get("/models")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def list_models(
    request: Request,
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    return await _list_models_impl()


async def _chat_completions_impl(
    request: Request,
    body: dict[str, Any],
    user: UserContext,
) -> Any:
    imports = _pantheon_imports()
    (
        _catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        consultation_cap_bucket,
        routing_payload,
        schedule_routing_audit,
        *optional_imports,
    ) = imports
    budget = optional_imports[0] if optional_imports else None
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(status_code=400, detail="messages required")
    model = _body_model(body)
    decision: Any | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    identity = _upstream_identity(gateway, request, user, session_id=session_id, request_id=request_id)
    try:
        decision = await pantheon_router.route_model(model, body)
        cap_result = _check_consultation_cap(
            consultation_cap_bucket,
            decision,
            user_id=user.user_id,
            session_id=session_id,
        )
        if cap_result is not None and not cap_result.allowed:
            return _consultation_cap_exceeded(cap_result)
        if budget is not None:
            budget_started_at = time.perf_counter()
            budget_response, estimated_cost_usd = await _check_budget_or_deny(budget, decision, body)
            if budget_response is not None:
                await _log_route_outcome(
                    routing_payload=routing_payload,
                    schedule_routing_audit=schedule_routing_audit,
                    request_id=request_id,
                    tenant_user_id=user.user_id,
                    session_id=session_id,
                    decision=decision,
                    outcome="budget_denied",
                    started_at=budget_started_at,
                    namespace=user.namespace,
                    forwarded_user=identity.opaque_user,
                    resolved_wire_model=decision.model_id or decision.alias,
                    estimated_cost_usd=estimated_cost_usd,
                )
                return budget_response
        started_at = time.perf_counter()
        forward_body = gateway.attach_upstream_identity(body, identity)
        if body.get("stream") is True:
            async def audited_stream():
                stream_buffer = ""
                wire_model: str | None = None
                usage: dict[str, int] | None = None
                completion_text = ""
                outcome = "success"
                error_class: str | None = None
                fallback_prompt_tokens = _approx_prompt_tokens(body)

                def ingest_event(event: str) -> None:
                    nonlocal completion_text, usage, wire_model
                    event_model = _stream_event_wire_model(event)
                    if event_model:
                        wire_model = event_model
                    event_usage = _stream_event_usage(event)
                    if event_usage is not None:
                        usage = event_usage
                    completion_text += _stream_event_completion_text(event)

                try:
                    async for chunk in gateway.stream_chat_completion(decision, forward_body):
                        try:
                            stream_buffer += chunk.decode("utf-8", "ignore")
                        except AttributeError:
                            stream_buffer += str(chunk)
                        events = stream_buffer.split("\n\n")
                        stream_buffer = events.pop()
                        for event in events:
                            ingest_event(event)
                        yield chunk
                except asyncio.CancelledError as exc:
                    outcome = "cancelled"
                    error_class = exc.__class__.__name__
                    raise
                except GeneratorExit as exc:
                    outcome = "cancelled"
                    error_class = exc.__class__.__name__
                    raise
                except Exception as exc:
                    outcome = "error"
                    error_class = exc.__class__.__name__
                    raise
                finally:
                    if stream_buffer:
                        ingest_event(stream_buffer)
                    response = _stream_usage_response(
                        model=wire_model or decision.model_id or decision.alias,
                        usage=usage,
                        fallback_prompt_tokens=fallback_prompt_tokens,
                        completion_text=completion_text,
                    )
                    try:
                        await _log_route_outcome(
                            routing_payload=routing_payload,
                            schedule_routing_audit=schedule_routing_audit,
                            request_id=request_id,
                            tenant_user_id=user.user_id,
                            session_id=session_id,
                            decision=decision,
                            outcome=outcome,
                            started_at=started_at,
                            response=response,
                            error_class=error_class,
                            namespace=user.namespace,
                            forwarded_user=identity.opaque_user,
                            resolved_wire_model=wire_model or decision.model_id or decision.alias,
                        )
                    except Exception as exc:
                        logger.debug("[PANTHEON] streaming route audit failed: %s", exc)

            return StreamingResponse(
                audited_stream(),
                media_type="text/event-stream",
            )
        response_data = await gateway.forward_chat_completion(decision, forward_body)
        await _log_route_outcome(
            routing_payload=routing_payload,
            schedule_routing_audit=schedule_routing_audit,
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
            namespace=user.namespace,
            forwarded_user=identity.opaque_user,
            resolved_wire_model=gateway.resolved_wire_model(response_data, decision),
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            await _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_audit=schedule_routing_audit,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
            )
        raise _to_http_exception(exc) from exc


@router.post("/chat/completions")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def chat_completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
):
    return await _chat_completions_impl(request, body, user)


async def _responses_impl(
    request: Request,
    body: dict[str, Any],
    user: UserContext,
) -> Any:
    # OpenAI Responses compatibility for clients that call /v1/responses
    # directly. Internally route through the same policy/cooldown gateway and
    # return an OpenAI-compatible response object.
    if "messages" not in body:
        input_value = body.get("input", "")
        content = input_value if isinstance(input_value, str) else str(input_value)
        body = {**body, "messages": [{"role": "user", "content": content}]}
    result = await _chat_completions_impl(request, body, user)
    if not isinstance(result, JSONResponse):
        return result
    chat = json.loads(result.body.decode("utf-8"))
    message = _first_chat_message(chat)
    usage = chat.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    output: list[dict[str, Any]] = []
    if message.get("content") is not None:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message.get("content") or ""}],
            }
        )
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": call.get("id"),
                "call_id": call.get("id"),
                "name": fn.get("name"),
                "arguments": fn.get("arguments") or "",
            }
        )
    return JSONResponse(
        status_code=result.status_code,
        content={
            "id": chat.get("id"),
            "object": "response",
            "created_at": chat.get("created"),
            "model": chat.get("model"),
            "output": output,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        },
    )


@router.post("/embeddings")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def embeddings(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> JSONResponse:
    (
        _catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        _consultation_cap_bucket,
        routing_payload,
        schedule_routing_audit,
        budget,
    ) = _pantheon_imports()
    if "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    model = _body_model(body)
    decision: Any | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    identity = _upstream_identity(gateway, request, user, session_id=session_id, request_id=request_id)
    try:
        decision = await pantheon_router.route_model(model, body)
        budget_started_at = time.perf_counter()
        budget_response, estimated_cost_usd = await _check_budget_or_deny(budget, decision, body)
        if budget_response is not None:
            await _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_audit=schedule_routing_audit,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="budget_denied",
                started_at=budget_started_at,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
                resolved_wire_model=decision.model_id or decision.alias,
                estimated_cost_usd=estimated_cost_usd,
            )
            return budget_response
        started_at = time.perf_counter()
        forward_body = gateway.attach_upstream_identity(body, identity)
        response_data = await gateway.forward_embeddings(decision, forward_body)
        await _log_route_outcome(
            routing_payload=routing_payload,
            schedule_routing_audit=schedule_routing_audit,
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
            namespace=user.namespace,
            forwarded_user=identity.opaque_user,
            resolved_wire_model=gateway.resolved_wire_model(response_data, decision),
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            await _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_audit=schedule_routing_audit,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
            )
        raise _to_http_exception(exc) from exc


@openai_router.get("/models")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def openai_list_models(
    request: Request,
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    return await _list_models_impl()


@openai_router.post("/chat/completions")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def openai_chat_completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
):
    return await _chat_completions_impl(request, body, user)


@openai_router.post("/responses")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def openai_responses(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
):
    return await _responses_impl(request, body, user)


@router.get("/route/explain")
@limiter.limit(_pantheon_gateway_rate_limit, key_func=_pantheon_rate_key)
async def route_explain(
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    model: str | None = Query(default=None),
    model_or_alias: str | None = Query(default=None),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    (
        _catalog,
        _gateway,
        pantheon_router,
        PantheonRoutingError,
        _consultation_cap_bucket,
        _routing_payload,
        _schedule_routing_audit,
        _budget,
    ) = _pantheon_imports()
    request_body: dict[str, Any] = dict(body or {})
    if model_or_alias is not None:
        request_body["model_or_alias"] = model_or_alias
    if model is not None:
        request_body["model"] = model
    try:
        return await pantheon_router.explain_route(request_body)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
