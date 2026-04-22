"""Federation endpoints — /v1/federation/*.

Two halves:
  * Admin side (root only): register peers, inspect sync status, trigger manual sync.
  * Protocol side (federation role): the `/feed` endpoint that remote peers pull from.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user, require_root
from api import federation as _fed
from api.models import (
    FederationFeedResponse,
    FederationPeer,
    FederationPeerCreateRequest,
    FederationPeerListResponse,
    FederationPeerUpdateRequest,
    FederationStatusResponse,
    FederationSyncLogEntry,
    FederationSyncLogResponse,
    FederationSyncTriggerResponse,
    MemoryItem,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/federation", tags=["federation"])


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _require_federation_role(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Allow feed access for roles 'federation' or 'root'."""
    if user.role not in ("federation", "root"):
        raise HTTPException(status_code=403, detail="federation role required")
    return user


def _to_peer(row) -> FederationPeer:
    return FederationPeer(
        id=str(row["id"]),
        name=row["name"],
        base_url=row["base_url"],
        namespace_filter=list(row["namespace_filter"]) if row["namespace_filter"] else None,
        category_filter=list(row["category_filter"]) if row["category_filter"] else None,
        enabled=row["enabled"],
        sync_interval_secs=row["sync_interval_secs"],
        last_sync_at=row["last_sync_at"].isoformat() if row["last_sync_at"] else None,
        last_sync_cursor=row["last_sync_cursor"].isoformat() if row["last_sync_cursor"] else None,
        last_error=row["last_error"],
        last_error_at=row["last_error_at"].isoformat() if row["last_error_at"] else None,
        total_pulled=row["total_pulled"],
        created=row["created"].isoformat(),
        updated=row["updated"].isoformat(),
    )


# ── Admin: peer CRUD ─────────────────────────────────────────────────────────


@router.post("/peers", response_model=FederationPeer, status_code=201)
async def register_peer(
    request: FederationPeerCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Register a remote peer to pull from."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO federation_peers
              (name, base_url, auth_token, namespace_filter, category_filter,
               enabled, sync_interval_secs)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            request.name, request.base_url, request.auth_token,
            request.namespace_filter, request.category_filter,
            request.enabled, request.sync_interval_secs,
        )
    logger.info("federation: peer registered name=%s", request.name)
    return _to_peer(row)


