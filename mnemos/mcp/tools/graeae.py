"""MCP tool handlers for GRAEAE multi-provider consultation."""

from __future__ import annotations

import logging
from typing import Any

from mnemos.core.auth_context import UserContext

from ._runtime import _rest_get, _rest_post, _safe_path_segment, _tool

logger = logging.getLogger(__name__)


async def tool_graeae_consult(
    prompt: str,
    task_type: str = "reasoning",
    mode: str = "auto",
    muses: list[str] | None = None,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Consult the GRAEAE multi-provider consensus engine."""
    del user
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    body: dict[str, Any] = {"prompt": prompt, "task_type": task_type, "mode": mode}
    if muses:
        if not isinstance(muses, list) or not all(isinstance(m, str) for m in muses):
            raise ValueError("muses must be a list of strings")
        body["providers"] = muses
    try:
        result = await _rest_post("/v1/consultations", body)
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"[MCP] graeae_consult failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def tool_graeae_get_consultation(
    consultation_id: str,
    user: UserContext | None = None,
) -> dict[str, Any]:
    """Fetch a previously-run GRAEAE consultation by ID."""
    del user
    _safe_path_segment(consultation_id, label="consultation_id")
    try:
        result = await _rest_get(f"/v1/consultations/{consultation_id}")
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"[MCP] graeae_get_consultation failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


TOOLS: dict[str, dict[str, Any]] = {
    "graeae_consult": _tool(
        "Consult GRAEAE's multi-provider consensus engine for reasoning, architecture, or code-generation tasks.",
        {
            "prompt": {"type": "string", "description": "The consultation prompt or question."},
            "task_type": {"type": "string", "default": "reasoning", "description": "Task category, e.g. reasoning, architecture_design, code_generation."},
            "mode": {"type": "string", "default": "auto", "description": "Routing mode: auto, local, external, or all."},
            "muses": {"type": "array", "items": {"type": "string"}, "maxItems": 16, "description": "Optional explicit provider list."},
        },
        ["prompt"],
        tool_graeae_consult,
    ),
    "graeae_get_consultation": _tool(
        "Fetch a previously-run GRAEAE consultation by its ID.",
        {
            "consultation_id": {"type": "string", "description": "The consultation ID returned by graeae_consult."},
        },
        ["consultation_id"],
        tool_graeae_get_consultation,
    ),
}
