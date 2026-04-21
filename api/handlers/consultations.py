"""GRAEAE multi-provider consultation endpoints — v3.0.0 unified service.

/v1/consultations — GRAEAE reasoning domain with hash-chained audit log and memory refs.
Replaces legacy graeae_routes.py (v2.x).
"""
import hashlib
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.rate_limit import limiter
from api.models import (
    ConsultationRequest,
    ConsultationResponse,
    ConsultationArtifact,
    AuditLogEntry,
    AuditVerifyResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["consultations"])

_GENESIS_HASH = hashlib.sha256(b"MNEMOS_AUDIT_GENESIS_v3").hexdigest()


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
    """Append a hash-chained entry to graeae_audit_log.
    Uses PostgreSQL advisory lock to serialize concurrent inserts."""
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    response_hash = hashlib.sha256(response.encode()).hexdigest()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Advisory lock serializes concurrent inserts.
                # SELECT FOR UPDATE alone has a TOCTOU race: T2 reads the "last row"
                # before blocking, then computes the chain against that stale row after
                # T1 has already inserted a newer one.
                # Advisory lock (magic key = 0x4772616561 = "Graea") ensures only
                # one writer holds the chain tip at a time.
                await conn.execute("SELECT pg_advisory_xact_lock(285734657)")
                prev_row = await conn.fetchrow(
                    "SELECT id, chain_hash FROM graeae_audit_log "
                    "ORDER BY sequence_num DESC LIMIT 1"
                )
                if prev_row:
                    prev_chain = prev_row["chain_hash"]
                    prev_id = prev_row["id"]
                else:
                    prev_chain = _GENESIS_HASH
                    prev_id = None

                # Chain covers prev_chain + prompt_hash + response_hash so that
                # neither the prompt nor the response can be swapped without
                # breaking chain integrity.
                chain_hash = hashlib.sha256(
                    (prev_chain + prompt_hash + response_hash).encode()
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


async def _write_memory_refs(
    pool,
    consultation_id: str,
    memory_ids: List[str],
) -> None:
    """Record which memories were injected into this consultation."""
    if not memory_ids or not pool:
        return
    try:
        async with pool.acquire() as conn:
            for memory_id in memory_ids:
                await conn.execute(
                    "INSERT INTO consultation_memory_refs "
                    "(consultation_id, memory_id, injected_at) "
                    "VALUES ($1, $2, NOW()) "
                    "ON CONFLICT DO NOTHING",
                    consultation_id, memory_id,
                )
    except Exception as e:
        logger.warning(f"[CONSULTATION] Failed to write memory refs: {e}")


# ── Consultation endpoint ─────────────────────────────────────────────────────

@router.post("/consultations", response_model=ConsultationResponse)
@limiter.limit("60/minute")
async def consult_graeae(request: Request, body: ConsultationRequest, user: UserContext = Depends(get_current_user)):
    """Consult GRAEAE multi-provider consensus engine.

    Creates a hash-chained audit entry and records any injected memories.
    Returns raw provider responses (full, best, or truncated per format param).
    """
    logger.info(
        f"[CONSULTATION] {user.user_id}: {body.task_type} "
        f"(limit_chars={body.limit_chars}, format={body.format})"
    )
    try:
        from graeae.engine import get_graeae_engine
        engine = get_graeae_engine()
        result = await engine.consult(body.prompt, body.task_type)

        if body.limit_chars and result.get("all_responses"):
            for provider, resp in result["all_responses"].items():
                if isinstance(resp.get("response_text"), str):
                    resp["response_text"] = resp["response_text"][:body.limit_chars]
                    resp["truncated"] = len(resp.get("response_text", "")) >= body.limit_chars

        if body.format == "best" and result.get("all_responses"):
            best = max(result["all_responses"].items(), key=lambda x: x[1].get("final_score", 0))
            result["all_responses"] = {best[0]: best[1]}

        consultation_id = None
        memory_ids = []
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
                        body.prompt,
                        body.task_type,
                        best_resp[1].get("response_text", "")[:500],
                        best_resp[1].get("final_score", 0),
                        best_resp[0],
                        0.02,
                        best_resp[1].get("latency_ms", 0),
                        body.mode or "auto",
                    )
                    consultation_id = row["id"] if row else None

                # Write hash-chained audit entry
                await _write_audit_entry(
                    pool=_lc._pool,
                    consultation_id=consultation_id,
                    prompt=body.prompt,
                    response=best_resp[1].get("response_text", ""),
                    task_type=body.task_type or "reasoning",
                    provider=best_resp[0],
                    quality_score=best_resp[1].get("final_score", 0),
                )
            except Exception as e:
                logger.warning(f"[CONSULTATION] Failed to log consultation: {e}")

        try:
            from api.webhook_dispatcher import dispatch as _dispatch_webhook
            if _lc._pool and consultation_id is not None:
                async with _lc._pool.acquire() as _wh_conn:
                    await _dispatch_webhook(_wh_conn, "consultation.completed", {
                        "consultation_id": str(consultation_id),
                        "task_type": body.task_type,
                        "winning_muse": result.get("winning_muse"),
                        "consensus_score": result.get("consensus_score"),
                        "owner_id": user.user_id,
                    }, owner_id=user.user_id)
        except Exception:
            logger.warning("webhook dispatch failed for consultation.completed %s", consultation_id, exc_info=True)

        return ConsultationResponse(
            consultation_id=consultation_id,
            all_responses=result.get("all_responses", {}),
            consensus_response=result.get("consensus_response"),
            consensus_score=result.get("consensus_score"),
            winning_muse=result.get("winning_muse"),
            cost=result.get("cost"),
            latency_ms=result.get("latency_ms"),
            mode=body.mode or "auto",
            timestamp=result.get("timestamp", ""),
        )

    except Exception as e:
        logger.error(f"[CONSULTATION] Error: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Consultation failed — see server logs for details")


