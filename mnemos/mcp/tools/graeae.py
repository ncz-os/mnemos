"""MCP tool handler for GRAEAE multi-provider consensus consultations.

Exposes the GRAEAE engine's consult() method over MCP so reasoning
consultations are callable from STUDIO and jperlow-mlt(.4) without
HTTP loopback — the engine runs in-process, exactly as the
/v1/consultations route does.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException

from mnemos.api.persistence_helpers import require_consultations_backend
from mnemos.api.routes.consultations import (
    audit_genesis_hash,
    _extract_memory_ids,
    _require_non_empty_consultation_result,
    _to_graeae_provider,
)
from mnemos.core.auth_context import UserContext

from ._runtime import (
    current_mcp_backend_api_key,
    reset_mcp_backend_context,
    set_mcp_backend_context,
    _mcp_user_or_system,
    _safe_path_segment,
    _tool,
)

logger = logging.getLogger(__name__)

_MUSE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _validate_muses(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("muses must be a list with at most 16 items")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _MUSE_RE.match(item):
            raise ValueError(f"invalid muse name: {item!r}")
        out.append(item)
    return out


async def tool_graeae_consult(
    prompt: str,
    category: str = "general",
    muses: list[str] | None = None,
    mode: str = "auto",
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Consult GRAEAE multi-provider consensus engine over MCP.

    Calls the SAME in-process engine function the /v1/consultations
    route uses — no HTTP loopback. Returns synthesis + per-muse
    outputs when available.

    Args:
        prompt: The consultation prompt / question.
        category: Task category (maps to task_type). Default "general".
        muses: Optional list of provider names to consult. When set,
               only those providers are queried; when None, the engine
               uses its default auto lineup.
        mode: Consultation mode (auto, single, debate, majority, all,
              local, external). Default "auto".
        user: MCP caller context (injected by the dispatcher).

    Returns:
        dict with synthesis (consensus_response), per_muse outputs,
        winning_muse, consensus_score, cost, and latency_ms.
    """
    _safe_path_segment(category, label="category")

    from mnemos.domain.graeae.engine import get_graeae_engine

    try:
        user = _mcp_user_or_system(user)
    except PermissionError as e:
        return {"success": False, "error": str(e)}

    context_tokens = None
    if not user.authenticated:
        context_tokens = set_mcp_backend_context(
            api_key=current_mcp_backend_api_key(),
            user_id=user.user_id,
            role=user.role,
            namespace=user.namespace,
        )

    try:
        # Audit persistence needs the in-process persistence backend, which is
        # initialised by the HTTP app lifespan. The stdio MCP server runs the
        # tools without that lifespan, so the backend global is absent there.
        # Resolve it softly: when present (HTTP route / in-process callers) we
        # persist + audit as required; when absent (stdio MCP) we still return
        # the engine synthesis but skip the audit row — the long-standing MCP
        # behaviour before audit persistence was made mandatory.
        from mnemos.core import lifecycle as _lifecycle

        backend = (
            require_consultations_backend()
            if _lifecycle._persistence_backend is not None
            else None
        )
        engine = get_graeae_engine()

        # Map `category` → engine `task_type` (one-to-one for MCP callers)
        task_type = category or "general"

        # Build a selection dict when muses are explicitly provided.
        # When muses is None, selection=None → engine uses auto lineup.
        # An empty list is an explicit "query no providers" — error it.
        selection: dict[str, str | None] | None = None
        if muses is not None:
            muses = _validate_muses(muses)
            normalised = [_to_graeae_provider(m) for m in muses]
            unknown: list[str] = []
            seen: set[str] = set()
            selection = {}
            for m in normalised:
                if m in engine.providers:
                    if m not in seen:
                        selection[m] = None
                        seen.add(m)
                else:
                    unknown.append(m)
            if unknown:
                return {
                    "success": False,
                    "error": f"unknown provider(s): {unknown}",
                    "available": sorted(engine.providers.keys()),
                }
            if not selection:
                return {
                    "success": False,
                    "error": "no valid providers in muses list",
                    "available": sorted(engine.providers.keys()),
                }

        try:
            result = await engine.consult(
                prompt=prompt,
                task_type=task_type,
                selection=selection,
                mode=mode,
            )
            result = _require_non_empty_consultation_result(result, mode)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except HTTPException as e:
            return {"success": False, "error": str(e.detail)}
        except Exception as e:
            logger.exception("[MCP] graeae_consult engine failed")
            return {
                "success": False,
                "error": "Consultation engine error",
                "error_type": type(e).__name__,
            }

        consultation_id: str | None = None
        if backend is None and result.get("all_responses"):
            # stdio MCP context (no in-process backend) — return synthesis
            # without persisting an audit row, as graeae_consult did for months
            # before audit persistence became mandatory on the HTTP route.
            logger.warning(
                "[MCP] graeae_consult: no in-process persistence backend "
                "(stdio MCP context) — returning synthesis without audit row"
            )
        elif result.get("all_responses"):
            memory_ids = _extract_memory_ids(result)
            consensus_response = result.get("consensus_response", "") or ""
            consensus_score = float(result.get("consensus_score", 0.0) or 0.0)
            winning_muse = result.get("winning_muse")
            engine_cost = float(result.get("cost", 0.0) or 0.0)
            engine_latency_ms = int(result.get("latency_ms", 0) or 0)
            try:
                async with backend.transactional() as tx:
                    consultation_id = str(
                        await backend.consultations.create_consultation_with_audit(
                            tx,
                            prompt=prompt,
                            task_type=task_type,
                            consensus_response=consensus_response,
                            consensus_score=consensus_score,
                            winning_muse=winning_muse,
                            cost=engine_cost,
                            latency_ms=engine_latency_ms,
                            mode=mode,
                            owner_id=user.user_id,
                            namespace=user.namespace,
                            memory_ids=memory_ids,
                            genesis_hash=audit_genesis_hash(),
                        )
                    )
            except HTTPException as e:
                return {"success": False, "error": str(e.detail)}
            except Exception as e:
                logger.exception("[MCP] graeae_consult persistence failed")
                return {
                    "success": False,
                    "error": "Consultation persistence failed; audit trail is required.",
                    "error_type": type(e).__name__,
                }

        # Build a caller-friendly response shape:
        #   synthesis   — the winning consensus response text (if any)
        #   per_muse    — each provider's result keyed by provider name
        #   metadata    — winning_muse, consensus_score, cost, latency_ms
        all_responses = result.get("all_responses", {})
        per_muse: dict[str, dict[str, Any]] = {}
        for name, resp in all_responses.items():
            per_muse[name] = {
                "status": resp.get("status", "unknown"),
                "response_text": resp.get("response_text", ""),
                "model_id": resp.get("model_id", ""),
                "final_score": resp.get("final_score", 0.0),
            }
            if resp.get("error"):
                per_muse[name]["error"] = resp["error"]

        return {
            "success": True,
            "consultation_id": consultation_id,
            "synthesis": result.get("consensus_response", ""),
            "per_muse": per_muse,
            "winning_muse": result.get("winning_muse"),
            "consensus_score": result.get("consensus_score", 0.0),
            "cost": result.get("cost", 0.0),
            "latency_ms": result.get("latency_ms", 0),
            "mode": mode,
            "cache_hit": bool(result.get("cache_hit")),
            "round_1": result.get("round_1"),
            "round_2": result.get("round_2"),
            "quorum_reached": result.get("quorum_reached"),
            "quorum_threshold": result.get("quorum_threshold"),
            "similarity_pairs": result.get("similarity_pairs"),
        }
    finally:
        if context_tokens is not None:
            reset_mcp_backend_context(context_tokens)


