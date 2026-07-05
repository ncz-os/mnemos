"""Health check and statistics endpoints."""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import mnemos.core.lifecycle as _lc
from mnemos._version import __version__ as _MNEMOS_VERSION
from mnemos.api.dependencies import require_root
from mnemos.core.config import get_settings
from mnemos.core.auth_context import UserContext
from mnemos.persistence.base import capability_details_for_backend
from mnemos.nats.client import publishing_enabled
from mnemos.domain.models import HealthResponse, StatsResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return health status including DB pool and background workers."""
    db_ok = False
    backend = _lc._persistence_backend
    backend_name = None
    backend_capabilities: list[str] = []
    backend_capability_details: list[str] = []
    if backend is not None:
        backend_name = type(backend).__name__
        backend_capabilities = sorted(str(cap) for cap in getattr(backend, "capabilities", set()))
        backend_capability_details = sorted(capability_details_for_backend(backend))
        db_ok = await backend.ping()

    # Get worker status
    worker_status = _lc._worker_status.get("distillation_worker", "unknown")

    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        database_connected=db_ok,
        version=_MNEMOS_VERSION,
        distillation_worker=worker_status,
        profile=get_settings().profile,
        nats_publishing_enabled=publishing_enabled(),
        persistence_backend=backend_name,
        persistence_capabilities=backend_capabilities,
        persistence_capability_details=backend_capability_details,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(_user: UserContext = Depends(require_root)) -> StatsResponse:
    """Get system statistics from database (cached 60 s)."""
    # Cache key versioned to invalidate after the federation-aware
    # native/federated split shipped in v3.4.x. Bumping the suffix
    # forces clients to recompute against the new schema.
    cache_key = "stats:global:v2"

    if _lc._cache:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug("[CACHE] /stats hit")
                return StatsResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] /stats read error: {e}")

    backend = _lc._persistence_backend
    if backend is None:
        raise HTTPException(status_code=503, detail="Persistence backend not available")

    try:
        async with backend.transactional() as tx:
            memory_stats = await backend.memories.gather_stats(tx)
            compression_stats = await backend.compression.gather_stats(tx)

        result = StatsResponse(
            total_memories=memory_stats.total_memories,
            native_memories=memory_stats.native_memories,
            federated_memories=memory_stats.federated_memories,
            memories_by_peer=memory_stats.memories_by_peer,
            total_compressions=compression_stats.total_compressions,
            average_compression_ratio=(
                round(compression_stats.average_compression_ratio, 2)
                if compression_stats.average_compression_ratio is not None
                else 0.57
            ),
            average_quality_rating=(
                int(memory_stats.avg_quality_rating) if memory_stats.avg_quality_rating is not None else 75
            ),
            memories_by_category=memory_stats.memories_by_category,
            memories_by_subcategory=memory_stats.memories_by_subcategory,
            unreviewed_compressions=compression_stats.unreviewed_compressions,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        )

        if _lc._cache:
            try:
                await _lc._cache.setex(cache_key, 60, result.model_dump_json())
            except Exception as e:
                logger.warning(f"[CACHE] /stats write error: {e}")

        return result

    except Exception as e:
        logger.error(f"Stats error: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Internal error: {e}")
