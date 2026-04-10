"""GRAEAE multi-provider consultation endpoints — v2 adds hash-chained audit log."""
import hashlib
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_GENESIS_HASH = hashlib.sha256(b"MNEMOS_AUDIT_GENESIS_v2").hexdigest()


# ── Models ────────────────────────────────────────────────────────────────────

from api.models import ConsultationRequest


class AuditLogEntry(BaseModel):
    id: str
    sequence_num: int
    consultation_id: Optional[str] = None
    prompt_hash: str
    response_hash: str
    chain_hash: str
    prev_id: Optional[str] = None
    task_type: Optional[str] = None
    provider: Optional[str] = None
    quality_score: Optional[float] = None
    created_at: str


class AuditVerifyResponse(BaseModel):
    valid: bool
    entries_checked: int
    first_broken_sequence: Optional[int] = None
    message: str


# ── Audit helpers ─────────────────────────────────────────────────────────────

async def _write_audit_entry(
    pool,
    consultation_id,
    prompt: str,
    response: str,
    task_type: str,
    provider: str,
    quality_score: float,
) -> None:
    """Append a hash-chained entry to graeae_audit_log. Uses SELECT FOR UPDATE
    on the last row to serialise concurrent inserts."""
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    response_hash = hashlib.sha256(response.encode()).hexdigest()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Lock the latest row to serialise chain computation
                prev_row = await conn.fetchrow(
                    "SELECT id, chain_hash FROM graeae_audit_log "
                    "ORDER BY sequence_num DESC LIMIT 1 FOR UPDATE"
                )
                if prev_row:
                    prev_chain = prev_row["chain_hash"]
                    prev_id = prev_row["id"]
                else:
                    prev_chain = _GENESIS_HASH
                    prev_id = None

                chain_hash = hashlib.sha256(
                    (prev_chain + response_hash).encode()
                ).hexdigest()

                await conn.execute(
                    "INSERT INTO graeae_audit_log "
                    "(consultation_id, prompt_hash, response_hash, chain_hash, "
                    "prev_id, task_type, provider, quality_score) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    consultation_id, prompt_hash, response_hash, chain_hash,
                    prev_id, task_type, provider, quality_score,
                )
    except Exception as e:
        logger.warning(f"[AUDIT] Failed to write audit entry: {e}")


# ── Consultation endpoint ─────────────────────────────────────────────────────

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

        consultation_id = None
        if _lc._pool and result.get("all_responses"):
            try:
                best_resp = max(
                    result["all_responses"].items(),
                    key=lambda x: x[1].get("final_score", 0),
                )
                async with _lc._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """INSERT INTO graeae_consultations
                            (prompt, task_type, consensus_response, consensus_score,
                             winning_muse, cost, latency_ms, mode)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                           RETURNING id""",
                        request.prompt,
                        request.task_type,
                        best_resp[1].get("response_text", "")[:500],
                        best_resp[1].get("final_score", 0),
                        best_resp[0],
                        0.02,
                        best_resp[1].get("latency_ms", 0),
                        request.mode or "auto",
                    )
                    consultation_id = row["id"] if row else None

                # Write hash-chained audit entry
                await _write_audit_entry(
                    pool=_lc._pool,
                    consultation_id=consultation_id,
                    prompt=request.prompt,
                    response=best_resp[1].get("response_text", ""),
                    task_type=request.task_type or "reasoning",
                    provider=best_resp[0],
                    quality_score=best_resp[1].get("final_score", 0),
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


# ── Audit log endpoints ───────────────────────────────────────────────────────

@router.get("/graeae/audit", response_model=List[AuditLogEntry])
async def list_audit_log(
    limit: int = Query(20, le=100),
    offset: int = 0,
    user: UserContext = Depends(get_current_user),
):
    """List GRAEAE audit log entries (newest first)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sequence_num, consultation_id, prompt_hash, response_hash, "
            "chain_hash, prev_id, task_type, provider, quality_score, created_at "
            "FROM graeae_audit_log ORDER BY sequence_num DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
    return [
        AuditLogEntry(
            id=str(r["id"]),
            sequence_num=r["sequence_num"],
            consultation_id=str(r["consultation_id"]) if r["consultation_id"] else None,
            prompt_hash=r["prompt_hash"],
            response_hash=r["response_hash"],
            chain_hash=r["chain_hash"],
            prev_id=str(r["prev_id"]) if r["prev_id"] else None,
            task_type=r.get("task_type"),
            provider=r.get("provider"),
            quality_score=r.get("quality_score"),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.get("/graeae/audit/verify", response_model=AuditVerifyResponse)
async def verify_audit_chain(
    user: UserContext = Depends(get_current_user),
):
    """Verify the integrity of the hash chain in the GRAEAE audit log."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sequence_num, response_hash, chain_hash, prev_id "
            "FROM graeae_audit_log ORDER BY sequence_num ASC"
        )

    if not rows:
        return AuditVerifyResponse(
            valid=True,
            entries_checked=0,
            message="Audit log is empty",
        )

    prev_chain = _GENESIS_HASH
    for row in rows:
        expected = hashlib.sha256(
            (prev_chain + row["response_hash"]).encode()
        ).hexdigest()
        if expected != row["chain_hash"]:
            return AuditVerifyResponse(
                valid=False,
                entries_checked=row["sequence_num"],
                first_broken_sequence=row["sequence_num"],
                message=f"Chain broken at sequence {row['sequence_num']}: "
                        f"expected {expected[:16]}…, stored {row['chain_hash'][:16]}…",
            )
        prev_chain = row["chain_hash"]

    return AuditVerifyResponse(
        valid=True,
        entries_checked=len(rows),
        message=f"All {len(rows)} entries verified — chain intact",
    )
