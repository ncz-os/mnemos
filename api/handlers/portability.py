"""Memory Portability Format (MPF) export / import endpoints.

Reference implementation of `docs/MEMORY_EXPORT_FORMAT.md` v0.1.0.
Scope of this first cut:

  * GET  /v1/export — bundles the caller's memories into a single
    MPF envelope as `kind: memory` records. Non-root callers get
    only their own owner_id + namespace; root may pass query params
    to export any owner/namespace/category slice.

  * POST /v1/import — accepts an MPF envelope and upserts `kind: memory`
    records. Non-root rewrites every record's owner_id + namespace
    to the caller's identity (you can't smuggle other owners' rows
    in via an import). Root may pass `?preserve_owner=true` to
    honor the envelope's owner_id + namespace fields verbatim —
    useful for migrations between MNEMOS instances.

Deferred to later commits:

  * document / fact / event record kinds (MPF knows about them, this
    handler doesn't emit or consume them yet)
  * kg_triples, compression_manifest, memory_versions (DAG), and
    embeddings sidecars — each is a separate surface with its own
    round-trip rules
  * JSONL streaming for large corpora (single-file JSON only today;
    tight cap on `limit` keeps request bodies manageable)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["portability"])


# ─── Constants ────────────────────────────────────────────────────────────────

MPF_VERSION = "0.1.0"
MEMORY_PAYLOAD_VERSION = "mnemos-3.1"
SOURCE_SYSTEM = "mnemos"
SOURCE_VERSION = "3.2.0"

# Server-side export cap. Anything larger should use the streaming
# JSONL variant — not in this v3.2.0 cut. Prevents a pathological
# full-table export from pinning memory.
_EXPORT_HARD_LIMIT = 10_000


# ─── Pydantic models (wire shape) ────────────────────────────────────────────

class MPFRecord(BaseModel):
    """A single record in an MPF envelope. Discriminated union by `kind`."""

    id: str
    kind: str  # "document" | "memory" | "fact" | "event" (we only emit/accept "memory" today)
    payload_version: str
    payload: Dict[str, Any]


class MPFEnvelope(BaseModel):
    """An MPF v0.1.0 file envelope.

    Fields kept optional / additive so this endpoint can consume MPF
    files emitted by other tools (docling, Mem0, Letta) that may
    populate sidecars this handler doesn't process. Unknown record
    kinds are skipped per the spec's forward-compatibility rule.
    """

    mpf_version: str = MPF_VERSION
    source_system: Optional[str] = SOURCE_SYSTEM
    source_version: Optional[str] = SOURCE_VERSION
    source_instance: Optional[str] = None
    exported_at: Optional[str] = None
    record_count: Optional[int] = None
    records: List[MPFRecord] = Field(default_factory=list)


class ImportStats(BaseModel):
    """Summary of an import run."""

    imported: int
    skipped: int
    failed: int
    unsupported_kinds: Dict[str, int] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


def _memory_to_record(row) -> MPFRecord:
    """Shape a memories-row dict into an MPFRecord(kind='memory').

    The payload is the MNEMOS v3.1 native memory schema as-is
    (content + category + provenance + tenancy fields). An importer
    running against a different MNEMOS version keys off
    payload_version to decide what to do with it.
    """
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {"_raw": metadata}

    def _iso(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        return str(value)

    payload: Dict[str, Any] = {
        "content": row.get("content"),
        "category": row.get("category"),
        "subcategory": row.get("subcategory"),
        "created": _iso(row.get("created")),
        "updated": _iso(row.get("updated")),
        "owner_id": row.get("owner_id"),
        "namespace": row.get("namespace"),
        "permission_mode": row.get("permission_mode"),
        "quality_rating": row.get("quality_rating"),
        "source_model": row.get("source_model"),
        "source_provider": row.get("source_provider"),
        "source_session": row.get("source_session"),
        "source_agent": row.get("source_agent"),
        "metadata": metadata,
    }
    # Strip None entries to keep the envelope tidy — importers default
    # missing fields via the schema, and nulls on absent columns
    # inflate envelope size noticeably at 10k rows.
    payload = {k: v for k, v in payload.items() if v is not None}

    return MPFRecord(
        id=row["id"],
        kind="memory",
        payload_version=MEMORY_PAYLOAD_VERSION,
        payload=payload,
    )


# ─── GET /v1/export ───────────────────────────────────────────────────────────


@router.get("/export", response_model=MPFEnvelope)
async def export_memories(
    category: Optional[str] = Query(None, description="Filter by category; all categories if unset."),
    limit: int = Query(1000, ge=1, le=_EXPORT_HARD_LIMIT),
    offset: int = Query(0, ge=0),
    owner_id: Optional[str] = Query(None, description="Root only. Export a specific owner's memories; defaults to the caller."),
    namespace: Optional[str] = Query(None, description="Root only. Export a specific namespace; defaults to the caller's."),
    user: UserContext = Depends(get_current_user),
):
    """Export memories as an MPF v0.1.0 envelope.

    Non-root callers are scoped to their own owner_id + namespace,
    regardless of the query params. Root callers may target a specific
    owner/namespace slice for migration or support work.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    if _is_root(user):
        effective_owner = owner_id  # may be None = no filter
        effective_ns = namespace    # may be None = no filter
    else:
        # Non-root cannot exfiltrate outside their own tenancy. If the
        # caller passed owner/namespace params that don't match their
        # identity, reject loudly — silent narrowing would hide the
        # mistake.
        if owner_id and owner_id != user.user_id:
            raise HTTPException(status_code=403, detail="cross-owner export requires root")
        if namespace and namespace != user.namespace:
            raise HTTPException(status_code=403, detail="cross-namespace export requires root")
        effective_owner = user.user_id
        effective_ns = user.namespace

    conditions: List[str] = []
    params: List[Any] = []
    idx = 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = (
        "SELECT id, content, category, subcategory, created, updated, "
        "owner_id, namespace, permission_mode, quality_rating, "
        "source_model, source_provider, source_session, source_agent, "
        "metadata "
        "FROM memories "
        f"{where} "
        f"ORDER BY created ASC "
        f"LIMIT ${idx} OFFSET ${idx + 1}"
    )
    params.extend([limit, offset])

    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    records = [_memory_to_record(dict(r)) for r in rows]

    return MPFEnvelope(
        mpf_version=MPF_VERSION,
        source_system=SOURCE_SYSTEM,
        source_version=SOURCE_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        record_count=len(records),
        records=records,
    )


# ─── POST /v1/import ──────────────────────────────────────────────────────────


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse for the handful of timestamp fields MPF
    memory payloads carry. Returns None on any failure — the caller
    lets the DB default fire instead of inserting garbage."""
    if not value:
        return None
    try:
        # `fromisoformat` handles "2026-01-15T10:30:00+00:00" and its
        # bare variants. Strip a trailing Z since older pre-3.11
        # Python doesn't accept it (we're on 3.11+ but belt+braces).
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


@router.post("/import", response_model=ImportStats, status_code=200)
async def import_memories(
    envelope: MPFEnvelope = Body(..., description="An MPF v0.1 envelope."),
    preserve_owner: bool = Query(
        False,
        description=(
            "Root only. When true, honor the owner_id + namespace on "
            "each incoming record instead of rewriting to the caller's "
            "identity. Required for cross-tenant migrations; refused for "
            "non-root callers even if passed."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Import an MPF envelope. v0.1 cut accepts `kind: memory` records
    only; other kinds are counted under `unsupported_kinds` and skipped
    per the spec's forward-compatibility rule."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    if envelope.mpf_version != MPF_VERSION:
        # Strict minor/major enforcement — this cut implements exactly
        # 0.1.0. Newer minor versions that add record kinds without
        # changing existing ones should be fine to relax later.
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported MPF version {envelope.mpf_version!r}; expected {MPF_VERSION}",
        )

    if preserve_owner and not _is_root(user):
        raise HTTPException(
            status_code=403, detail="preserve_owner=true requires root",
        )

    stats = ImportStats(imported=0, skipped=0, failed=0, unsupported_kinds={}, errors=[])

    async with _lc._pool.acquire() as conn:
        async with conn.transaction():
            for record in envelope.records:
                if record.kind != "memory":
                    stats.unsupported_kinds[record.kind] = (
                        stats.unsupported_kinds.get(record.kind, 0) + 1
                    )
                    continue

                if record.payload_version != MEMORY_PAYLOAD_VERSION:
                    # Payload version mismatch isn't fatal — record the
                    # skip for operator visibility. Migrating payloads
                    # across versions is a follow-up commit.
                    stats.skipped += 1
                    stats.errors.append(
                        f"{record.id}: unsupported payload_version "
                        f"{record.payload_version!r}; expected {MEMORY_PAYLOAD_VERSION}"
                    )
                    continue

                p = record.payload

                if preserve_owner:
                    imported_owner = p.get("owner_id") or user.user_id
                    imported_ns = p.get("namespace") or user.namespace
                else:
                    imported_owner = user.user_id
                    imported_ns = user.namespace

                content = p.get("content")
                if not content or not str(content).strip():
                    stats.failed += 1
                    stats.errors.append(f"{record.id}: empty content; skipped")
                    continue

                category = p.get("category") or "imported"
                subcategory = p.get("subcategory")
                permission_mode = p.get("permission_mode") or 600
                metadata = p.get("metadata") or {}
                quality_rating = p.get("quality_rating") or 75

                # Use the envelope-provided id verbatim. ON CONFLICT DO NOTHING
                # gives us idempotent re-imports — running /v1/export followed
                # by /v1/import against the same DB is a no-op.
                try:
                    row = await conn.execute(
                        """
                        INSERT INTO memories (
                            id, content, category, subcategory, metadata,
                            quality_rating, owner_id, namespace, permission_mode,
                            source_model, source_provider, source_session, source_agent,
                            created, updated
                        )
                        VALUES (
                            $1, $2, $3, $4, $5::jsonb,
                            $6, $7, $8, $9,
                            $10, $11, $12, $13,
                            COALESCE($14, NOW()), COALESCE($15, NOW())
                        )
                        ON CONFLICT (id) DO NOTHING
                        """,
                        record.id, content, category, subcategory,
                        json.dumps(metadata),
                        quality_rating, imported_owner, imported_ns, permission_mode,
                        p.get("source_model"), p.get("source_provider"),
                        p.get("source_session"), p.get("source_agent"),
                        _parse_iso(p.get("created")),
                        _parse_iso(p.get("updated")),
                    )
                    if row == "INSERT 0 0":
                        stats.skipped += 1
                    else:
                        stats.imported += 1
                except Exception as exc:
                    stats.failed += 1
                    stats.errors.append(f"{record.id}: {type(exc).__name__}: {exc}")
                    logger.exception("MPF import failed for record %s", record.id)

    logger.info(
        "[MPF] import: user=%s imported=%d skipped=%d failed=%d unsupported=%s",
        user.user_id, stats.imported, stats.skipped, stats.failed, stats.unsupported_kinds,
    )
    return stats