@router.get("/peers", response_model=FederationPeerListResponse)
async def list_peers(_: UserContext = Depends(require_root)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_peers ORDER BY name")
    peers = [_to_peer(r) for r in rows]
    return FederationPeerListResponse(count=len(peers), peers=peers)


@router.get("/peers/{peer_id}", response_model=FederationPeer)
async def get_peer(peer_id: str, _: UserContext = Depends(require_root)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM federation_peers WHERE id = $1::uuid", peer_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="peer not found")
    return _to_peer(row)


@router.patch("/peers/{peer_id}", response_model=FederationPeer)
async def update_peer(
    peer_id: str,
    request: FederationPeerUpdateRequest,
    _: UserContext = Depends(require_root),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="no fields to update")
    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE federation_peers SET {', '.join(set_clauses)} "
            f"WHERE id=$1::uuid RETURNING *",
            peer_id, *updates.values(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="peer not found")
    return _to_peer(row)


@router.delete("/peers/{peer_id}", status_code=204)
async def delete_peer(peer_id: str, _: UserContext = Depends(require_root)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM federation_peers WHERE id = $1::uuid", peer_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="peer not found")


@router.post("/peers/{peer_id}/sync", response_model=FederationSyncTriggerResponse)
async def trigger_sync(
    peer_id: str,
    _: UserContext = Depends(require_root),
):
    """Run a sync against a peer right now (blocks on completion)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        pulled, new, updated = await _fed.sync_peer(_lc._pool, peer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FederationSyncTriggerResponse(
        pulled=pulled, new=new, updated=updated,
    )


@router.get("/peers/{peer_id}/log", response_model=FederationSyncLogResponse)
async def peer_sync_log(
    peer_id: str,
    _: UserContext = Depends(require_root),
    limit: int = 50,
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = $1::uuid
            ORDER BY started_at DESC
            LIMIT $2
            """,
            peer_id, limit,
        )
    entries = [
        FederationSyncLogEntry(
            id=r["id"],
            started_at=r["started_at"].isoformat(),
            finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
            memories_pulled=r["memories_pulled"],
            memories_new=r["memories_new"],
            memories_updated=r["memories_updated"],
            error=r["error"],
            cursor_before=r["cursor_before"].isoformat() if r["cursor_before"] else None,
            cursor_after=r["cursor_after"].isoformat() if r["cursor_after"] else None,
        )
        for r in rows
    ]
    return FederationSyncLogResponse(count=len(entries), entries=entries)


@router.get("/status", response_model=FederationStatusResponse)
async def federation_status(_: UserContext = Depends(require_root)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_peers ORDER BY name")
    peers = [_to_peer(r) for r in rows]
    return FederationStatusResponse(
        count=len(peers),
        enabled_count=sum(1 for p in peers if p.enabled),
        error_count=sum(1 for p in peers if p.last_error),
        peers=peers,
    )


# ── Protocol: serving peers pulling from us ──────────────────────────────────


@router.get("/feed", response_model=FederationFeedResponse)
async def federation_feed(
    request: Request,
    _: UserContext = Depends(_require_federation_role),
    since: Optional[str] = Query(None, description="ISO 8601 timestamp"),
    namespace: Optional[str] = Query(None, description="Comma-separated namespace filter"),
    category: Optional[str] = Query(None, description="Comma-separated category filter"),
    limit: int = Query(100, ge=1, le=500),
):
    """Serve memories for a remote peer to pull. Requires role='federation' or 'root'."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    since_ts: Optional[datetime] = None
    if since:
        try:
            since_ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="since must be ISO 8601")

    namespaces = [s.strip() for s in namespace.split(",") if s.strip()] if namespace else []
    categories = [s.strip() for s in category.split(",") if s.strip()] if category else []

    # Exclude memories we ourselves pulled from another federation (no loops).
    query_parts = ["federation_source IS NULL"]
    args: list = []
    if since_ts is not None:
        args.append(since_ts)
        query_parts.append(f"updated > ${len(args)}")
    if namespaces:
        args.append(namespaces)
        query_parts.append(f"namespace = ANY(${len(args)})")
    if categories:
        args.append(categories)
        query_parts.append(f"category = ANY(${len(args)})")

    args.append(limit + 1)   # request one extra to detect has_more
    where_clause = " AND ".join(query_parts)

    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated
            FROM memories
            WHERE {where_clause}
            ORDER BY updated
            LIMIT ${len(args)}
            """,
            *args,
        )

    has_more = len(rows) > limit
    rows = rows[:limit]

    memories = [
        MemoryItem(
            id=r["id"],
            content=r["content"],
            category=r["category"],
            subcategory=r["subcategory"],
            created=r["created"].isoformat(),
            updated=r["updated"].isoformat() if r["updated"] else None,
            metadata=dict(r["metadata"]) if r["metadata"] else None,
            quality_rating=r["quality_rating"],
            verbatim_content=r["verbatim_content"],
            owner_id=r["owner_id"],
            namespace=r["namespace"],
            permission_mode=r["permission_mode"],
            source_model=r["source_model"],
            source_provider=r["source_provider"],
            source_session=r["source_session"],
            source_agent=r["source_agent"],
        )
        for r in rows
    ]
    next_cursor = rows[-1]["updated"].isoformat() if rows and rows[-1]["updated"] else None

    return FederationFeedResponse(
        memories=memories,
        next_cursor=next_cursor,
        has_more=has_more,
    )