# ── Audit log endpoints (declared before dynamic /{consultation_id} to prevent
#    'audit' string being matched as a UUID path param) ───────────────────────

@router.get("/consultations/audit", response_model=List[AuditLogEntry])
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


@router.get("/consultations/audit/verify", response_model=AuditVerifyResponse)
async def verify_audit_chain(
    user: UserContext = Depends(get_current_user),
):
    """Verify the integrity of the hash chain in the GRAEAE audit log.

    Walks the entire chain from genesis, verifying each link.
    Returns details of any broken sequences.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id "
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
            (prev_chain + row["prompt_hash"] + row["response_hash"]).encode()
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


# ── Dynamic /{consultation_id} routes (declared after static /audit above) ────

@router.get("/consultations/{consultation_id}")
async def get_consultation(
    consultation_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Retrieve a consultation by ID."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, prompt, task_type, consensus_response, consensus_score, "
            "winning_muse, cost, latency_ms, mode, created AS created_at "
            "FROM graeae_consultations WHERE id = $1",
            consultation_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail="Consultation not found")

    return {
        "id": str(row["id"]),
        "prompt": row["prompt"],
        "task_type": row["task_type"],
        "consensus_response": row["consensus_response"],
        "consensus_score": row["consensus_score"],
        "winning_muse": row["winning_muse"],
        "cost": row["cost"],
        "latency_ms": row["latency_ms"],
        "mode": row["mode"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/consultations/{consultation_id}/artifacts")
async def get_consultation_artifacts(
    consultation_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Retrieve structured outputs and citations from a consultation."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        # Get consultation
        consultation = await conn.fetchrow(
            "SELECT id, created AS created_at FROM graeae_consultations WHERE id = $1",
            consultation_id,
        )
        if not consultation:
            raise HTTPException(status_code=404, detail="Consultation not found")

        # Get referenced memories
        memory_refs = await conn.fetch(
            "SELECT memory_id, injected_at FROM consultation_memory_refs "
            "WHERE consultation_id = $1 ORDER BY injected_at",
            consultation_id,
        )

    return ConsultationArtifact(
        consultation_id=str(consultation["id"]),
        citations=[str(ref["memory_id"]) for ref in memory_refs],
        memory_refs=[
            {
                "memory_id": str(ref["memory_id"]),
                "injected_at": ref["injected_at"].isoformat(),
            }
            for ref in memory_refs
        ],
        created_at=consultation["created_at"].isoformat(),
    )


