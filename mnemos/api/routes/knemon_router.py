"""KNEMON hybrid routing endpoint."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.domain.knemon.router import KnemonRouteRequest, NoModelAvailable, route

router = APIRouter(prefix="/v1/knemon", tags=["knemon"])


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get("/route")
async def route_knemon(
    task_kind: str = Query(..., min_length=1),
    priority: int = Query(..., ge=0),
    est_tokens_in: int = Query(..., ge=0),
    est_tokens_out: int = Query(..., ge=0),
    caller_session_id: str | None = Query(default=None, max_length=128),
    caller_subsystem: str = Query(default="api", min_length=1),
    exclude_providers: str | None = Query(default=None),
    require_capability: str | None = Query(default=None),
    _: UserContext = Depends(require_root),
):
    backend = backend_or_503()
    req = KnemonRouteRequest(
        task_kind=task_kind,
        priority=priority,
        est_tokens_in=est_tokens_in,
        est_tokens_out=est_tokens_out,
        caller_session_id=caller_session_id,
        caller_subsystem=caller_subsystem,
        exclude_providers=_csv(exclude_providers),
        require_capability=_csv(require_capability),
    )
    try:
        decision = await route(req, backend)
    except NoModelAvailable as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NotImplementedError:
        raise HTTPException(status_code=503, detail="KNEMON routing requires a registry-capable backend")
    return asdict(decision)
