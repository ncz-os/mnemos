"""MNEMOS Model Registry API endpoints.

MNEMOS is the authoritative model registry: daily provider API sync keeps
model_registry current; Arena.ai rankings decorate models with quality scores.

Endpoints:
  GET  /model-registry/                 list all models (paginated, filterable)
  GET  /model-registry/providers        list available providers + last sync time
  GET  /model-registry/{provider}       list models for one provider
  GET  /model-registry/best             top model per provider by graeae_weight
  POST /model-registry/sync             trigger on-demand provider sync
  GET  /model-registry/stats            summary statistics
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/model-registry", tags=["model-registry"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class ModelEntry(BaseModel):
    provider: str
    model_id: str
    display_name: Optional[str] = None
    family: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    capabilities: List[str] = []
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None
    available: bool
    deprecated: bool
    arena_score: Optional[float] = None
    arena_rank: Optional[int] = None
    graeae_weight: Optional[float] = None
    first_seen: str
    last_seen: str
    last_synced: str


class ProviderSummary(BaseModel):
    provider: str
    total_models: int
    available_models: int
    last_synced: Optional[str] = None
    last_sync_error: Optional[str] = None


class SyncRequest(BaseModel):
    providers: Optional[List[str]] = None  # None = all
    dry_run: bool = False


class SyncResult(BaseModel):
    provider: str
    models_found: int
    models_added: int
    models_updated: int
    models_deprecated: int
    error: Optional[str] = None
    duration_ms: int
    dry_run: bool


class RegistryStats(BaseModel):
    total_models: int
    available_models: int
    providers_tracked: int
    models_with_arena_scores: int
    last_sync: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_model_entry(r) -> ModelEntry:
    return ModelEntry(
        provider=r["provider"],
        model_id=r["model_id"],
        display_name=r.get("display_name"),
        family=r.get("family"),
        context_window=r.get("context_window"),
        max_output_tokens=r.get("max_output_tokens"),
        capabilities=r.get("capabilities") or [],
        input_cost_per_mtok=float(r["input_cost_per_mtok"]) if r.get("input_cost_per_mtok") is not None else None,
        output_cost_per_mtok=float(r["output_cost_per_mtok"]) if r.get("output_cost_per_mtok") is not None else None,
        available=r["available"],
        deprecated=r["deprecated"],
        arena_score=float(r["arena_score"]) if r.get("arena_score") is not None else None,
        arena_rank=r.get("arena_rank"),
        graeae_weight=float(r["graeae_weight"]) if r.get("graeae_weight") is not None else None,
        first_seen=r["first_seen"].isoformat(),
        last_seen=r["last_seen"].isoformat(),
        last_synced=r["last_synced"].isoformat(),
    )


def _require_pool():
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _lc._pool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ModelEntry])
async def list_models(
    provider: Optional[str] = Query(None, description="Filter by provider"),
    available_only: bool = Query(True, description="Only return available (non-deprecated) models"),
    capability: Optional[str] = Query(None, description="Filter by capability (chat, vision, reasoning, …)"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    """List models from the registry, optionally filtered."""
    pool = _require_pool()

    conditions = []
    params: list = []
    idx = 1

    if provider:
        conditions.append(f"provider = ${idx}")
        params.append(provider)
        idx += 1

    if available_only:
        conditions.append("available = TRUE AND deprecated = FALSE")

    if capability:
        conditions.append(f"${idx} = ANY(capabilities)")
        params.append(capability)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    params += [limit, offset]
    query = f"""
        SELECT provider, model_id, display_name, family,
               context_window, max_output_tokens, capabilities,
               input_cost_per_mtok, output_cost_per_mtok,
               available, deprecated, arena_score, arena_rank, graeae_weight,
               first_seen, last_seen, last_synced
        FROM model_registry
        {where}
        ORDER BY graeae_weight DESC NULLS LAST, arena_score DESC NULLS LAST, provider, model_id
        LIMIT ${idx} OFFSET ${idx+1}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [_row_to_model_entry(r) for r in rows]


