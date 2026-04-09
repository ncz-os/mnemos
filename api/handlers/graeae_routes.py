"""GRAEAE multi-provider consultation endpoints."""
import logging

from fastapi import APIRouter

import api.lifecycle as _lc
from api.models import ConsultationRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/graeae/consult")
async def consult_graeae(request: ConsultationRequest):
    """Consult GRAEAE multi-provider consensus engine."""
    logger.info(
        f"GRAEAE Consultation: {request.task_type} "
        f"(limit_chars={request.limit_chars}, format={request.format})"
    )
    try:
        from graeae_providers import get_graeae_engine
        engine = get_graeae_engine()
        result = await engine.consult(request.prompt, request.task_type)

        if request.limit_chars and result.get("all_responses"):
            for provider, resp in result["all_responses"].items():
                if isinstance(resp.get("response_text"), str):
                    resp["response_text"] = resp["response_text"][:request.limit_chars]
                    resp["truncated"] = len(resp.get("response_text", "")) >= request.limit_chars

        if request.format == "best" and result.get("all_responses"):
            best = max(result["all_responses"].items(), key=lambda x: x[1].get("final_score", 0))
            result["all_responses"] = {best[0]: best[1]}

        if _lc._pool and result.get("all_responses"):
            try:
                best_resp = max(
                    result["all_responses"].items(),
                    key=lambda x: x[1].get("final_score", 0),
                )
                async with _lc._pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO graeae_consultations
                            (prompt, task_type, consensus_response, consensus_score,
                             winning_muse, cost, latency_ms, mode)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                        request.prompt,
                        request.task_type,
                        best_resp[1].get("response_text", "")[:500],
                        best_resp[1].get("final_score", 0),
                        best_resp[0],
                        0.02,
                        best_resp[1].get("latency_ms", 0),
                        request.mode or "auto",
                    )
            except Exception as e:
                logger.warning(f"Failed to log consultation: {e}")

        return result

    except Exception as e:
        logger.error(f"GRAEAE error: {e}", exc_info=True)
        return {"error": str(e), "status": "error"}


@router.get("/graeae/health")
async def graeae_health():
    return {"status": "healthy", "service": "graeae"}
