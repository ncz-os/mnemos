"""KNEMON usage ledger routes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.persistence.base import UsageLedgerRecord

router = APIRouter(prefix="/v1", tags=["ledger"])

_PLAN_WINDOWS: dict[tuple[str, str], tuple[str, int | None]] = {
    ("anthropic", "claude_max_200"): ("rolling", 18000),
    ("anthropic", "claude_max_100"): ("rolling", 18000),
    ("openai", "chatgpt_plus"): ("rolling", 18000),
    ("openai", "chatgpt_pro"): ("rolling", 18000),
    ("openai", "chatgpt_pro_100_codex_promo"): ("rolling", 18000),
    ("openai", "chatgpt_pro_100_codex"): ("rolling", 18000),
    ("openai", "chatgpt_pro_200_codex"): ("rolling", 18000),
    ("nvidia", "ngc_integrate"): ("monthly", None),
    ("nvidia", "ngc_inference"): ("monthly", None),
    ("groq", "dev_tier"): ("monthly", None),
    ("together", "api"): ("monthly", None),
    ("deepseek-direct", "api"): ("monthly", None),
    ("xai", "api"): ("monthly", None),
    ("xai", "supergrok"): ("monthly", None),
    ("gemini", "api"): ("monthly", None),
    ("perplexity", "api"): ("monthly", None),
}


def compute_plan_window_id(
    provider: str,
    plan_name: str,
    ts: datetime | None = None,
    *,
    reset_anchor: str | None = None,
    window_seconds: int | None = None,
) -> str:
    """Return the deterministic KNEMON plan-window id for a usage record."""
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    provider_key = provider.strip().lower()
    plan_key = plan_name.strip().lower()
    anchor, seconds = _PLAN_WINDOWS.get((provider_key, plan_key), ("monthly", None))
    anchor = (reset_anchor or anchor or "monthly").lower()
    seconds = int(window_seconds or seconds or 0)
    if anchor in {"daily", "day"}:
        return f"{provider_key}-{plan_key}-{ts:%Y-%m-%d}"
    if anchor in {"monthly", "month"}:
        return f"{provider_key}-{plan_key}-{ts:%Y-%m}"
    iso = ts.isocalendar()
    day_seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
    window_index = day_seconds // max(seconds, 1)
    return f"{provider_key}-{plan_key}-{iso.year}-W{iso.week:02d}-d{iso.weekday}-w{window_index}"


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
    path_kind: str | None = Field(default="api", max_length=64)


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

    record = UsageLedgerRecord(
        **payload.model_dump(),
        plan_window_id=compute_plan_window_id(payload.provider, payload.tier),
    )
    try:
        async with backend.transactional() as tx:
            result = await recorder(tx, record)
    except NotImplementedError:
        raise HTTPException(status_code=503, detail="usage_ledger requires a ledger-capable backend")

    return LedgerRecordResponse(id=result.id, est_cost_usd=result.est_cost_usd)
