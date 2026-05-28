"""KNEMON usage ledger routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.core.plan_windows import compute_plan_window_id, plan_path_kind
from mnemos.persistence.base import UsageLedgerRecord

router = APIRouter(prefix="/v1", tags=["ledger"])


class LedgerRecordRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    task_kind: str = Field(..., min_length=1)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    tokens_reasoning: int = Field(default=0, ge=0)
    latency_ms: int = Field(..., ge=0)
    outcome: Literal["ok", "err", "timeout"]
    caller_subsystem: str = Field(..., min_length=1)
    tier: str = Field(..., min_length=1)
    session_id: str | None = Field(default=None, max_length=64)
    request_count: int = Field(default=1, ge=1)
    path_kind: str | None = Field(default=None, max_length=64)


class LedgerRecordResponse(BaseModel):
    id: int
    est_cost_usd: Decimal


@router.post("/ledger", response_model=LedgerRecordResponse)
async def record_ledger_usage(
    payload: LedgerRecordRequest,
    _: UserContext = Depends(require_root),
) -> LedgerRecordResponse:
    backend = backend_or_503()
    recorder = getattr(backend, "record_usage_ledger", None)
    if recorder is None:
        raise HTTPException(status_code=503, detail="usage_ledger requires a ledger-capable backend")

    record_payload = payload.model_dump()
    record_payload["path_kind"] = plan_path_kind(payload.provider, payload.tier, payload.path_kind)
    record = UsageLedgerRecord(
        **record_payload,
        plan_window_id=compute_plan_window_id(payload.provider, payload.tier),
    )
    try:
        async with backend.transactional() as tx:
            result = await recorder(tx, record)
    except NotImplementedError:
        raise HTTPException(status_code=503, detail="usage_ledger requires a ledger-capable backend")

    return LedgerRecordResponse(id=result.id, est_cost_usd=result.est_cost_usd)