@router.get("/providers", response_model=List[ProviderSummary])
async def list_providers(
    user: UserContext = Depends(get_current_user),
):
    """List all tracked providers with model counts and last sync time."""
    pool = _require_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                mr.provider,
                COUNT(*) FILTER (WHERE mr.available AND NOT mr.deprecated) AS available_models,
                COUNT(*) AS total_models,
                MAX(mr.last_synced) AS last_synced
            FROM model_registry mr
            GROUP BY mr.provider
            ORDER BY mr.provider
        """)

        # Pull last error per provider from sync log
        sync_rows = await conn.fetch("""
            SELECT DISTINCT ON (provider) provider, error
            FROM model_registry_sync_log
            ORDER BY provider, synced_at DESC
        """)

    error_map = {r["provider"]: r["error"] for r in sync_rows}

    return [
        ProviderSummary(
            provider=r["provider"],
            total_models=r["total_models"],
            available_models=r["available_models"],
            last_synced=r["last_synced"].isoformat() if r["last_synced"] else None,
            last_sync_error=error_map.get(r["provider"]),
        )
        for r in rows
    ]


@router.get("/best", response_model=List[ModelEntry])
async def best_per_provider(
    user: UserContext = Depends(get_current_user),
):
    """Return the top model per provider by graeae_weight (ties broken by arena_score)."""
    pool = _require_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (provider)
                provider, model_id, display_name, family,
                context_window, max_output_tokens, capabilities,
                input_cost_per_mtok, output_cost_per_mtok,
                available, deprecated, arena_score, arena_rank, graeae_weight,
                first_seen, last_seen, last_synced
            FROM model_registry
            WHERE available = TRUE AND deprecated = FALSE
            ORDER BY provider,
                     graeae_weight DESC NULLS LAST,
                     arena_score   DESC NULLS LAST
        """)

    return [_row_to_model_entry(r) for r in rows]


@router.get("/stats", response_model=RegistryStats)
async def registry_stats(
    user: UserContext = Depends(get_current_user),
):
    """Return summary statistics for the model registry."""
    pool = _require_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*)                                  AS total_models,
                COUNT(*) FILTER (WHERE available)         AS available_models,
                COUNT(DISTINCT provider)                  AS providers_tracked,
                COUNT(*) FILTER (WHERE arena_score IS NOT NULL) AS models_with_arena_scores,
                MAX(last_synced)                          AS last_sync
            FROM model_registry
        """)

    return RegistryStats(
        total_models=row["total_models"],
        available_models=row["available_models"],
        providers_tracked=row["providers_tracked"],
        models_with_arena_scores=row["models_with_arena_scores"],
        last_sync=row["last_sync"].isoformat() if row["last_sync"] else None,
    )


@router.get("/{provider}", response_model=List[ModelEntry])
async def list_provider_models(
    provider: str,
    available_only: bool = Query(True),
    user: UserContext = Depends(get_current_user),
):
    """List all models for a specific provider."""
    pool = _require_pool()

    conditions = ["provider = $1"]
    params: list = [provider]

    if available_only:
        conditions.append("available = TRUE AND deprecated = FALSE")

    where = "WHERE " + " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT provider, model_id, display_name, family,
                       context_window, max_output_tokens, capabilities,
                       input_cost_per_mtok, output_cost_per_mtok,
                       available, deprecated, arena_score, arena_rank, graeae_weight,
                       first_seen, last_seen, last_synced
                FROM model_registry {where}
                ORDER BY graeae_weight DESC NULLS LAST, arena_score DESC NULLS LAST, model_id""",
            *params,
        )

    if not rows and available_only:
        # Check if provider exists at all
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM model_registry WHERE provider = $1 LIMIT 1", provider
            )
        if not exists:
            raise HTTPException(status_code=404, detail=f"Provider {provider!r} not found in registry")

    return [_row_to_model_entry(r) for r in rows]


@router.post("/sync", response_model=List[SyncResult])
async def trigger_sync(
    request: SyncRequest,
    user: UserContext = Depends(get_current_user),
):
    """Trigger an on-demand provider model sync.

    Pass providers=null to sync all providers.
    Pass dry_run=true to preview without writing.
    """
    pool = _require_pool()

    try:
        from graeae.provider_sync import sync_all_providers
        results = await sync_all_providers(
            pool=pool if not request.dry_run else None,
            dry_run=request.dry_run,
            providers=request.providers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"[REGISTRY] sync error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Sync failed — see server logs")

    return [
        SyncResult(
            provider=r["provider"],
            models_found=r["models_found"],
            models_added=r["models_added"],
            models_updated=r["models_updated"],
            models_deprecated=r["models_deprecated"],
            error=r.get("error"),
            duration_ms=r["duration_ms"],
            dry_run=r["dry_run"],
        )
        for r in results
    ]
