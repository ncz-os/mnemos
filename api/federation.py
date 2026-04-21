"""Federation sync engine — pull memories from remote MNEMOS peers.

Pull model: each peer is a remote instance; we periodically fetch their
`/v1/federation/feed` endpoint with a Bearer token they issued us. Memories
are stored locally with id = `fed:{peer_name}:{remote_id}` and
`federation_source = peer_name`, dedupable on re-pull via the id + updated
timestamp.

Peers are configured via admin endpoints (api/handlers/federation.py). A
lifespan-owned worker iterates enabled peers on their individual sync
intervals.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx

logger = logging.getLogger(__name__)

FEDERATION_HTTP_TIMEOUT = 30.0
FEDERATION_BATCH_LIMIT = 100
FEDERATION_ID_PREFIX = "fed:"


# ── Pull + store ─────────────────────────────────────────────────────────────


async def sync_peer(
    pool: asyncpg.Pool,
    peer_id: str,
) -> Tuple[int, int, int]:
    """Run a full sync against one peer. Returns (pulled, new, updated)."""
    async with pool.acquire() as conn:
        peer = await conn.fetchrow(
            """
            SELECT id::text, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor
            FROM federation_peers WHERE id = $1::uuid
            """,
            peer_id,
        )
    if not peer:
        raise ValueError(f"peer {peer_id} not found")
    if not peer["enabled"]:
        logger.info("federation: peer %s disabled — skipping", peer["name"])
        return 0, 0, 0

    cursor_before = peer["last_sync_cursor"]

    async with pool.acquire() as conn:
        log_id = await conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id, cursor_before,
        )

    total_pulled = 0
    total_new = 0
    total_updated = 0
    cursor = cursor_before
    err: Optional[str] = None

    try:
        while True:
            batch, next_cursor, has_more = await _pull_batch(
                peer["base_url"], peer["auth_token"], cursor,
                peer["namespace_filter"], peer["category_filter"],
            )
            if not batch:
                break
            async with pool.acquire() as conn:
                new_n, upd_n = await _store_memories(conn, peer["name"], batch)
            total_pulled += len(batch)
            total_new += new_n
            total_updated += upd_n
            cursor = next_cursor
            if not has_more:
                break
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("federation: pull from %s failed", peer["name"])

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE federation_sync_log
            SET finished_at = NOW(),
                memories_pulled = $2,
                memories_new = $3,
                memories_updated = $4,
                error = $5,
                cursor_after = $6
            WHERE id = $1::uuid
            """,
            log_id, total_pulled, total_new, total_updated, err, cursor,
        )
        if err:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(), last_error = $2, last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id, err,
            )
        else:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(),
                    last_sync_cursor = $2,
                    last_error = NULL,
                    last_error_at = NULL,
                    total_pulled = total_pulled + $3
                WHERE id = $1::uuid
                """,
                peer_id, cursor, total_pulled,
            )

    logger.info(
        "federation: peer=%s pulled=%d new=%d updated=%d cursor=%s",
        peer["name"], total_pulled, total_new, total_updated, cursor,
    )
    return total_pulled, total_new, total_updated


async def _pull_batch(
    base_url: str,
    auth_token: str,
    since: Optional[datetime],
    namespace_filter: Optional[List[str]],
    category_filter: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], Optional[datetime], bool]:
    """HTTP GET one batch. Returns (memories, next_cursor, has_more)."""
    url = base_url.rstrip("/") + "/v1/federation/feed"
    params: Dict[str, Any] = {"limit": FEDERATION_BATCH_LIMIT}
    if since is not None:
        params["since"] = since.astimezone(timezone.utc).isoformat()
    if namespace_filter:
        params["namespace"] = ",".join(namespace_filter)
    if category_filter:
        params["category"] = ",".join(category_filter)

    headers = {"Authorization": f"Bearer {auth_token}"}

    async with httpx.AsyncClient(timeout=FEDERATION_HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("federation auth token rejected (401)")
        if r.status_code == 403:
            raise RuntimeError("federation auth insufficient role (403)")
        r.raise_for_status()
        body = r.json()

    memories = body.get("memories", []) or []
    next_cursor_raw = body.get("next_cursor")
    next_cursor = (
        datetime.fromisoformat(next_cursor_raw.replace("Z", "+00:00"))
        if next_cursor_raw else None
    )
    has_more = bool(body.get("has_more"))
    return memories, next_cursor, has_more


async def _store_memories(
    conn: asyncpg.Connection,
    peer_name: str,
    memories: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """Upsert a batch. Returns (newly_inserted, updated_existing)."""
    new_n = 0
    upd_n = 0
    for mem in memories:
        remote_id = mem.get("id")
        if not remote_id or not isinstance(remote_id, str):
            continue
        local_id = f"{FEDERATION_ID_PREFIX}{peer_name}:{remote_id}"
        remote_updated_raw = mem.get("updated") or mem.get("created")
        if remote_updated_raw:
            try:
                remote_updated = datetime.fromisoformat(
                    remote_updated_raw.replace("Z", "+00:00")
                )
            except ValueError:
                remote_updated = None
        else:
            remote_updated = None

        # Check existing
        existing = await conn.fetchrow(
            "SELECT federation_remote_updated FROM memories WHERE id = $1",
            local_id,
        )

        meta_raw = mem.get("metadata") or {}
        if isinstance(meta_raw, dict):
            meta_raw = {**meta_raw, "federation_remote_id": remote_id}
        else:
            meta_raw = {"federation_remote_id": remote_id}
        meta_json = json.dumps(meta_raw)

        if existing is None:
            await conn.execute(
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'federation', $8, 644,
                        $9, $10, $11, $12, $13, $14, NOW(), $14)
                """,
                local_id,
                mem.get("content", ""),
                mem.get("category", "federation"),
                mem.get("subcategory"),
                meta_json,
                mem.get("verbatim_content") or mem.get("content", ""),
                mem.get("quality_rating") or 75,
                mem.get("namespace", "default"),
                mem.get("source_model"),
                mem.get("source_provider"),
                mem.get("source_session"),
                mem.get("source_agent"),
                peer_name,
                remote_updated,
            )
            new_n += 1
        else:
            # Only update if the remote is newer.
            if (
                existing["federation_remote_updated"] is None
                or (remote_updated and remote_updated > existing["federation_remote_updated"])
            ):
                await conn.execute(
                    """
                    UPDATE memories SET
                      content = $2, category = $3, subcategory = $4,
                      metadata = $5::jsonb, verbatim_content = $6,
                      quality_rating = $7, namespace = $8,
                      federation_remote_updated = $9, updated = $9
                    WHERE id = $1
                    """,
                    local_id,
                    mem.get("content", ""),
                    mem.get("category", "federation"),
                    mem.get("subcategory"),
                    meta_json,
                    mem.get("verbatim_content") or mem.get("content", ""),
                    mem.get("quality_rating") or 75,
                    mem.get("namespace", "default"),
                    remote_updated,
                )
                upd_n += 1

    return new_n, upd_n


# ── Background worker ────────────────────────────────────────────────────────


async def federation_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: iterate enabled peers, sync those whose interval has elapsed.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    import asyncio

    logger.info("federation worker started")
    while True:
        try:
            await asyncio.sleep(60)  # check every minute
            async with pool.acquire() as conn:
                due = await conn.fetch(
                    """
                    SELECT id::text, name, sync_interval_secs, last_sync_at
                    FROM federation_peers
                    WHERE enabled
                      AND (last_sync_at IS NULL
                           OR last_sync_at + (sync_interval_secs || ' seconds')::interval <= NOW())
                    ORDER BY COALESCE(last_sync_at, 'epoch'::timestamptz)
                    LIMIT 10
                    """
                )
            for p in due:
                try:
                    await sync_peer(pool, p["id"])
                except Exception:
                    logger.exception("federation: sync failed for peer %s", p["name"])
        except asyncio.CancelledError:
            logger.info("federation worker cancelled")
            raise
        except Exception:  # pragma: no cover
            logger.exception("federation worker iteration failed")
