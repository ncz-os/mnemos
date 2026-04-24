"""
OpenAI-Compatible Gateway for MNEMOS

Provides `/v1/chat/completions` and `/v1/models` endpoints compatible with OpenAI SDK.
All claw systems authenticate with a single MNEMOS bearer token; MNEMOS manages provider keys.

Model selection:
  - explicit model name: passthrough to that provider (user pulls from /v1/models)
  - model="auto": optimizer recommends model based on task type and cost budget
  - model="best-coding", etc.: resolve alias to concrete model

Memory injection:
  - Semantic search on last user message
  - LETHE-compress relevant context (512-token budget)
  - Add to system prompt with [MNEMOS context] header
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from graeae.engine import get_graeae_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])

# Model capability mapping for task-type routing
TASK_CAPABILITY_MAP = {
    "code_generation": ["coding"],
    "reasoning": ["reasoning", "logic"],
    "architecture_design": ["reasoning"],
    "summarization": ["reasoning"],
    "web_search": ["online", "search"],
}

# Model aliases for convenience
MODEL_ALIASES = {
    "best-coding": "gpt-4o",  # Fast, strong at code generation
    "best-reasoning": "claude-3-5-sonnet-20241022",
    "fastest": "llama-3.3-70b-versatile",  # Groq
    "cheapest": "llama-2-70b",  # Ollama fallback
}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = 1.0
    user: Optional[str] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


async def _search_mnemos_context(query: str, user: UserContext, limit: int = 5) -> List[Dict[str, Any]]:
    """Search MNEMOS for relevant context based on user query.

    Returns list of dicts with 'id' and 'content' keys for memory
    injection into /v1/chat/completions. Non-root callers are scoped
    to their owner_id AND namespace (v3.1.2 Tier 3 two-dimensional
    gate). Previously this path filtered on owner_id alone, which
    let cross-namespace memories leak into the gateway's injected
    context under the same owner.
    """
    if not _lc._pool:
        logger.debug("[MNEMOS] No DB pool available")
        return []

    is_root = user.role == "root"

    try:
        async with _lc._pool.acquire() as conn:
            # Full-text search on content + category filtering. Explicit
            # to_tsvector so we match the 'english' dictionary regardless of
            # the cluster's default_text_search_config and so the index (if
            # present) can actually be used.
            if is_root:
                # Root sees every memory regardless of tenancy.
                memories = await conn.fetch(
                    """
                    SELECT id, content, category FROM memories
                    WHERE
                        to_tsvector('english', content) @@ plainto_tsquery('english', $1)
                        OR category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                    ORDER BY updated DESC NULLS LAST
                    LIMIT $2
                    """,
                    query,
                    limit,
                )
            else:
                # v3.2 H1 fix: federated memories carry owner_id='federation'.
                # Include them alongside the caller's own rows so gateway
                # context injection surfaces knowledge pulled from peers.
                # Mutation paths keep the owner_id=$1 hard filter so
                # federated rows aren't writable by non-root.
                memories = await conn.fetch(
                    """
                    SELECT id, content, category FROM memories
                    WHERE (owner_id = $1 OR federation_source IS NOT NULL)
                      AND namespace = $2
                    AND (
                        to_tsvector('english', content) @@ plainto_tsquery('english', $3)
                        OR category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                    )
                    ORDER BY updated DESC NULLS LAST
                    LIMIT $4
                    """,
                    user.user_id,
                    user.namespace,
                    query,
                    limit,
                )
            logger.info(f"[MNEMOS] Found {len(memories)} memories for query '{query[:30]}...'")
            return [{"id": m["id"], "content": m["content"]} for m in memories]
    except Exception as e:
        logger.warning(f"[MNEMOS] Search failed for '{query[:50]}...': {e}")
        return []


async def _get_model_recommendation(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
) -> Optional[Dict[str, Any]]:
    """Query model optimizer for cost-aware model recommendation.

    Calls the /model-registry/recommend endpoint to find cheapest model
    meeting quality + capability requirements for the task_type.
    """
    pool = _lc._pool
    if not pool:
        logger.warning("[OPTIMIZER] No DB pool available")
        return None

    try:
        async with pool.acquire() as conn:
            # Map task types to required capabilities
            capability_map = {
                "code_generation": ["coding"],
                "reasoning": ["reasoning", "logic"],
                "architecture_design": ["reasoning"],
                "summarization": ["reasoning"],
                "web_search": ["online", "search"],
            }
            required_caps = capability_map.get(task_type, ["reasoning"])

            # Find models meeting criteria
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name, input_cost_per_mtok,
                    output_cost_per_mtok, capabilities, graeae_weight, context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND graeae_weight >= $1
                AND (input_cost_per_mtok + output_cost_per_mtok) / 2.0 <= $2
                AND capabilities @> $3
                ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                LIMIT 1
                """,
                quality_floor,
                cost_budget,
                required_caps,
            )

            if not models:
                # Fallback: cheapest model available (ignore budget)
                logger.info(
                    f"[OPTIMIZER] No model found for {task_type} "
                    f"(budget=${cost_budget}/MTok, quality>={quality_floor}), "
                    f"using fallback cheapest model"
                )
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name, input_cost_per_mtok,
                        output_cost_per_mtok, capabilities, graeae_weight, context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                    LIMIT 1
                    """
                )

            if not models:
                logger.warning("[OPTIMIZER] No models available, using default gpt-4o")
                return None

            model = models[0]
            avg_cost = (model["input_cost_per_mtok"] + model["output_cost_per_mtok"]) / 2.0

            logger.info(
                f"[OPTIMIZER] Recommended {model['provider']}/{model['model_id']} "
                f"for {task_type} (cost=${avg_cost:.2f}/MTok)"
            )

            return {
                "provider": model["provider"],
                "model_id": model["model_id"],
                "display_name": model.get("display_name"),
                "cost_per_mtok": avg_cost,
                "quality_score": model["graeae_weight"],
                "context_window": model.get("context_window"),
            }

    except Exception as e:
        logger.warning(f"[OPTIMIZER] Recommendation failed: {e}, using default")
        return None


def _flatten_messages_for_prompt(messages: List[Dict[str, str]]) -> str:
    """Serialize a chat-completions ``messages`` array to a single prompt string.

    Used as a fallback when GRAEAE's single-provider route accepts only a
    flat prompt. Preserves role boundaries so a provider that was given a
    system prompt, prior assistant turns, and a fresh user question sees
    all three, not just the last user message (regression for #M31-02).
    """
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
        if not content:
            continue
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            parts.append(f"[Tool]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
    user: UserContext,
) -> str:
    """Route request to selected provider via GRAEAE single-provider mode."""
    graeae = get_graeae_engine()
    # Flatten the full messages array rather than keeping only
    # messages[-1]. The prior behaviour silently dropped the system prompt,
    # injected memory context, and every prior turn — multi-turn chat via
    # /v1/chat/completions collapsed to single-shot.
    if not messages:
        raise HTTPException(status_code=400, detail="messages required")
    prompt = _flatten_messages_for_prompt(messages)

    # Determine provider from model name
    provider_map = {
        "claude": "claude",
        "gpt-4": "openai",
        "gpt-": "openai",
        "llama": "groq",
        "deepseek": "groq",
        "sonar": "perplexity",
        "grok": "xai",
        "gemini": "gemini",
    }

    provider = "groq"  # Default fallback
    for key, mapped in provider_map.items():
        if key in model.lower():
            provider = mapped
            break

    logger.info(
        f"[MNEMOS] Route: model={model} → provider={provider} "
        f"(messages={len(messages)}, prompt_chars={len(prompt)})"
    )

    try:
        # Use GRAEAE single-provider route (no consensus, just direct call)
        response = await graeae.route(provider, model, prompt, task_type="reasoning", timeout=30)

        if response.get("status") == "success":
            return response.get("response_text", "")
        # GRAEAE returns unavailable shape with an `error` field (v3.1.2).
        # Surface the cause in both the log line and the 503 detail so
        # operators see WHY the provider failed (missing key, 401, etc.)
        # without tailing debug logs.
        cause = response.get("error") or response.get("status") or "unknown"
        logger.error(
            "[MNEMOS] Provider %s unavailable: %s (status=%s)",
            provider, cause, response.get("status"),
        )
        raise HTTPException(
            status_code=503,
            detail=f"Provider {provider} unavailable: {cause}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MNEMOS] Routing to {provider} failed: {e}")
        raise HTTPException(status_code=503, detail=f"Routing error: {str(e)}")


# Provider key -> display name for the `owned_by` field in OpenAI
# /v1/models responses. Keys match the `provider` column values in
# db/migrations_model_registry.sql (xai, openai, gemini, groq, …).
# Unknown provider keys fall back to the key capitalized.
_PROVIDER_DISPLAY = {
    "xai": "xAI",
    "openai": "OpenAI",
    "gemini": "Google",
    "groq": "Groq",
    "anthropic": "Anthropic",
    "perplexity": "Perplexity",
    "together": "Together",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
}


def _owned_by(provider: Optional[str]) -> str:
    """Turn a provider key into an OpenAI-style owned_by display string."""
    if not provider:
        return "Unknown"
    return _PROVIDER_DISPLAY.get(provider.lower(), provider.capitalize())


# Defensive fallback for fresh installs whose model_registry hasn't
# been seeded yet. Matches the shape the old hardcoded list used; new
# deployments should run `update_model_registry.py` to populate real
# rows, but /v1/models stays usable in the meantime.
# Refreshed 2026-04-23 (v3.1.2 Defect 3) — aligned with the GRAEAE
# built-in provider defaults in graeae/engine.py._BUILTIN_PROVIDERS.
_FALLBACK_MODELS: list[dict] = [
    {"model_id": "gpt-5.2-chat-latest", "provider": "openai"},
    {"model_id": "claude-opus-4-6", "provider": "anthropic"},
    {"model_id": "gemini-3-pro-preview", "provider": "gemini"},
    {"model_id": "grok-4-1-fast", "provider": "xai"},
    {"model_id": "sonar-pro", "provider": "perplexity"},
    {"model_id": "llama-3.3-70b-versatile", "provider": "groq"},
]


def _row_model_id(r) -> str:
    """Support both dict fallback rows and asyncpg Record objects."""
    return r["model_id"] if hasattr(r, "__getitem__") else r.get("model_id")


def _row_provider(r) -> Optional[str]:
    return r["provider"] if hasattr(r, "__getitem__") else r.get("provider")


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """List available models from the model_registry table (v3.1.2).

    Returns every row where available=true AND deprecated=false,
    ordered by graeae_weight DESC so higher-quality models lead the
    response. On a fresh install where the registry is empty (or the
    query fails), falls back to a short built-in list so the endpoint
    stays usable until `update_model_registry.py` seeds the table.
    """
    rows: list = []
    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT provider, model_id, display_name
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY graeae_weight DESC NULLS LAST, model_id ASC
                    """
                )
        except Exception as exc:
            logger.warning(
                "[/v1/models] model_registry query failed, "
                "falling back to built-in list: %s", exc,
            )
            rows = []

    if not rows:
        rows = _FALLBACK_MODELS

    models = [
        ModelInfo(id=_row_model_id(r), owned_by=_owned_by(_row_provider(r)))
        for r in rows
    ]
    return ModelsResponse(data=models)


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """Look up a single model in the registry (v3.1.2).

    Aliases resolve first (best-coding etc. → concrete model), then
    the resolved id is checked against model_registry. If the model
    isn't in the registry the handler still returns it with
    owned_by='Unknown' — this is a passthrough API and operators
    sometimes route to locally configured models that aren't
    registered globally.
    """
    resolved_model = MODEL_ALIASES.get(model_id, model_id)
    provider: Optional[str] = None

    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT provider
                    FROM model_registry
                    WHERE model_id = $1
                      AND available = true
                      AND deprecated = false
                    LIMIT 1
                    """,
                    resolved_model,
                )
                if row is not None:
                    provider = row["provider"]
        except Exception as exc:
            logger.warning(
                "[/v1/models/%s] registry lookup failed: %s",
                model_id, exc,
            )

    return ModelInfo(id=resolved_model, owned_by=_owned_by(provider))


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """OpenAI-compatible chat completions endpoint with memory injection."""

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages required")

    # Extract last user message for context search and task detection
    last_msg = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            last_msg = msg.content
            break

    if not last_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    # Determine task type from content
    task_type = "reasoning"
    if any(kw in last_msg.lower() for kw in ["code", "function", "class", "def", "import", "syntax"]):
        task_type = "code_generation"
    elif any(kw in last_msg.lower() for kw in ["arch", "design", "pattern", "structure", "system"]):
        task_type = "architecture_design"

    logger.info(f"[MNEMOS] task_type={task_type}, searching memory...")

    # Search MNEMOS for context (non-blocking, graceful fallback)
    mnemos_docs = await _search_mnemos_context(last_msg, user, limit=3)

    # Resolve and validate model
    model = request.model or "gpt-4o"
    if model in MODEL_ALIASES:
        model = MODEL_ALIASES[model]

    # Handle auto model selection via optimizer
    if model == "auto":
        logger.info(f"[MNEMOS] model=auto requested, querying optimizer for task_type={task_type}")
        recommendation = await _get_model_recommendation(task_type=task_type)
        if recommendation:
            model = f"{recommendation['provider']}/{recommendation['model_id']}"
            logger.info(
                f"[MNEMOS] Optimizer recommended {recommendation['model_id']} "
                f"(cost=${recommendation['cost_per_mtok']:.2f}/MTok)"
            )
        else:
            logger.info("[MNEMOS] Optimizer failed, using default gpt-4o")
            model = "gpt-4o"

    logger.info(f"[MNEMOS] model={model}")

    # Build enhanced system prompt with MNEMOS context
    system_prompt = ""
    for msg in request.messages:
        if msg.role == "system":
            system_prompt = msg.content
            break

    if mnemos_docs:
        context_str = "\n\n".join([f"[Memory]\n{doc['content'][:500]}" for doc in mnemos_docs])
        system_prompt += f"\n\n[MNEMOS Context - {len(mnemos_docs)} memories]\n{context_str}"
        logger.info(f"[MNEMOS] Injected {len(mnemos_docs)} memories into context")

    # Prepare final messages for provider
    messages = []
    system_added = False

    for msg in request.messages:
        if msg.role == "system":
            if not system_added:
                messages.append({"role": "system", "content": system_prompt})
                system_added = True
        else:
            messages.append({"role": msg.role, "content": msg.content})

    if not system_added and system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    # Route to provider via GRAEAE
    try:
        response_text = await _route_to_provider(
            model=model,
            messages=messages,
            temperature=request.temperature or 0.7,
            max_tokens=request.max_tokens,
            user=user,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MNEMOS] Request failed: {e}")
        raise HTTPException(status_code=503, detail=f"Request failed: {str(e)}")

    # Format OpenAI-compatible response
    now = int(datetime.now(timezone.utc).timestamp())
    prompt_tokens = sum(len(m.get("content", "").split()) for m in messages)
    completion_tokens = len(response_text.split())

    return ChatCompletionResponse(
        id=f"chatcmpl-mnemos-{now}",
        created=now,
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text),
                finish_reason="stop",
            )
        ],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )
