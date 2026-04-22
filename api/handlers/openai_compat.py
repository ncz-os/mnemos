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

    Returns list of dicts with 'id' and 'content' keys for memory injection.
    """
    if not _lc._pool:
        logger.debug("[MNEMOS] No DB pool available")
        return []

    try:
        async with _lc._pool.acquire() as conn:
            # Full-text search on content + category filtering. Explicit
            # to_tsvector so we match the 'english' dictionary regardless of
            # the cluster's default_text_search_config and so the index (if
            # present) can actually be used.
            memories = await conn.fetch(
                """
                SELECT id, content, category FROM memories
                WHERE owner_id = $1
                AND (
                    to_tsvector('english', content) @@ plainto_tsquery('english', $2)
                    OR category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                )
                ORDER BY updated DESC NULLS LAST
                LIMIT $3
                """,
                user.user_id,
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


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
    user: UserContext,
) -> str:
    """Route request to selected provider via GRAEAE single-provider mode."""
    graeae = get_graeae_engine()
    prompt = messages[-1]["content"] if messages else ""

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

    logger.info(f"[MNEMOS] Route: model={model} → provider={provider}")

    try:
        # Use GRAEAE single-provider route (no consensus, just direct call)
        response = await graeae.route(provider, model, prompt, task_type="reasoning", timeout=30)

        if response.get("status") == "success":
            return response.get("response_text", "")
        else:
            logger.error(f"[MNEMOS] Provider {provider} returned error: {response.get('status')}")
            raise HTTPException(status_code=503, detail=f"Provider {provider} unavailable")

    except Exception as e:
        logger.error(f"[MNEMOS] Routing to {provider} failed: {e}")
        raise HTTPException(status_code=503, detail=f"Routing error: {str(e)}")


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """List available models in OpenAI format."""
    # Load from model_registry table (Phase 5)
    # For now, return built-in models
    models = [
        ModelInfo(id="claude-3-5-sonnet-20241022", owned_by="Anthropic"),
        ModelInfo(id="gpt-4o", owned_by="OpenAI"),
        ModelInfo(id="llama-3.3-70b-versatile", owned_by="Groq"),
        ModelInfo(id="grok-2-latest", owned_by="xAI"),
        ModelInfo(id="sonar-pro", owned_by="Perplexity"),
        ModelInfo(id="gemini-1.5-pro", owned_by="Google"),
    ]
    return ModelsResponse(data=models)


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """Get info about a specific model."""
    # Resolve alias
    resolved_model = MODEL_ALIASES.get(model_id, model_id)
    return ModelInfo(id=resolved_model, owned_by="Unknown")


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