TOOLS: dict[str, dict[str, Any]] = {
    "graeae_consult": _tool(
        "Consult GRAEAE multi-provider consensus engine. "
        "Submits a reasoning consultation to the GRAEAE engine "
        "in-process (same as the /v1/consultations HTTP route) and "
        "returns a synthesis with per-muse outputs. Supports all "
        "consultation modes: auto, single, debate, majority, all, "
        "local, external. When muses is specified, only those "
        "providers are queried.",
        {
            "prompt": {
                "type": "string",
                "description": "The consultation prompt or question to submit.",
            },
            "category": {
                "type": "string",
                "description": "Task category mapped to GRAEAE task_type. "
                               "Default: 'general'. Common values: reasoning, "
                               "architecture_design, code_generation, web_search.",
            },
            "muses": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 16,
                "description": "Optional list of provider names to consult "
                               "(e.g. ['claude', 'openai', 'gemini']). "
                               "When set, only those providers are queried. "
                               "When omitted, the engine uses its default "
                               "auto lineup.",
            },
            "mode": {
                "type": "string",
                "description": "Consultation mode. Default: 'auto'. "
                               "Supported: auto, single, debate, majority, "
                               "all, local, external.",
            },
        },
        ["prompt"],
        tool_graeae_consult,
    ),
}
