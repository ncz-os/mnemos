"""v6.2 M-2.2.4 admin endpoints for per-category temporal decay.

* ``GET /v1/admin/category_decay`` — list current table
* ``PUT /v1/admin/category_decay/{cat}`` — update half_life / kind / floor
* ``POST /v1/admin/category_decay/reseed`` — reset to schema defaults

All root-only; matches /v1/admin/sessions auth posture.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mnemos.api.dependencies import UserContext, require_root
from mnemos.api.persistence_helpers import backend_or_503 as _backend_or_503
from mnemos.domain.search.decay import (
    DEFAULT_CATEGORY,
    invalidate_decay_cache,
    load_decay_table,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/category_decay", tags=["admin"])


# Default seed rows (mirror migrations 0031_memory_category_decay.sql).
_DEFAULT_SEED: tuple[tuple[str, float, str, float], ...] = (
    ("feedback", 365, "exponential", 0.5),
    ("rules", 730, "exponential", 0.7),
    ("user", 365, "exponential", 0.6),
    ("reference", 180, "exponential", 0.3),
    ("project", 60, "exponential", 0.05),
    ("facts", 90, "exponential", 0.2),
    ("infrastructure", 30, "exponential", 0.1),
    ("credentials", 14, "sigmoid", 0.0),
    ("working", 7, "exponential", 0.0),
    (DEFAULT_CATEGORY, 180, "exponential", 0.1),
)


class DecayRowResponse(BaseModel):
    category: str
    half_life_days: float
    decay_kind: str
    floor: float


class DecayUpdateRequest(BaseModel):
    half_life_days: float = Field(..., gt=0)
    decay_kind: Literal["exponential", "sigmoid", "none"]
    floor: float = Field(..., ge=0.0, le=1.0)


@router.get("", response_model=list[DecayRowResponse])
async def list_category_decay(
    _: UserContext = Depends(require_root),
) -> list[DecayRowResponse]:
    """Return current memory_category_decay table contents."""
    backend = _backend_or_503()
    table = await load_decay_table(backend)
    return [
        DecayRowResponse(
            category=p.category,
            half_life_days=p.half_life_days,
            decay_kind=p.decay_kind,
            floor=p.floor,
        )
        for p in sorted(table.values(), key=lambda x: x.category)
    ]


@router.put("/{category}", response_model=DecayRowResponse)
async def update_category_decay(
    category: str,
    request: DecayUpdateRequest,
    _: UserContext = Depends(require_root),
) -> DecayRowResponse:
    """UPSERT one category row. Cache invalidated post-write."""
    if not category or len(category) > 64:
        raise HTTPException(
            status_code=400,
            detail="category must be 1-64 chars",
        )
    backend = _backend_or_503()
    await _upsert_category_row(
        backend,
        category=category,
        half_life_days=request.half_life_days,
        decay_kind=request.decay_kind,
        floor=request.floor,
    )
    invalidate_decay_cache()
    logger.info(
        "[decay-admin] PUT category=%s half_life=%.2f kind=%s floor=%.4f",
        category,
        request.half_life_days,
        request.decay_kind,
        request.floor,
    )
    return DecayRowResponse(
        category=category,
        half_life_days=request.half_life_days,
        decay_kind=request.decay_kind,
        floor=request.floor,
    )


@router.post("/reseed", response_model=list[DecayRowResponse])
async def reseed_category_decay(
    _: UserContext = Depends(require_root),
) -> list[DecayRowResponse]:
    """Reset all rows to schema defaults."""
    backend = _backend_or_503()
    for category, hl, kind, floor in _DEFAULT_SEED:
        await _upsert_category_row(
            backend,
            category=category,
            half_life_days=hl,
            decay_kind=kind,
            floor=floor,
        )
    invalidate_decay_cache()
    logger.info("[decay-admin] reseeded %d category rows", len(_DEFAULT_SEED))
    return [
        DecayRowResponse(
            category=cat,
            half_life_days=hl,
            decay_kind=kind,
            floor=floor,
        )
        for cat, hl, kind, floor in _DEFAULT_SEED
    ]


async def _upsert_category_row(
    backend,
    *,
    category: str,
    half_life_days: float,
    decay_kind: str,
    floor: float,
) -> None:
    """Backend-agnostic UPSERT for memory_category_decay.

    PG / SQLite: INSERT ... ON CONFLICT (category) DO UPDATE.
    Oracle / Db2: MERGE INTO ... ON ... WHEN MATCHED.
    """
    async with backend.transactional() as tx:
        conn_attr = getattr(tx, "conn", None)
        # Postgres
        if conn_attr is not None and hasattr(conn_attr, "fetch") and hasattr(conn_attr, "execute"):
            await conn_attr.execute(
                """
                INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (category) DO UPDATE SET
                    half_life_days = EXCLUDED.half_life_days,
                    decay_kind = EXCLUDED.decay_kind,
                    floor = EXCLUDED.floor
                """,
                category,
                half_life_days,
                decay_kind,
                floor,
            )
            return
        # SQLite
        if conn_attr is not None and "sqlite" in type(conn_attr).__module__:
            from mnemos.persistence.sqlite import _execute

            await _execute(
                conn_attr,
                """
                INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (category) DO UPDATE SET
                    half_life_days = excluded.half_life_days,
                    decay_kind = excluded.decay_kind,
                    floor = excluded.floor
                """,
                (category, half_life_days, decay_kind, floor),
            )
            return
        # Oracle / Db2
        if conn_attr is not None and hasattr(conn_attr, "cursor"):
            from mnemos.persistence.oracle import _call

            cursor = await _call(conn_attr.cursor)
            try:
                await _call(
                    cursor.execute,
                    """
                    MERGE INTO memory_category_decay tgt
                    USING (SELECT :category AS category FROM dual) src
                    ON (tgt.category = src.category)
                    WHEN MATCHED THEN UPDATE SET
                        half_life_days = :half_life_days,
                        decay_kind = :decay_kind,
                        floor = :floor
                    WHEN NOT MATCHED THEN
                        INSERT (category, half_life_days, decay_kind, floor)
                        VALUES (:category, :half_life_days, :decay_kind, :floor)
                    """,
                    {
                        "category": category,
                        "half_life_days": half_life_days,
                        "decay_kind": decay_kind,
                        "floor": floor,
                    },
                )
            finally:
                await _call(cursor.close)
            return
        raise RuntimeError(f"[decay-admin] unsupported tx shape {type(tx)!r}")
