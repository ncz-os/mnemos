"""Memory CRUD, search, and rehydration endpoints."""

import asyncio
import inspect
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

import mnemos.core.lifecycle as _lc
from mnemos.api.content_negotiation import negotiate_narrate_format
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import (
    backend_or_503 as _backend_or_503,
    maybe_set_pg_rls as _maybe_set_pg_rls,
    require_postgres_pool_or_503,
)
from mnemos.core.config import runtime_env_value_stripped
from mnemos.core.ids import new_memory_id
from mnemos.core.extras import is_extra_installed, missing_extra_detail
from mnemos.core.lifecycle import (
    _get_cache_key,
    _get_embedding,
)
from mnemos.core.security import is_root
from mnemos.core.visibility import handle_trigger_pgerror
from mnemos.audit import write_audit_entry
from mnemos.domain.search import SearchProfile, apply_decay, get_reranker, load_decay_table, resolve_profile
from mnemos.domain.artemis_dedup import (
    duplicate_content_error_body,
    evaluate_memory_create_dedup,
)
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.persistence.base import DuplicateMemoryError
from mnemos.persistence.nats_events import safe_subject_segment
from mnemos.domain.models import (
    DEFAULT_SEMANTIC_FLOOR,
    DEFAULT_SEMANTIC_MARGIN_FLOOR,
    METRIC_COSINE_DISTANCE,
    SEMANTIC_SCORE_KEY,
    is_ood_result_set,
    BulkCreateRequest,
    BulkCreateResponse,
    MemoryCreateRequest,
    MemoryItem,
    MemoryListRequest,
    MemoryListResponse,
    MemorySearchRequest,
    MemoryUpdateRequest,
    RehydrationRequest,
    RehydrationResponse,
    normalize_similarity,
    row_to_memory as _row_to_memory,
    score_to_similarity,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["memories"])


@dataclass
class _BulkCreateCandidate:
    index: int
    memory: MemoryCreateRequest
    memory_id: str
    verbatim: str
    owner_id: str
    namespace: str
    metadata: dict
    permission_mode: int
    embedding_content: str
    embedding: list[float] | None = None


async def _write_memory_mutation_audit_entry(
    backend,
    tx,
    *,
    op: str,
    memory_id: str,
    content: str,
    category: str,
    subcategory: str | None,
    metadata: dict | None,
    writer_id: str,
) -> None:
    """Append a route-side audit-chain entry for one memory mutation.

    This keeps the single create/update/delete and bulk-create paths on the
    same helper so new write paths do not accidentally bypass the Ed25519
    per-memory chain. The helper intentionally preserves the legacy API
    behavior: when audit is disabled, unsupported by the backend, or missing
    a session secret, the data write still commits.
    """
    from mnemos.core.config import get_settings as _get_settings
    from mnemos.workers.audit_sealer import audit_chain_enabled as _ace

    if not _ace():
        return
    _settings = _get_settings()
    _session_secret = (getattr(_settings.server, "session_secret", "") or "").encode("utf-8")
    if not _session_secret:
        logger.warning(
            "[%s] MNEMOS_AUDIT_CHAIN=on but session_secret is empty; skipping audit write",
            op,
        )
        return
    await write_audit_entry(
        backend,
        tx,
        op=op,
        memory_id_str=memory_id,
        content=content,
        category=category,
        subcategory=subcategory,
        metadata=metadata,
        embedding=None,
        writer_id=writer_id,
        session_secret=_session_secret,
    )


def _metadata_for_audit(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except Exception:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    try:
        return dict(value)
    except Exception:
        return None


def effective_semantic_floor() -> float:
    """Default semantic relevance floor applied when a caller does not
    pass min_score. Operator-overridable fleet-wide via
    MNEMOS_SEMANTIC_FLOOR (set 0.0 to restore legacy top-k-nearest
    behavior for clients that pre-date the floor). Clamped to [0, 1].
    Invalid values fall back to DEFAULT_SEMANTIC_FLOOR.
    """
    raw = runtime_env_value_stripped("MNEMOS_SEMANTIC_FLOOR")
    if raw is None or raw == "":
        return DEFAULT_SEMANTIC_FLOOR
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("[VECTOR] invalid MNEMOS_SEMANTIC_FLOOR=%r; using default %.2f", raw, DEFAULT_SEMANTIC_FLOOR)
        return DEFAULT_SEMANTIC_FLOOR
    # Reject NaN/inf — float() accepts them but the clamp would silently
    # turn them into 1.0/0.0, contradicting "invalid -> default".
    if not math.isfinite(val):
        logger.warning("[VECTOR] non-finite MNEMOS_SEMANTIC_FLOOR=%r; using default %.2f", raw, DEFAULT_SEMANTIC_FLOOR)
        return DEFAULT_SEMANTIC_FLOOR
    return max(0.0, min(1.0, val))


def effective_semantic_margin_floor() -> float:
    """Default MARGIN floor for the OOD (nonsense-query) gate, applied when a
    caller does not pass min_margin. Operator-overridable fleet-wide via
    MNEMOS_SEMANTIC_MARGIN_FLOOR (set 0.0 to DISABLE the margin/anchor gate
    and keep the absolute-floor behavior only). Invalid / non-finite values
    fall back to DEFAULT_SEMANTIC_MARGIN_FLOOR. Negative clamps to 0 (disabled).
    """
    raw = runtime_env_value_stripped("MNEMOS_SEMANTIC_MARGIN_FLOOR")
    if raw is None or raw == "":
        return DEFAULT_SEMANTIC_MARGIN_FLOOR
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[VECTOR] invalid MNEMOS_SEMANTIC_MARGIN_FLOOR=%r; using default %.3f",
            raw,
            DEFAULT_SEMANTIC_MARGIN_FLOOR,
        )
        return DEFAULT_SEMANTIC_MARGIN_FLOOR
    if not math.isfinite(val):
        logger.warning(
            "[VECTOR] non-finite MNEMOS_SEMANTIC_MARGIN_FLOOR=%r; using default %.3f",
            raw,
            DEFAULT_SEMANTIC_MARGIN_FLOOR,
        )
        return DEFAULT_SEMANTIC_MARGIN_FLOOR
    # Clamp to [0, 1] to match the request field's bounds; a stray large
    # env value would otherwise empty nearly every unanchored semantic set
    # (ngc-review 2026-06-13). 0.0 = gate disabled.
    return max(0.0, min(1.0, val))


def _resolve_margin_floor(request_min_margin) -> float:
    """Single source of truth for the OOD margin floor actually applied to a
    request: a per-request override if finite & in range, else the env/default.
    Returned value is finite and clamped to [0, 1]. Used for BOTH the cache key
    and the gate execution so a cached result and a fresh recompute always gate
    identically, even if an internal caller passes an out-of-range/non-finite
    min_margin that bypasses request-field validation (ngc-review 2026-06-13).
    """
    if request_min_margin is not None:
        try:
            v = float(request_min_margin)
        except (TypeError, ValueError):
            v = None
        if v is not None and math.isfinite(v):
            return max(0.0, min(1.0, v))
    return effective_semantic_margin_floor()


def ood_gate_enabled() -> bool:
    """Master on/off for the OOD (nonsense-query) margin+anchor gate. Defaults
    ON (UAT-mandated, recall-first: empirically 0 false-negatives). Operators
    flip it OFF fleet-wide with MNEMOS_SEMANTIC_OOD_GATE in
    {0,false,no,off} (case-insensitive). Independent of, and ANDed with, the
    per-request/min_margin opt-out (min_margin=0.0 also disables it for one
    call). Exposed as a named flag so the behavior change is explicit and
    reversible without a code change (ngc-review 2026-06-13).

    ROLLOUT DECISION: default ON is UAT-mandated and empirically safe (0
    false-negatives over 15 genuine queries incl. paraphrases; the gate only
    drops a result set that is BOTH unanchored — after morphological stemming
    — AND flat-scored). The named flag + per-request min_margin=0.0 are the
    rollback levers; set MNEMOS_SEMANTIC_OOD_GATE=0 to restore pre-gate recall
    instantly. Residual risk: a genuine zero-lexical-overlap paraphrase with a
    flat distribution; none was observed in tuning.
    """
    raw = runtime_env_value_stripped("MNEMOS_SEMANTIC_OOD_GATE")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


NatsPublishIntent = tuple[str, dict, str]


def _log_search_phase(trace_id: str, started_at: float, phase: str) -> None:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info("[search:%s] %s done in %dms", trace_id, phase, elapsed_ms)


@asynccontextmanager
async def _rls_context(conn, user: UserContext):
    """Set PostgreSQL session variables for RLS when auth is active.

    Uses ``SELECT set_config(name, $1, true)`` rather than
    ``SET LOCAL <name> = $1`` because Postgres SET syntax does not
    accept bind parameters (the value position must be a literal —
    https://www.postgresql.org/docs/current/sql-set.html). The third
    argument ``true`` makes the binding transaction-local, equivalent
    to SET LOCAL. Same shape as ``maybe_set_pg_rls`` in
    ``mnemos.api.persistence_helpers`` so the two RLS context paths
    cannot drift.
    """
    if _lc._rls_enabled and user.authenticated:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('mnemos.current_user_id', $1, true)",
                user.user_id,
            )
            await conn.execute(
                "SELECT set_config('mnemos.current_role', $1, true)",
                user.role,
            )
            yield conn
    else:
        yield conn


def _validate_permission_mode(value: int | None, *, default: int | None = None) -> int | None:
    """Validate Unix-style octal permission digits stored as an integer."""
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(status_code=422, detail="permission_mode must be an integer")
    if value < 0 or value > 777 or any(digit not in "01234567" for digit in str(value)):
        raise HTTPException(status_code=422, detail="permission_mode must be octal-style 0-777")
    return value


async def _assert_no_active_deletion(
    backend,
    *,
    owner_id: str,
    namespace: str,
) -> None:
    """Refuse to write a row onto a scope that is currently being deleted.

    Without this fence, a memory added during the 30-day grace window
    (after the soft-delete sweep has marked the scope, before the
    hard-delete phase runs) would survive past ``hard_deleted`` -- the
    hard-delete only removes rows already carrying
    ``deleted_at IS NOT NULL``, so a row inserted during grace still has
    ``deleted_at IS NULL`` and is skipped forever, even though the
    audit log claims the user was hard-deleted. Root is the exception:
    operators may need to inject tombstone rows even mid-deletion.
    """
    from mnemos.persistence.worker_lifecycle import active_deletion_for_scope

    try:
        active = await active_deletion_for_scope(
            backend, target_user_id=owner_id, target_namespace=namespace
        )
    except Exception:
        # If the fence itself errors (e.g. backend doesn't expose
        # transactional), fall through. The hard-delete resweep+verify
        # loop added in deletion_request_worker.py is the second line of
        # defence and refuses to mark the request complete on live rows.
        return
    if active is not None:
        restore_by = active.get("restore_by")
        detail = (
            "This scope is currently subject to an active deletion request; "
            "new writes are rejected until the grace window expires. "
        )
        if restore_by is not None:
            detail += f"Restore deadline: {restore_by}."
        raise HTTPException(status_code=409, detail=detail)


def _should_redact_secrets(user: UserContext, *, include_secrets: bool = False, namespace: str | None = None) -> bool:
    """Whether to mask credential spans for this read (redact-at-retrieval).

    Privileged (full content, no masking) ONLY when the caller is root
    AND explicitly opted in (include_secrets=True) or targeted the vault
    namespace. Everything else — every non-root caller, and root on the
    default path — gets credential spans masked ``[REDACTED]`` before the
    content leaves the server. This is the redact-at-retrieval gate that
    backstops a vaulted-miss and masks incidental spans (release-blocking
    2026-06-13).
    """
    if not is_root(user):
        return True
    privileged = bool(include_secrets) or namespace == VAULT_NAMESPACE
    return not privileged


def _content_redacted_for_embedding(content: str, classified) -> str:
    """Return ``content`` with secret spans masked, for embedding (F2, 2026-06-28).

    Adversarial review F2: embeddings must not encode raw secret text. The
    ingest classifier records spans per field (authoritative at ingest);
    reuse them here rather than recomputing, so the embedding is masked
    consistently with what was classified. Content with no spans is
    returned unchanged (no recall cost for clean memories).
    """
    from mnemos.core.secret_detection import redact

    spans = (getattr(classified, "redact_fields", None) or {}).get("content") or []
    if not spans:
        return content
    return redact(content, spans)


def _redacted_for_webhook(content: str, metadata) -> str:
    """Span-redact ``content`` for outbound webhook payloads (F3, 2026-06-28).

    Webhook payloads leave the server to an external HTTP endpoint; raw
    secret content must not leak there (a non-root REST read would mask
    it, and the receiver may feed the payload to an LLM). Use the stored
    ingest spans when available, else recompute, matching the
    redact-at-retrieval contract.
    """
    from mnemos.core.secret_detection import redact_field_with_stored

    return redact_field_with_stored(content, metadata, "content")


async def _get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Embed a request batch through the process embedder."""
    from mnemos.runtime.embedder import get_embedder

    return await get_embedder().embed_batch(texts)


async def _execute_tx_sql(tx, sql: str) -> bool:
    conn = getattr(tx, "conn", None)
    if conn is None:
        return False
    execute = getattr(conn, "execute", None)
    if callable(execute):
        result = execute(sql)
        if inspect.isawaitable(result):
            await result
        return True
    cursor_factory = getattr(conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    cursor_cm = cursor_factory()
    if hasattr(cursor_cm, "__aenter__"):
        async with cursor_cm as cursor:
            result = cursor.execute(sql)
            if inspect.isawaitable(result):
                await result
        return True
    cursor = cursor_cm
    try:
        result = cursor.execute(sql)
        if inspect.isawaitable(result):
            await result
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
    return True


@asynccontextmanager
async def _bulk_item_savepoint(tx, index: int):
    name = f"mnemos_bulk_item_{index}"
    enabled = await _execute_tx_sql(tx, f"SAVEPOINT {name}")
    can_release = enabled and "mnemos.persistence.oracle" not in type(tx).__module__
    try:
        yield
    except BaseException:
        if enabled:
            await _execute_tx_sql(tx, f"ROLLBACK TO SAVEPOINT {name}")
        if can_release:
            await _execute_tx_sql(tx, f"RELEASE SAVEPOINT {name}")
        raise
    else:
        if can_release:
            await _execute_tx_sql(tx, f"RELEASE SAVEPOINT {name}")


def _should_redact_secrets_for_row(user: UserContext, row) -> bool:
    """Row-aware redact gate for the GET-by-id explicit-fetch escape hatch.

    Root fetching the exact id of a VAULT-namespace row gets full content
    (the deliberate credential-fetch path); root fetching a NON-vault row by
    id is still redacted so an ordinary root read can't leak an incidental
    credential span (ngc-review 2026-06-13). Non-root always redacts.
    """
    if not is_root(user):
        return True
    row_ns = row.get("namespace") if hasattr(row, "get") else None
    return row_ns != VAULT_NAMESPACE


def _should_frame_data(user, *, operational: bool = False) -> bool:
    """Whether to apply untrusted-data framing + injection quarantine.

    Prompt-injection defense (release-gate 2026-06-13). Retrieved memories
    are framed as untrusted reference DATA and AI-targeting injection
    meta-instructions are quarantined on EVERY read path by default, so a
    malicious stored memory cannot steer a consuming agent. The ONLY way to
    get verbatim, unframed content is the explicit operational opt-in --
    and, like ``include_secrets``, that opt-in is root-only. A non-root
    caller is ALWAYS framed regardless of the flag.
    """
    if not is_root(user):
        return True
    return not bool(operational)


def _read_visibility_for(user: UserContext, *, namespace: str) -> VisibilityFilter:
    """Read-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are pinned. Use when the
    handler has already pinned the namespace explicitly (e.g. on
    create/update) so the same-namespace round-trip doesn't reject.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_read(user, namespace=namespace)


def _vault_inclusive_read_visibility_for(user: UserContext, *, namespace: str | None = None) -> VisibilityFilter:
    """Root/vault-inclusive visibility for post-write response re-fetches.

    Auto-vaulting moves a just-written row from the caller namespace to
    ``vault``. The normal default read filter intentionally subtracts the
    vault, so a same-transaction response re-fetch would return None and crash
    or roll back. Post-write re-fetches are not enumeration; they target the
    exact id that was just authorized and written, so use root-bypass without
    the default vault subtraction to return the row reliably on every backend.
    """
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
        exclude_namespaces=(),
    )


def _mutation_visibility_for(user: UserContext, *, namespace: str | None) -> VisibilityFilter:
    """Mutation-path visibility for an already-resolved namespace.

    Root callers bypass; non-root callers are owner+namespace pinned.
    """
    if is_root(user):
        return VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=namespace,
        )
    return VisibilityFilter.for_mutation(user, namespace=namespace)


def _schedule_outbox_deliveries(delivery_ids: list[str]) -> None:
    """Schedule HTTP send attempts for newly-enqueued outbox rows.

    Called AFTER the writing transaction commits so the delivery
    worker sees a committed row when it runs. ``_attempt_delivery``
    is imported lazily to avoid pulling the webhook subsystem into
    edge-profile cold paths.
    """
    if not delivery_ids:
        return
    from mnemos.webhooks.sender import _attempt_delivery

    for did in delivery_ids:
        _lc._schedule_delivery_attempt(_attempt_delivery(str(did)))


async def _publish_nats_with_timeout(
    subject: str,
    payload: dict,
    *,
    msg_id: str,
) -> None:
    from mnemos.core.config import get_settings
    from mnemos.nats import publish_event as _nats_publish_event

    timeout = float(get_settings().nats.publish_timeout_seconds)
    try:
        await asyncio.wait_for(
            _nats_publish_event(subject, payload, msg_id=msg_id),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "NATS publish timed out after %.3fs for %s; scheduling retry",
            timeout,
            subject,
        )
        try:
            _lc._schedule_background(_nats_publish_event(subject, payload, msg_id=msg_id))
        except RuntimeError as exc:
            logger.warning("NATS publish retry scheduling failed for %s: %s", subject, exc)


async def _invalidate_caches_after_mutation() -> None:
    """Drop /stats + per-user search cache entries on any memory write.

    Also bumps the visibility epoch so that in-flight search writes land
    under the old epoch (orphaned) rather than leaking stale visibility into
    the new epoch — closing the write-after-invalidate (TOCTOU) window
    (mnemos-#<issue>).
    """
    if not _lc._cache:
        return
    try:
        await _lc._cache.delete("stats:global:v2")
        try:
            async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                await _lc._cache.delete(_k)
        except Exception:
            pass
    except Exception:
        pass
    try:
        await _lc._vis_epoch_get_incr()  # bump; errors silently
    except Exception:
        pass


def _row_archived_at(row) -> object | None:
    try:
        return row.get("archived_at")
    except AttributeError:
        try:
            return row["archived_at"]
        except (KeyError, IndexError, TypeError):
            return None


def _is_memory_owner(row, user: UserContext) -> bool:
    try:
        owner_id = row.get("owner_id")
    except AttributeError:
        try:
            owner_id = row["owner_id"]
        except (KeyError, IndexError, TypeError):
            owner_id = None
    return owner_id == user.user_id


# #183: removed `_read_visibility_predicate` adapter — defined but
# never called. Callers either pull `read_visibility_predicate`
# directly from mnemos.core.visibility or use one of the
# `_listing_visibility_for` / `_mutation_visibility_for` helpers.


async def _insert_memory_with_created_webhook(
    *,
    conn,
    mem_id: str,
    content: str,
    category: str,
    subcategory: Optional[str] = None,
    metadata: Optional[dict] = None,
    owner_id: str,
    namespace: str,
    permission_mode: int = 600,
    verbatim_content: Optional[str] = None,
    source_model: Optional[str] = None,
    source_provider: Optional[str] = None,
    source_session: Optional[str] = None,
    source_agent: Optional[str] = None,
    audit_writer_id: Optional[str] = None,
):
    """Insert a canonical memory row and enqueue memory.created in the same txn."""
    verbatim = verbatim_content if verbatim_content is not None else content
    await conn.execute(
        "INSERT INTO memories "
        "(id, content, category, subcategory, metadata, quality_rating, verbatim_content, "
        "owner_id, namespace, permission_mode, "
        "source_model, source_provider, source_session, source_agent) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)",
        mem_id,
        content,
        category,
        subcategory,
        json.dumps(metadata or {}),
        verbatim,
        owner_id,
        namespace,
        permission_mode,
        source_model,
        source_provider,
        source_session,
        source_agent,
    )
    if audit_writer_id:
        try:
            backend = _lc.get_persistence_backend()
            tx = getattr(conn, "_mnemos_transaction", None)
            if tx is None:
                from mnemos.persistence.postgres import PostgresTransaction

                tx = PostgresTransaction(conn)
            await _write_memory_mutation_audit_entry(
                backend,
                tx,
                op="create",
                memory_id=mem_id,
                content=content,
                category=category,
                subcategory=subcategory,
                metadata=metadata,
                writer_id=audit_writer_id,
            )
        except Exception:
            logger.exception("[ingest/create] audit-chain write failed for memory %s", mem_id)

    event_payload = {
        "memory_id": mem_id,
        "category": category,
        "subcategory": subcategory,
        "content": _redacted_for_webhook(content, metadata),
        "owner_id": owner_id,
        "namespace": namespace,
    }

    from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook

    await _dispatch_webhook(
        "memory.created",
        event_payload,
        conn=conn,
        owner_id=owner_id,
        namespace=namespace,
    )

    from mnemos.nats.client import get_node_name as _nats_get_node_name

    safe_ns = safe_subject_segment(namespace)
    nats_intents: list[NatsPublishIntent] = [
        (
            f"mnemos.memory.created.{safe_ns}",
            {
                "memory_id": mem_id,
                "namespace": namespace,
                "category": category,
                "source_node": _nats_get_node_name(),
            },
            f"{mem_id}.created",
        )
    ]

    return nats_intents


async def _bump_recall_counters(memory_ids: list) -> None:
    """Increment recall_count + set last_recalled_at for a hit set.

    Called fire-and-forget after a search returns its response, so
    counter updates don't add latency to the user-visible search path.
    Failures log and swallow — recall counters are observability, not
    user-content correctness.

    Single UPDATE for the whole hit set, so search hits with N memories
    cost one DB round-trip not N.
    """
    if not memory_ids:
        return
    try:
        backend = _backend_or_503()
        async with backend.transactional() as tx:
            await tx.conn.execute(
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = now() "
                "WHERE id = ANY($1::text[]) AND deleted_at IS NULL AND archived_at IS NULL",
                list(memory_ids),
            )
    except Exception as e:
        logger.warning(f"[RECALL] bump failed for {len(memory_ids)} ids: {e}")


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    namespace: Optional[str] = None,
    include_archived: bool = False,
    exclude_superseded: bool = False,
    current_only: bool = False,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    operational: bool = False,
    user: UserContext = Depends(get_current_user),
):
    backend = _backend_or_503()
    # Cross-namespace request rejected explicitly for non-root —
    # don't silently scope and hide rows. Root callers may pass any
    # namespace for cross-tenant audit lookups.
    root = is_root(user)
    if not root and namespace and namespace != user.namespace:
        raise HTTPException(
            status_code=403,
            detail="cross-namespace list requires root",
        )
    if operational and not root:
        raise HTTPException(
            status_code=403,
            detail="operational (verbatim, unframed) recall requires root",
        )
    if include_archived and not root:
        raise HTTPException(
            status_code=403,
            detail="include_archived requires root",
        )
    effective_namespace = namespace if root else user.namespace
    # Vault DISCOVERY (2026-06-19): root/trusted listing surfaces vault
    # rows tagged ``vaulted`` so an agent can find credential-class
    # memories. Content stays redacted here (``_should_redact_secrets``
    # below still returns True on the default path); the secret value is
    # fetched with get_memory(id). Non-root callers never see the vault
    # (include_vault is gated on root inside the factory).
    visibility = VisibilityFilter.for_read(
        user,
        namespace=effective_namespace,
        include_vault=root,
    )
    # When invoked directly (internal callers, unit tests) rather than through
    # FastAPI's dependency resolution, limit/offset arrive as the Query(...)
    # sentinels declared as their defaults. Fall back to those defaults so
    # MemoryListRequest still validates (FastAPI always passes real ints).
    if not isinstance(limit, int):
        limit = 20
    if not isinstance(offset, int):
        offset = 0
    list_request = MemoryListRequest(
        category=category,
        subcategory=subcategory,
        namespace=namespace,
        include_archived=include_archived,
        exclude_superseded=exclude_superseded,
        current_only=current_only,
        limit=limit,
        offset=offset,
        operational=operational,
    )
    exclude_superseded_effective = bool(list_request.exclude_superseded or list_request.current_only)

    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)
        rows, total = await backend.memories.list_memories(
            tx,
            visibility=visibility,
            category=category,
            subcategory=subcategory,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
            exclude_superseded=exclude_superseded_effective,
        )
    redact = _should_redact_secrets(user, namespace=effective_namespace)
    frame = _should_frame_data(user, operational=operational)
    return MemoryListResponse(
        count=total,
        memories=[_row_to_memory(r, redact_secrets=redact, frame_data=frame) for r in rows],
    )


@router.get("/memories/{memory_id}", response_model=MemoryItem)
async def get_memory(
    memory_id: str,
    request: Request,
    include_archived: bool = False,
    restore: bool = False,
    operational: bool = False,
    user: UserContext = Depends(get_current_user),
):
    """Fetch a memory by id.

    Content-negotiation surface (Accept header):
      * default / ``application/json`` / ``*/*`` — returns the JSON
        ``MemoryItem`` (existing behaviour, unchanged).
      * ``text/plain`` — returns the prose narration body, framed as
        untrusted retrieved DATA.
      * ``application/x-apollo-dense`` — returns the winning variant
        content (APOLLO dense form) framed as untrusted retrieved DATA.

    All representations honour the same ``VisibilityFilter.for_read``
    read contract — owner, federated, world-readable, and group-
    readable memories are returned identically across Accept values,
    so a memory the caller could read as JSON cannot 404 under
    ``Accept: text/plain``. ``Vary: Accept`` is set on every
    representation so caches keyed on URL alone never replay a JSON
    body to a text/plain caller (or vice-versa).
    """
    accept = request.headers.get("accept", "") if request else ""
    narrate_format = negotiate_narrate_format(accept)

    backend = _backend_or_503()
    if operational and not is_root(user):
        raise HTTPException(
            status_code=403,
            detail="operational (verbatim, unframed) recall requires root",
        )
    # Root callers see everything (namespace=None); non-root callers
    # are pinned to their namespace by the visibility factory. 404
    # (not 403) keeps other-tenant memory existence invisible — same
    # contract as the legacy handler.
    visibility = VisibilityFilter.for_read(
        user,
        namespace=None if is_root(user) else user.namespace,
        # GET-by-id is an EXPLICIT fetch: for ROOT callers, knowing the
        # exact memory id is itself the opt-in, so the vault namespace
        # is NOT excluded — fleet agents fetch a credential they hold
        # the id for without an include_secrets flag. (Search/list/
        # rehydrate DO exclude it for everyone because those enumerate.)
        # Secret vault (release-blocking 2026-06-13): for NON-ROOT
        # callers the vault MUST stay excluded so a non-root user who
        # knows/guesses a vault memory id cannot fetch it — the row is
        # filtered out and the handler returns 404 (not-found), keeping
        # vault-row existence invisible. Vault GET-by-id is therefore
        # root-only by construction.
        include_secrets=is_root(user),
    )
    body: Optional[str] = None
    row = None
    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)
        row = await backend.memories.get_memory(
            tx,
            memory_id,
            visibility=visibility,
            include_archived=True,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        archived_at = _row_archived_at(row)
        if archived_at is not None:
            if restore:
                if not (is_root(user) or _is_memory_owner(row, user)):
                    raise HTTPException(
                        status_code=403,
                        detail="restore requires root or memory owner",
                    )
            elif include_archived:
                from fastapi.encoders import jsonable_encoder

                return JSONResponse(
                    content=jsonable_encoder(
                        _row_to_memory(
                            row,
                            include_compressed=True,
                            redact_secrets=_should_redact_secrets_for_row(user, row),
                            frame_data=_should_frame_data(user, operational=operational),
                        )
                    ),
                    headers={"Vary": "Accept"},
                )
            else:
                archived_at_text = archived_at.isoformat() if hasattr(archived_at, "isoformat") else str(archived_at)
                return JSONResponse(
                    status_code=410,
                    content={
                        "archived": True,
                        "archived_at": archived_at_text,
                        "restore_endpoint": f"/admin/persephone/restore/{memory_id}",
                    },
                    headers={"Vary": "Accept"},
                )
        # Variant lookup must run inside the same transaction as the
        # memory fetch so a SQLite backend (single shared connection)
        # sees a consistent view, and so the visibility-gated row and
        # the variant we narrate from it cannot drift across a
        # concurrent compression-write boundary.
        if narrate_format is not None:
            from mnemos.api.routes.narrate import build_narration_body

            body = await build_narration_body(
                backend,
                tx,
                row,
                narrate_format,
            )

    if restore:
        if not is_extra_installed("persephone"):
            raise HTTPException(
                status_code=503,
                detail=missing_extra_detail("persephone", label="PERSEPHONE"),
            )
        from mnemos.domain.persephone.runner import restore_memory as _restore_archived_memory

        pool = require_postgres_pool_or_503(route_label="GET /v1/memories/{memory_id}?restore=true")
        try:
            async with pool.acquire() as conn:
                await _restore_archived_memory(conn, memory_id, user.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _invalidate_caches_after_mutation()
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            row = await backend.memories.get_memory(
                tx,
                memory_id,
                visibility=visibility,
                include_archived=False,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Memory not found after restore")
            if narrate_format is not None:
                from mnemos.api.routes.narrate import build_narration_body

                body = await build_narration_body(
                    backend,
                    tx,
                    row,
                    narrate_format,
                )

    # Vary: Accept on every successful representation. Unifies cache
    # behaviour even when the negotiated branch was not taken (a
    # JSON-first response cached without Vary could otherwise be
    # replayed to a later text/plain caller). Setting on the JSONResponse
    # path requires building it explicitly so the header is on the
    # serialised response — relying on FastAPI's response_model would
    # bypass our header injection.
    if narrate_format is not None:
        media_type = "text/plain" if narrate_format == "prose" else "application/x-apollo-dense"
        narrated_body = body or ""
        # GET-by-id narrate escape hatch, narrowed to VAULT rows: root
        # narrating the exact id of a vault memory gets full content; root
        # narrating a non-vault memory still has incidental spans masked;
        # non-root always redacts (ngc-review 2026-06-13).
        if _should_redact_secrets_for_row(user, row):
            from mnemos.core.secret_detection import redact_content

            narrated_body = redact_content(narrated_body)
        # Content-negotiated prose/dense is an agent-facing retrieval path
        # just like JSON get/list/search. Frame it as untrusted DATA unless
        # root explicitly requested operational verbatim recall.
        if _should_frame_data(user, operational=operational):
            from mnemos.core.injection_defense import defend as _defend_untrusted

            narrated_body = _defend_untrusted(narrated_body)
        return PlainTextResponse(
            narrated_body,
            media_type=media_type,
            headers={"Vary": "Accept"},
        )

    from fastapi.encoders import jsonable_encoder

    # GET-by-id is the ROOT explicit-fetch escape hatch, but narrowed to
    # VAULT rows only (ngc-review 2026-06-13): root fetching the exact id of
    # a VAULT memory gets full content (fleet agents fetch a credential they
    # hold the id for — task contract "Fleet/root explicit … GET-by-id still
    # gets full content"). Root fetching a NON-vault memory by id still has
    # any incidental credential span masked — an ordinary root read must not
    # leak an accidental span. Non-root never reaches a vault row (-> 404)
    # and is always redacted.
    memory_item = _row_to_memory(
        row,
        include_compressed=True,
        redact_secrets=_should_redact_secrets_for_row(user, row),
        frame_data=_should_frame_data(user, operational=operational),
    )
    return JSONResponse(
        content=jsonable_encoder(memory_item),
        headers={"Vary": "Accept"},
    )


def _render_content_preview(content: Optional[str], include_content: bool) -> Optional[str]:
    """Full content when the caller asked for it, first-200-chars preview
    otherwise. Returning None stays None — the engine produced no output."""
    if content is None:
        return None
    if include_content:
        return content
    return content if len(content) <= 200 else content[:200] + "…"


@router.get("/memories/{memory_id}/compression-manifests")
async def get_compression_manifests(
    memory_id: str,
    include_content: bool = Query(
        False,
        description=(
            "Return full compressed_content for the winning variant and "
            "every candidate. Default returns a 200-character preview to "
            "keep responses small; flip for deep audit inspection."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Return the v3.1 compression audit trail for a memory.

    Two sections:
      * `variant`  — the current winning dense form (or null if no contest
                     has produced a winner yet). Pointer into the contest
                     candidate that "won" most recently.
      * `contests` — every historical contest, grouped by contest_id,
                     ordered most recent first. Each contest lists every
                     engine attempt with scoring fields and reject_reason.

    The response shape mirrors the v3.1 compression schema exactly so
    operators can reason about what was tried, what scored how, and why
    each engine was or wasn't picked.
    """
    pool = require_postgres_pool_or_503(route_label="GET /v1/memories/{memory_id}/compression-manifests")

    async with pool.acquire() as conn:
        async with _rls_context(conn, user):
            # Enforce memory visibility — check owner + namespace for
            # non-root so manifests for cross-tenant memories don't
            # leak their existence. RLS (when enabled) scopes owner_id
            # but never namespace; the app-layer filter here is
            # defense-in-depth for the RLS-disabled case too.
            if is_root(user):
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories WHERE id = $1 AND deleted_at IS NULL",
                    memory_id,
                )
            else:
                exists = await conn.fetchval(
                    "SELECT 1 FROM memories WHERE id = $1 AND owner_id = $2 AND namespace = $3 AND deleted_at IS NULL",
                    memory_id,
                    user.user_id,
                    user.namespace,
                )
            if not exists:
                raise HTTPException(status_code=404, detail="Memory not found")

            variant_row = await conn.fetchrow(
                """
                SELECT engine_id, engine_version, compressed_content,
                       compressed_tokens, compression_ratio, quality_score,
                       composite_score, scoring_profile, judge_model,
                       selected_at, winner_candidate_id
                FROM memory_compressed_variants
                WHERE memory_id = $1
                """,
                memory_id,
            )

            candidate_rows = await conn.fetch(
                """
                SELECT contest_id, engine_id, engine_version,
                       compressed_content, original_tokens, compressed_tokens,
                       compression_ratio, quality_score, speed_factor,
                       composite_score, scoring_profile, elapsed_ms,
                       judge_model, gpu_used, is_winner, reject_reason,
                       manifest, created
                FROM memory_compression_candidates
                WHERE memory_id = $1
                ORDER BY created ASC, is_winner DESC, engine_id
                """,
                memory_id,
            )

    variant: Optional[dict] = None
    if variant_row is not None:
        variant = {
            "engine_id": variant_row["engine_id"],
            "engine_version": variant_row["engine_version"],
            "compressed_content": _render_content_preview(
                variant_row["compressed_content"],
                include_content,
            ),
            "compressed_tokens": variant_row["compressed_tokens"],
            "compression_ratio": variant_row["compression_ratio"],
            "quality_score": variant_row["quality_score"],
            "composite_score": variant_row["composite_score"],
            "scoring_profile": variant_row["scoring_profile"],
            "judge_model": variant_row["judge_model"],
            "selected_at": (variant_row["selected_at"].isoformat() if variant_row["selected_at"] else None),
            "winner_candidate_id": (
                str(variant_row["winner_candidate_id"]) if variant_row["winner_candidate_id"] else None
            ),
        }

    contests: dict[str, dict] = {}
    for row in candidate_rows:
        cid = str(row["contest_id"])
        bucket = contests.setdefault(
            cid,
            {
                "contest_id": cid,
                "started_at": row["created"],
                "candidates": [],
            },
        )
        # earliest created row's timestamp represents the contest start
        if row["created"] < bucket["started_at"]:
            bucket["started_at"] = row["created"]

        manifest_field = row["manifest"]
        if isinstance(manifest_field, str):
            try:
                manifest_field = json.loads(manifest_field)
            except Exception:
                manifest_field = {"_raw": manifest_field}

        bucket["candidates"].append(
            {
                "engine_id": row["engine_id"],
                "engine_version": row["engine_version"],
                "compressed_content": _render_content_preview(
                    row["compressed_content"],
                    include_content,
                ),
                "original_tokens": row["original_tokens"],
                "compressed_tokens": row["compressed_tokens"],
                "compression_ratio": row["compression_ratio"],
                "quality_score": row["quality_score"],
                "speed_factor": row["speed_factor"],
                "composite_score": row["composite_score"],
                "scoring_profile": row["scoring_profile"],
                "elapsed_ms": row["elapsed_ms"],
                "judge_model": row["judge_model"],
                "gpu_used": row["gpu_used"],
                "is_winner": row["is_winner"],
                "reject_reason": row["reject_reason"],
                "manifest": manifest_field,
                "created": row["created"].isoformat(),
            }
        )

    contests_list = sorted(
        ({**bucket, "started_at": bucket["started_at"].isoformat()} for bucket in contests.values()),
        key=lambda c: c["started_at"],
        reverse=True,
    )

    return {
        "memory_id": memory_id,
        "variant": variant,
        "contests": contests_list,
    }


@router.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(
    request: MemorySearchRequest,
    user: UserContext = Depends(get_current_user),
):
    """Search memories with optional 5-minute response caching."""
    # Secret vault (release-blocking 2026-06-13): include_secrets is
    # root-only and MUST be enforced BEFORE any cache-key construction,
    # cache read, or cache write. The cache key encodes
    # include_secrets into a distinct (secret-inclusive) bucket; if a
    # non-root caller were allowed past this point they could read — or
    # populate — the root-only secret bucket via that key. Rejecting up
    # front guarantees the secret-inclusive cache bucket is reachable
    # only by root. (Duplicated visibility-factory call below stays
    # defense-in-depth.)
    if request.operational and not is_root(user):
        raise HTTPException(
            status_code=403,
            detail="operational (verbatim, unframed) recall requires root",
        )
    if request.include_secrets and not is_root(user):
        raise HTTPException(
            status_code=403,
            detail="include_secrets requires root",
        )
    search_trace_id = uuid4().hex[:8]
    search_started_at = time.monotonic()
    # v6.2 M-2.2.3: validate retrieval profile (unknown → 400). Default
    # = balanced (current behavior); deep enables cross-encoder rerank.
    try:
        search_profile = resolve_profile(request.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Per-profile limit cap (spec § Design table): fast=25, balanced=100,
    # deep=200. Server-side hard cap of 500 still applies on top.
    _profile_caps = {
        SearchProfile.FAST: 25,
        SearchProfile.BALANCED: 100,
        SearchProfile.DEEP: 200,
    }
    request_limit = min(request.limit, _profile_caps[search_profile], 500)
    _log_search_phase(search_trace_id, search_started_at, "parse")

    # v3.1.2 Tier 3: pin owner_id + namespace to the caller's identity
    # for non-root searches. Previously request.namespace was caller-
    # controlled (a non-root user could search any namespace) and
    # owner_id was never passed at all. Root callers may pass any
    # namespace / owner to support cross-tenant audit.
    if is_root(user):
        search_owner_id = None  # no owner filter for root
        search_namespace = request.namespace  # honor caller's request
    else:
        if request.include_archived:
            raise HTTPException(
                status_code=403,
                detail="include_archived requires root",
            )
        search_owner_id = user.user_id
        # If the caller asked for a different namespace than theirs,
        # reject explicitly — don't silently scope and hide rows.
        if request.namespace and request.namespace != user.namespace:
            raise HTTPException(
                status_code=403,
                detail="cross-namespace search requires root",
            )
        search_namespace = user.namespace

    # Cache key MUST include user.user_id and the EFFECTIVE namespace +
    # owner_id — the server-resolved filter values, not the caller's
    # raw request. Using request.namespace (possibly None) would create
    # duplicate cache entries for identical result sets.
    # Cache key must include the caller's group_ids — search visibility
    # now depends on group membership (slice 2.1), so caching by
    # user_id alone would either leak rows after a group revoke or
    # hide rows after a group grant for the cache TTL window.
    #
    # Round-8 fix: pass RAW values (no `or ""` truthy-coalescing).
    # The query helpers distinguish None (no SQL predicate) from ""
    # (predicate with empty value); collapsing both to "" before
    # serialization aliases distinct semantics. JSON encoding inside
    # _get_cache_key now preserves None as null vs "" as "" so the
    # digest reflects the request's actual filter shape.
    # v6.3 TOCTOU guard: fold a monotonic visibility epoch into the cache key
    # so an in-flight cache write after a bump lands under the old epoch
    # (orphaned, never read).  Bump is triggered by every visibility-narrowing
    # mutation (delete, archive, ACL revoke, permission-mode tighten).
    try:
        _epoch = await _lc._vis_epoch_current()
    except Exception:
        _epoch = 0
    cache_key = _get_cache_key(
        "search",
        user.user_id,
        user.namespace,
        request.query,
        request_limit,
        request.category,
        request.subcategory,
        "semantic" if request.semantic else "fts",
        request.source_provider,
        request.source_model,
        request.source_agent,
        search_namespace,
        search_owner_id,
        bool(request.include_secrets),  # vault opt-in -> distinct cache bucket
        bool(request.operational),  # framing opt-in -> distinct (unframed) cache bucket
        sorted(user.group_ids),  # list, not pre-serialized string
        request.include_archived,
        bool(request.exclude_superseded),
        bool(request.current_only),
        request.boost_recency,
        request.recency_weight,
        search_profile.value,  # v6.2 M-2.2.3: distinct cache per profile
        # UAT 2026-06-13: floor changes the result set. Resolve to the
        # EFFECTIVE clamped floor (caller min_score or the env/default)
        # BEFORE keying so equivalent floors (e.g. 2.0 and 1.0, or -1.0
        # and 0.0) share one cache entry instead of fragmenting it.
        (max(0.0, min(1.0, float(request.min_score))) if request.min_score is not None else effective_semantic_floor()),
        _resolve_margin_floor(request.min_margin),
        # Master OOD-gate flag participates in the key: toggling
        # MNEMOS_SEMANTIC_OOD_GATE must NOT serve a stale gated/ungated
        # result from before the flip (ngc-review 2026-06-13).
        ood_gate_enabled(),
        _epoch,  # v6.3 TOCTOU guard — epoch at read time
    )

    if _lc._cache and not request.include_compressed:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug(f"[CACHE] /memories/search hit for '{request.query[:30]}'")
                _log_search_phase(search_trace_id, search_started_at, "serialize")
                return MemoryListResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] search read error: {e}")

    backend = _backend_or_503()
    semantic_boosted_order = False
    # Root callers can search across namespaces (search_owner_id is
    # None); non-root callers are pinned. The visibility factory
    # rejects namespace=None for non-root, which the namespace 403
    # check above already prevents reaching.
    #
    # Secret vault (release-blocking 2026-06-13): the DEFAULT path
    # subtracts the vault namespace even for root, so credential-class
    # memories never surface in normal/public/phone search. Fleet
    # agents opt in with include_secrets=true OR by targeting
    # namespace="vault" explicitly. include_secrets is root-only and is
    # rejected at the TOP of this handler (before cache-key build) so a
    # non-root caller can never touch the secret-inclusive cache bucket.
    # The visibility factory below independently clears the vault
    # exclusion only for root callers (defense-in-depth).
    visibility = VisibilityFilter.for_read(
        user,
        namespace=search_namespace,
        include_secrets=bool(request.include_secrets),
        # Vault DISCOVERY (2026-06-19): root/trusted search surfaces vault
        # rows tagged ``vaulted`` (content still redacted unless the
        # caller also passes include_secrets, the retrieve opt-in). Gated
        # on root inside the factory; non-root never sees the vault.
        include_vault=is_root(user),
    )

    async with backend.transactional() as tx:
        await _maybe_set_pg_rls(tx, user)

        async def _fts_fallback() -> list:
            return await backend.memories.fts_search(
                tx,
                query=request.query,
                limit=request_limit,
                visibility=visibility,
                category=request.category,
                subcategory=request.subcategory,
                source_provider=request.source_provider,
                source_model=request.source_model,
                source_agent=request.source_agent,
                include_archived=bool(request.include_archived),
                exclude_superseded=bool(request.exclude_superseded or request.current_only),
            )

        if request.semantic:
            embedding = await _get_embedding(request.query)
            _log_search_phase(search_trace_id, search_started_at, "embed")
            if not embedding:
                logger.warning("[VECTOR] Embedding failed, falling back to FTS")
                rows = await _fts_fallback()
            else:
                logger.info(f"[VECTOR] Semantic search: {len(embedding)}-dim vector")
                semantic_trace_kwargs = {}
                if getattr(backend, "supports_pgvector", False):
                    semantic_trace_kwargs = {
                        "search_trace_id": search_trace_id,
                        "search_started_at": search_started_at,
                    }
                semantic_failed = False
                try:
                    rows = await backend.memories.semantic_search(
                        tx,
                        embedding=embedding,
                        limit=request_limit,
                        visibility=visibility,
                        category=request.category,
                        subcategory=request.subcategory,
                        source_provider=request.source_provider,
                        source_model=request.source_model,
                        source_agent=request.source_agent,
                        include_archived=bool(request.include_archived),
                        boost_recency=bool(request.boost_recency),
                        recency_weight=request.recency_weight,
                        exclude_superseded=bool(request.exclude_superseded or request.current_only),
                        **semantic_trace_kwargs,
                    )
                    semantic_boosted_order = bool(request.boost_recency)
                except Exception as exc:
                    logger.warning(
                        "[VECTOR] semantic search failed for '%s'; falling back to FTS: %s",
                        request.query[:30],
                        exc,
                    )
                    rows = await _fts_fallback()
                    semantic_failed = True
                    semantic_boosted_order = False
                # Review #6 fix (2026-05-23): when semantic search returns
                # zero rows, auto-fall-back to FTS in the same request so
                # callers don't have to retry with semantic=false. Most
                # mnemos rows historically had no embedding row in
                # memory_embeddings (slice 2 backfill incomplete on some
                # deployments); semantic_search filters embedding IS NOT
                # NULL, so multi-term queries against partially-embedded
                # corpora silently returned empty. FTS still hits the
                # FTS5 index regardless of embedding state.
                if not rows and not semantic_failed:
                    logger.info(f"[VECTOR] semantic returned 0 rows for '{request.query[:30]}'; falling back to FTS")
                    rows = await _fts_fallback()
                    semantic_boosted_order = False
                elif rows and not semantic_failed:
                    # Relevance floor (UAT 2026-06-13). These are genuine
                    # vector rows (not the semantic_failed / 0-row->FTS
                    # fallback paths above, which carry no vector score and
                    # are never filtered). Two steps:
                    #
                    # 1) Stamp each row with a canonical normalized cosine
                    #    similarity (0..1, higher=better) under
                    #    SEMANTIC_SCORE_KEY, converting the backend's raw
                    #    score column via its declared metric. Done HERE —
                    #    not in row_to_memory — so an FTS row's rank/
                    #    rank_score relevance column can never be mistaken
                    #    for a vector score.
                    # 2) Drop hits below the floor. Default floor =
                    #    effective_semantic_floor() unless the caller set
                    #    min_score; min_score=0.0 opts out (legacy top-k).
                    score_col = getattr(backend.memories, "SEMANTIC_SCORE_COLUMN", "rank_score")
                    score_metric = getattr(backend.memories, "SEMANTIC_SCORE_METRIC", METRIC_COSINE_DISTANCE)
                    for r in rows:
                        if isinstance(r, dict):
                            r[SEMANTIC_SCORE_KEY] = score_to_similarity(r.get(score_col), score_metric)
                    floor = request.min_score
                    if floor is None:
                        floor = effective_semantic_floor()
                    floor = max(0.0, min(1.0, float(floor)))
                    if floor > 0.0:
                        before = len(rows)
                        kept = []
                        for r in rows:
                            sim = normalize_similarity(r)
                            # A genuine semantic row whose score is
                            # missing/non-finite is treated as BELOW the
                            # floor — failing closed protects precision
                            # exactly when scoring is broken (ngc-review
                            # finding 2026-06-13).
                            if sim is not None and sim >= floor:
                                kept.append(r)
                        rows = kept
                        if len(rows) != before:
                            logger.info(
                                "[VECTOR] min_score floor=%.3f dropped %d/%d for '%s'",
                                floor,
                                before - len(rows),
                                before,
                                request.query[:30],
                            )
                    # GRAEAE OOD / nonsense-query gate (UAT 2026-06-13 cycle 2).
                    # A scalar floor cannot separate gibberish from weak-valid
                    # queries (bge-m3 cosine baseline overlap). After the
                    # absolute floor, apply a relative margin + lexical-anchor
                    # gate: an ANCHORABLE query whose top hits share NO
                    # significant token AND whose score distribution is FLAT
                    # (low top1-mean margin) is treated as out-of-distribution
                    # and returned EMPTY. Anchored or skewed sets pass
                    # (recall-favoring). min_margin=0.0 (or the env override
                    # set to 0) DISABLES the gate. Runs on the already-floored
                    # `rows`, so it can only shrink an already-precision-cut set.
                    # OOD gate is ON BY DEFAULT (UAT-mandated, recall-first):
                    # empirically 0 false-negatives across 15 genuine queries
                    # (incl. paraphrases) because a lexical anchor OR a skewed
                    # score distribution always keeps the set — only an
                    # UNANCHORED *and* FLAT result set is dropped. Operators
                    # disable it fleet-wide with MNEMOS_SEMANTIC_MARGIN_FLOOR=0
                    # and callers per-request with min_margin=0.0. Resolve via
                    # the SAME sanitizer used for the cache key so a cached
                    # entry and its live recompute can never gate differently
                    # (ngc-review 2026-06-13).
                    margin_floor = _resolve_margin_floor(request.min_margin)
                    if rows and ood_gate_enabled() and margin_floor and margin_floor > 0.0:
                        if is_ood_result_set(request.query, rows, margin_floor=margin_floor):
                            # Do NOT log any query-derived value — even a
                            # truncated hash of a short/low-entropy query is
                            # brute-forceable and linkable (ngc-review
                            # 2026-06-13). Counts + floor are enough to reason
                            # about gate behavior; the trace_id correlates the
                            # request.
                            logger.info(
                                "[VECTOR] OOD gate (margin<%.3f, no anchor) emptied %d rows trace=%s",
                                margin_floor,
                                len(rows),
                                search_trace_id,
                            )
                            rows = []
        else:
            rows = await _fts_fallback()

    _log_search_phase(search_trace_id, search_started_at, "metadata_fetch")
    # Redact-at-retrieval: mask credential spans unless this is a root
    # include_secrets / vault-targeted search. Backstops a vaulted-miss and
    # masks incidental spans before the result set is serialized OR cached.
    _redact = _should_redact_secrets(user, include_secrets=bool(request.include_secrets), namespace=search_namespace)
    # Framing is applied as the FINAL pass below (after rerank/decay) so the
    # cross-encoder reranker scores RAW content, not the data-boundary
    # wrapper. ``_row_to_memory`` here only redacts; injection-defense
    # framing is layered on at serialize time.
    _frame = _should_frame_data(user, operational=bool(request.operational))
    memories = [
        _row_to_memory(
            r,
            include_compressed=request.include_compressed,
            redact_secrets=_redact,
        )
        for r in rows
    ]

    # v6.2 M-2.2.3: cross-encoder rerank for deep profile.
    # Reranker returns [] on breaker-open / error — we keep original
    # order in that case (no hard failure on search path).
    if search_profile is SearchProfile.DEEP and memories:
        try:
            reranker = get_reranker()
            docs = [m.content or "" for m in memories]
            scores = await reranker.rerank(request.query, docs)
            if scores and len(scores) == len(memories):
                indexed = sorted(
                    range(len(memories)),
                    key=lambda i: scores[i],
                    reverse=True,
                )
                memories = [memories[i] for i in indexed]
                logger.info(
                    "[SEARCH] profile=deep reranked n=%d trace=%s",
                    len(memories),
                    search_trace_id,
                )
        except Exception as exc:  # safety net; reranker.rerank should not raise
            logger.warning(
                "[SEARCH] reranker dispatch failed trace=%s err=%s",
                search_trace_id,
                exc,
            )

    # v6.2 M-2.2.4: per-category temporal decay applied AFTER any
    # reranker (deep profile) but BEFORE the response. Decay is
    # cheap (process-local TTL cache + per-row math), so it runs
    # unconditionally when the table exists. Override map from the
    # request overrides per-category half-life or "*" flattens all.
    if memories:
        try:
            decay_table = await load_decay_table(backend)
            if decay_table or request.decay_overrides or any(getattr(m, "superseded_by", None) for m in memories):
                memories = apply_decay(
                    memories,
                    decay_table,
                    overrides=request.decay_overrides,
                    recency_weight=request.recency_weight,
                    preserve_current_order=semantic_boosted_order,
                    exclude_superseded=bool(request.exclude_superseded or request.current_only),
                )
        except Exception:
            logger.exception(
                "[SEARCH] decay application failed trace=%s; returning unsorted",
                search_trace_id,
            )

    # Fire-and-forget recall-frequency bump for the hit set.
    # Doesn't block the response; failure here is logged and ignored
    # (recall counters are observability, not user-content correctness).
    if memories:
        hit_ids = [m.id for m in memories]
        asyncio.create_task(_bump_recall_counters(hit_ids))

    compression_applied = False
    compression_metadata = {}

    # Prompt-injection defense (release-gate 2026-06-13): frame each hit's
    # content as untrusted DATA + quarantine AI-targeting injection
    # meta-instructions, as the LAST transform before serialize/cache so a
    # malicious stored memory cannot steer a consuming agent. Skipped only
    # for the root operational opt-in (verbatim recall). Reranker/decay ran
    # on raw content above; framing here keeps their scoring intact.
    if _frame and memories:
        from mnemos.core.injection_defense import defend as _defend

        for _m in memories:
            _m.content = _defend(_m.content)
            if _m.compressed_content:
                _m.compressed_content = _defend(_m.compressed_content)
            if _m.verbatim_content:
                _m.verbatim_content = _defend(_m.verbatim_content)

    response = MemoryListResponse(
        count=len(memories),
        memories=memories,
        compression_applied=compression_applied,
        compression_metadata=compression_metadata if compression_applied else None,
    )
    _log_search_phase(search_trace_id, search_started_at, "serialize")

    if _lc._cache and not request.include_compressed and not compression_applied:
        try:
            # v6.2 M-2.2.3: per-profile cache TTL. fast/balanced 5min;
            # deep 30s (less cacheable per spec — reranker scoring drifts
            # faster as memories churn).
            _profile_ttl = {
                SearchProfile.FAST: 300,
                SearchProfile.BALANCED: 300,
                SearchProfile.DEEP: 30,
            }
            await _lc._cache.setex(
                cache_key,
                _profile_ttl[search_profile],
                response.model_dump_json(),
            )
        except Exception as e:
            logger.warning(f"[CACHE] search write error: {e}")

    return response


@router.post("/memories", response_model=MemoryItem, status_code=201)
async def create_memory(
    request: MemoryCreateRequest,
    response: Response,
    user: UserContext = Depends(get_current_user),
):
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=422, detail="Memory content cannot be empty")
    backend = _backend_or_503()
    mem_id = new_memory_id()

    # Only root may create a memory attributed to a different owner
    # or namespace than the caller — closes the ghost-writing
    # vulnerability where any user could set request.owner_id.
    if request.owner_id and request.owner_id != user.user_id and user.role != "root":
        raise HTTPException(status_code=403, detail="owner_id override requires root")
    if request.namespace and request.namespace != user.namespace and user.role != "root":
        raise HTTPException(status_code=403, detail="namespace override requires root")
    owner_id = request.owner_id or user.user_id
    namespace = request.namespace or user.namespace
    permission_mode = _validate_permission_mode(request.permission_mode, default=600)

    # GDPR fence: refuse to insert onto a scope that is mid-deletion
    # (soft_deleted and inside the 30-day grace window). A new memory
    # would otherwise be skipped by the next hard-delete -- it only
    # removes rows with ``deleted_at IS NOT NULL`` -- and would survive
    # even though the request is recorded as complete. Root bypasses the
    # fence so operators can still inject tombstone / restoration rows.
    if user.role != "root":
        await _assert_no_active_deletion(
            backend, owner_id=owner_id, namespace=namespace
        )

    # Secret-vault persisted-text classification (release-blocking 2026-06-14).
    # Classify every text field that will be stored. Any VAULT-class finding in
    # content or verbatim_content moves the row to the vault namespace;
    # incidental spans are recorded for redact-at-retrieval.
    _meta = dict(request.metadata or {})
    if request.metadata is None and request.source is not None:
        _meta.setdefault("source", request.source)
    _classified = classify_persisted_text_fields(
        content=request.content,
        verbatim_content=(request.verbatim_content if request.verbatim_content is not None else request.content),
        metadata=_meta,
        namespace=namespace,
        classified_at="ingest",
        memory_id=mem_id,
    )
    _meta = _classified.metadata
    namespace = _classified.namespace

    persisted_metadata = _meta or {"source": request.source}
    metadata_json = json.dumps(persisted_metadata)
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            dedup = await evaluate_memory_create_dedup(
                backend.memories,
                tx,
                owner_id=owner_id,
                namespace=namespace,
                content=request.content,
                logger=logger,
            )
            if dedup.action == "reject" and dedup.existing_id:
                return JSONResponse(
                    status_code=409,
                    content=duplicate_content_error_body(dedup.existing_id),
                )
            if dedup.action == "merge" and dedup.existing_id:
                row = await backend.memories.bump_recall_and_get_memory(
                    tx,
                    dedup.existing_id,
                    visibility=_read_visibility_for(user, namespace=namespace),
                )
                if row is None:
                    return JSONResponse(
                        status_code=409,
                        content=duplicate_content_error_body(dedup.existing_id),
                    )
                response.status_code = 200
                return _row_to_memory(row)
            # The Postgres trg_memory_version_insert trigger writes
            # version 1 + branch automatically; the SQLite path does
            # not have that trigger today (deferred to v4.2 with
            # branch/version surface).
            # Inline embed via mnemos.runtime.embedder.embed_text — the
            # in-process embedder (architectural decision
            # mem_1779334716543_f8ebd4, 2026-05-21) loads the GGUF model
            # once per worker and returns a 768-dim vector in ~50-100ms
            # on PYTHIA CPU. Failures (empty vec) are swallowed and the
            # row keeps embedding=NULL; the backfill script picks it up
            # on the next pass.
            #
            # Codex adversarial-review gate (2026-06-05): the embedding
            # generation (_get_embedding) is separated from the DB write
            # (upsert_memory_embedding) so that only embedding-generation
            # errors are silently degraded to NULL. DB write errors (e.g.
            # constraint violation, disk-full, connection loss) MUST
            # propagate and roll back the transaction — no partial-commit
            # NULL vector.
            #
            # Codex adversarial-review gate fix (2026-06-05): the initial
            # gate edit removed the try/except around _get_embedding
            # entirely, which meant embedding-model failures (OOM, model
            # load error, etc.) would abort the entire create_memory tx.
            # Restore the try/except around embedding generation only —
            # upsert_memory_embedding remains outside so DB write errors
            # still propagate.
            try:
                # F2 (adversarial review 2026-06-28): embed the span-redacted
                # content, not raw, so secret text never enters the vector index.
                vec = await _get_embedding(_content_redacted_for_embedding(request.content, _classified))
            except Exception:
                logger.exception(
                    "[create_memory] inline embed generation failed for %s; "
                    "row will be backfilled with embedding later",
                    mem_id,
                )
                vec = None
            # CHILD C v2 (2026-06-06): Embedding is now co-transactional —
            # passed inline to insert_memory so the VECTOR column is written
            # in the same tx. upsert_memory_embedding is kept for backfill
            # (scripts) and federation copy_embeddings (F-1.4).
            await backend.memories.insert_memory(
                tx,
                memory_id=mem_id,
                content=request.content,
                category=request.category,
                subcategory=request.subcategory,
                metadata_json=metadata_json,
                quality_rating=75,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=permission_mode,
                source_model=request.source_model,
                source_provider=request.source_provider,
                source_session=request.source_session,
                source_agent=request.source_agent,
                verbatim_content=(
                    request.verbatim_content if request.verbatim_content is not None else request.content
                ),
                embedding=vec,
                created=None,
                updated=None,
            )
            await _write_memory_mutation_audit_entry(
                backend,
                tx,
                op="create",
                memory_id=mem_id,
                content=request.content,
                category=request.category,
                subcategory=request.subcategory,
                metadata=persisted_metadata,
                writer_id=user.user_id,
            )
            # Same-tx outbox enqueue — preserves the v4.0 contract
            # that webhook_deliveries rows commit atomically with
            # the data write.
            if getattr(backend, "supports_webhooks", True):
                delivery_ids = await backend.webhooks.dispatch_event(
                    tx,
                    "memory.created",
                    {
                        "memory_id": mem_id,
                        "category": request.category,
                        "subcategory": request.subcategory,
                        "content": _redacted_for_webhook(request.content, _classified.metadata),
                        "owner_id": owner_id,
                        "namespace": namespace,
                    },
                    owner_id=owner_id,
                    namespace=namespace,
                )
            else:
                delivery_ids = []
            # Re-fetch the row inside the same tx so the response
            # carries DB-resolved values (created/updated, etc).
            row = await backend.memories.get_memory(
                tx,
                mem_id,
                visibility=_vault_inclusive_read_visibility_for(user, namespace=namespace),
            )
            if row is None:
                raise RuntimeError(f"post-write re-fetch missed just-created memory {mem_id}")
    except DuplicateMemoryError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("memory.create transaction failed for %s: %s", mem_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Memory creation failed") from e

    # Schedule HTTP delivery for each enqueued outbox row, after the
    # transaction has committed.
    _schedule_outbox_deliveries(delivery_ids)

    # v4.2 NATS additive emit. Best-effort — silent skip when broker
    # unreachable. Webhooks outbox above is the durable path.
    from mnemos.nats.client import get_node_name as _nats_get_node_name

    safe_ns = safe_subject_segment(namespace)
    await _publish_nats_with_timeout(
        f"mnemos.memory.created.{safe_ns}",
        {
            "memory_id": mem_id,
            "namespace": namespace,
            "category": request.category,
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{mem_id}.created",
    )

    await _invalidate_caches_after_mutation()
    result = _row_to_memory(row)
    # issue #38: tell the caller whether this just-saved memory is already
    # semantically searchable. `vec` is the inline-embed result computed
    # above; when it's empty the row is durable + ID-retrievable now but
    # won't match semantic search until the backfill worker embeds it.
    # Review-gate fix: on SQLite the inline embedding lands in
    # memories.embedding while semantic_search reads memory_embeddings (only
    # filled by backfill), so an inline vec is NOT yet searchable there —
    # backends advertise this via inline_embedding_searchable (default True).
    inline_searchable = getattr(backend, "inline_embedding_searchable", True)
    result.embedding_status = "ready" if (vec and inline_searchable) else "pending"
    return result


@router.post("/memories/bulk", response_model=BulkCreateResponse, status_code=201)
async def bulk_create_memories(
    request: BulkCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create multiple memories in one request. Per-item errors are collected, not raised."""
    backend = _backend_or_503()
    created_ids: list[str] = []
    errors: list[str] = []
    delivery_ids: list[str] = []
    nats_created_events: list[dict] = []
    candidates: list[_BulkCreateCandidate] = []
    for i, mem in enumerate(request.memories):
        if not mem.content.strip():
            errors.append(f"[{i}] content is empty")
            continue
        if mem.owner_id and mem.owner_id != user.user_id and user.role != "root":
            errors.append(f"[{i}] owner_id override requires root")
            continue
        if mem.namespace and mem.namespace != user.namespace and user.role != "root":
            errors.append(f"[{i}] namespace override requires root")
            continue
        try:
            permission_mode = _validate_permission_mode(mem.permission_mode, default=600)
        except HTTPException as exc:
            errors.append(f"[{i}] {exc.detail}")
            continue
        mid = new_memory_id()
        verbatim = mem.verbatim_content if mem.verbatim_content is not None else mem.content
        owner_id = mem.owner_id or user.user_id
        namespace = mem.namespace or user.namespace
        _classified = classify_persisted_text_fields(
            content=mem.content,
            verbatim_content=verbatim,
            metadata=mem.metadata or {},
            namespace=namespace,
            classified_at="bulk_ingest",
            memory_id=mid,
        )
        item_metadata = _classified.metadata
        namespace = _classified.namespace
        candidates.append(
            _BulkCreateCandidate(
                index=i,
                memory=mem,
                memory_id=mid,
                verbatim=verbatim,
                owner_id=owner_id,
                namespace=namespace,
                metadata=item_metadata,
                permission_mode=permission_mode,
                embedding_content=_content_redacted_for_embedding(mem.content, _classified),
            )
        )

    if candidates:
        try:
            embeddings = await _get_embeddings_batch([candidate.embedding_content for candidate in candidates])
        except Exception:
            logger.exception(
                "[bulk_create_memories] batch inline embed generation failed for %d memories; "
                "rows will be backfilled with embeddings later",
                len(candidates),
            )
            embeddings = []
        for candidate, vec in zip(candidates, embeddings):
            candidate.embedding = vec or None
        if len(embeddings) < len(candidates):
            for candidate in candidates[len(embeddings) :]:
                candidate.embedding = None

    if candidates:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            for candidate in candidates:
                i = candidate.index
                mem = candidate.memory
                mid = candidate.memory_id
                namespace = candidate.namespace
                owner_id = candidate.owner_id
                item_metadata = candidate.metadata
                vec = candidate.embedding
                item_delivery_ids = []
                try:
                    async with _bulk_item_savepoint(tx, i):
                        if vec is None:
                            logger.warning(
                                "[bulk_create_memories] inline embed generation failed for %s; "
                                "row will be backfilled with embedding later",
                                mid,
                            )
                        dedup = await evaluate_memory_create_dedup(
                            backend.memories,
                            tx,
                            owner_id=owner_id,
                            namespace=namespace,
                            content=mem.content,
                            logger=logger,
                        )
                        if dedup.action == "reject" and dedup.existing_id:
                            errors.append(f"[{i}] duplicate_content: {dedup.existing_id}")
                            continue
                        if dedup.action == "merge" and dedup.existing_id:
                            row = await backend.memories.bump_recall_and_get_memory(
                                tx,
                                dedup.existing_id,
                                visibility=_read_visibility_for(user, namespace=namespace),
                            )
                            if row is None:
                                errors.append(f"[{i}] duplicate_content: {dedup.existing_id}")
                                continue
                            created_ids.append(row["id"])
                            continue
                        await backend.memories.insert_memory(
                            tx,
                            memory_id=mid,
                            content=mem.content,
                            category=mem.category,
                            subcategory=mem.subcategory,
                            metadata_json=json.dumps(item_metadata),
                            quality_rating=75,
                            owner_id=owner_id,
                            namespace=namespace,
                            permission_mode=candidate.permission_mode,
                            source_model=mem.source_model,
                            source_provider=mem.source_provider,
                            source_session=mem.source_session,
                            source_agent=mem.source_agent,
                            verbatim_content=candidate.verbatim,
                            embedding=vec,
                            created=None,
                            updated=None,
                        )
                        if vec:
                            await backend.memories.upsert_memory_embedding(tx, mid, vec)
                        await _write_memory_mutation_audit_entry(
                            backend,
                            tx,
                            op="create",
                            memory_id=mid,
                            content=mem.content,
                            category=mem.category,
                            subcategory=mem.subcategory,
                            metadata=item_metadata,
                            writer_id=user.user_id,
                        )
                        if getattr(backend, "supports_webhooks", True):
                            item_delivery_ids = await backend.webhooks.dispatch_event(
                                tx,
                                "memory.created",
                                {
                                    "memory_id": mid,
                                    "category": mem.category,
                                    "subcategory": mem.subcategory,
                                    "content": _redacted_for_webhook(mem.content, item_metadata),
                                    "owner_id": owner_id,
                                    "namespace": namespace,
                                },
                                owner_id=owner_id,
                                namespace=namespace,
                            )
                        else:
                            item_delivery_ids = []
                except Exception as e:
                    errors.append(f"[{i}] {e}")
                    continue
                created_ids.append(mid)
                nats_created_events.append(
                    {
                        "memory_id": mid,
                        "namespace": namespace,
                        "category": mem.category,
                    }
                )
                delivery_ids.extend(item_delivery_ids)
    _schedule_outbox_deliveries(delivery_ids)
    from mnemos.nats.client import get_node_name as _nats_get_node_name

    source_node = _nats_get_node_name()
    for event in nats_created_events:
        safe_ns = safe_subject_segment(event["namespace"])
        await _publish_nats_with_timeout(
            f"mnemos.memory.created.{safe_ns}",
            {**event, "source_node": source_node},
            msg_id=f"{event['memory_id']}.created",
        )
    await _invalidate_caches_after_mutation()
    return BulkCreateResponse(created=len(created_ids), memory_ids=created_ids, errors=errors)


@router.patch("/memories/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Partially update a memory (content, category, subcategory, metadata)."""
    backend = _backend_or_503()
    updates: dict = {}
    if request.content is not None:
        if not request.content.strip():
            raise HTTPException(status_code=422, detail="Memory content cannot be empty")
        updates["content"] = request.content
    if request.category is not None:
        updates["category"] = request.category
    if request.subcategory is not None:
        updates["subcategory"] = request.subcategory
    if request.metadata is not None:
        updates["metadata"] = json.dumps(request.metadata)
    if request.verbatim_content is not None:
        updates["verbatim_content"] = request.verbatim_content
    if request.permission_mode is not None:
        updates["permission_mode"] = _validate_permission_mode(request.permission_mode)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    # Classify PATCH text variants before persistence. If the PATCH turns an
    # ordinary row into a credential record, move it to the vault in the same
    # atomic UPDATE.
    if request.content is not None or request.verbatim_content is not None:
        base_meta = request.metadata or {}
        _classified = classify_persisted_text_fields(
            content=request.content,
            verbatim_content=request.verbatim_content,
            metadata=base_meta,
            namespace=user.namespace,
            classified_at="patch",
            memory_id=memory_id,
        )
        if _classified.metadata:
            updates["metadata"] = json.dumps(_classified.metadata)
        if _classified.vaulted:
            updates["namespace"] = _classified.namespace

    # Authorization + mutation in a single repository call: the
    # visibility predicate folds into the UPDATE … RETURNING, so a
    # concurrent admin/repair changing ownership between auth check
    # and write cannot complete the update. Same TOCTOU-safe shape
    # as the legacy handler.
    visibility = _mutation_visibility_for(
        user,
        namespace=None if is_root(user) else user.namespace,
    )
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            try:
                row = await backend.memories.update_memory(
                    tx,
                    memory_id,
                    visibility=visibility,
                    fields=updates,
                )
            except asyncpg.PostgresError as exc:
                handle_trigger_pgerror(exc)
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Memory {memory_id} not found",
                )
            if updates.get("namespace") == VAULT_NAMESPACE:
                row = await backend.memories.get_memory(
                    tx,
                    memory_id,
                    visibility=_vault_inclusive_read_visibility_for(user, namespace=VAULT_NAMESPACE),
                )
                if row is None:
                    raise RuntimeError(f"post-write re-fetch missed just-vaulted memory {memory_id}")
            await _write_memory_mutation_audit_entry(
                backend,
                tx,
                op="update",
                memory_id=memory_id,
                content=row["content"],
                category=row["category"],
                subcategory=row["subcategory"],
                metadata=_metadata_for_audit(row["metadata"]),
                writer_id=user.user_id,
            )
            if getattr(backend, "supports_webhooks", True):
                delivery_ids = await backend.webhooks.dispatch_event(
                    tx,
                    "memory.updated",
                    {
                        "memory_id": memory_id,
                        "category": row["category"],
                        "subcategory": row["subcategory"],
                        "content": _redacted_for_webhook(row["content"], row.get("metadata")),
                        "owner_id": row["owner_id"],
                        "namespace": row["namespace"],
                    },
                    owner_id=row["owner_id"],
                    namespace=row["namespace"],
                )
            else:
                delivery_ids = []
    except HTTPException:
        raise
    _schedule_outbox_deliveries(delivery_ids)
    try:
        updated_at = row["updated"]
    except (KeyError, TypeError):
        updated_at = None
    if hasattr(updated_at, "isoformat"):
        updated_suffix = updated_at.isoformat()
    else:
        updated_suffix = str(int(time.time() * 1000))
    namespace = row["namespace"]
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name

    safe_ns = safe_subject_segment(namespace)
    await _nats_publish_event(
        f"mnemos.memory.updated.{safe_ns}",
        {
            "memory_id": memory_id,
            "namespace": namespace,
            "category": row["category"],
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{memory_id}.updated.{updated_suffix}",
    )
    await _invalidate_caches_after_mutation()
    return _row_to_memory(row)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Delete a memory by ID."""
    backend = _backend_or_503()
    # Mutation visibility: non-root pinned to (owner_id, namespace);
    # root sees everything. Closes the cross-namespace deletion path
    # where a namespace-A user could delete a namespace-B row under
    # the same owner_id.
    visibility = _mutation_visibility_for(
        user,
        namespace=None if is_root(user) else user.namespace,
    )
    delivery_ids: list[str] = []
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            try:
                row = await backend.memories.delete_memory(
                    tx,
                    memory_id,
                    visibility=visibility,
                    requested_by=user.user_id,
                    requested_at=None,
                    request_kind="admin_purge",
                    reason=None,
                    source=["api.delete_memory"],
                )
            except asyncpg.PostgresError as exc:
                handle_trigger_pgerror(exc)
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Memory {memory_id} not found",
                )
            await _write_memory_mutation_audit_entry(
                backend,
                tx,
                op="delete",
                memory_id=memory_id,
                content=row["content"],
                category=row["category"],
                subcategory=row["subcategory"],
                metadata=None,
                writer_id=user.user_id,
            )
            if getattr(backend, "supports_webhooks", True):
                delivery_ids = await backend.webhooks.dispatch_event(
                    tx,
                    "memory.deleted",
                    {
                        "memory_id": row["id"],
                        "category": row["category"],
                        "subcategory": row["subcategory"],
                        "content": _redacted_for_webhook(row["content"], row.get("metadata")),
                        "owner_id": row["owner_id"],
                        "namespace": row["namespace"],
                    },
                    owner_id=row["owner_id"],
                    namespace=row["namespace"],
                )
            else:
                delivery_ids = []
    except HTTPException:
        raise
    _schedule_outbox_deliveries(delivery_ids)
    namespace = row["namespace"]
    from mnemos.nats import publish_event as _nats_publish_event
    from mnemos.nats.client import get_node_name as _nats_get_node_name

    safe_ns = safe_subject_segment(namespace)
    await _nats_publish_event(
        f"mnemos.memory.deleted.{safe_ns}",
        {
            "memory_id": row["id"],
            "namespace": namespace,
            "category": row["category"],
            "source_node": _nats_get_node_name(),
        },
        msg_id=f"{row['id']}.deleted",
    )
    await _invalidate_caches_after_mutation()


@router.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(
    request: RehydrationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Return memories optimized for Claude context injection (Phase 5).

    Prompt-injection defense (release-gate 2026-06-13): rehydrate is THE
    canonical "inject memories into an agent's context" path, so it is
    ALWAYS defended -- there is deliberately NO operational/verbatim opt-in
    here (unlike search/list/get): handing an agent unframed, un-quarantined
    recall as context is exactly the steer-the-agent risk this gate exists
    to close. Each memory is injection-quarantined and the whole blob is
    framed as untrusted DATA. Trusted callers needing verbatim operational
    recall use search/get with operational=true instead.
    """
    require_postgres_pool_or_503(route_label="POST /v1/memories/rehydrate")
    # Same v3.1.2 Tier 3 pinning as /memories/search — rehydrate is a
    # read path for the caller's own corpus.
    rehydrate_owner_id = None if is_root(user) else user.user_id
    rehydrate_namespace = None if is_root(user) else user.namespace

    # v3.2 compression-in-hot-paths: rehydrate is the canonical
    # "fit memories into a token budget" path, so it benefits most
    # from preferring the contest winner variant over the raw content.
    # Fallback chain: contest winner -> raw content.
    #
    # Inlined here (rather than routed through _fts_fetch) because
    # the JOIN shape is rehydrate-specific: one-to-one with
    # memory_compressed_variants, COALESCE chosen in SELECT. The
    # shared helper doesn't need the complexity.
    #
    # We also track `compression_applied` for the response: true
    # iff at least one row returned a variant-compressed form.
    clean_query = request.query.strip()
    sql_conditions = [
        "to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)",
        "m.deleted_at IS NULL",
        "m.archived_at IS NULL",
    ]
    sql_params: list = [clean_query, request.limit]
    idx = 3
    if rehydrate_owner_id is not None:
        # Slice 2.1: full v1_multiuser-mirror visibility predicate
        # (owner / federation / world-readable / group-readable),
        # aliased to the JOIN's `m.` table reference. Same predicate
        # as list/get/search so a memory visible there is visible
        # via /memories/rehydrate.
        from mnemos.core.visibility import read_visibility_predicate

        clause, vis_params = read_visibility_predicate(
            rehydrate_owner_id,
            list(user.group_ids),
            idx,
            table_alias="m",
        )
        sql_conditions.append(clause)
        sql_params.extend(vis_params)
        idx += len(vis_params)
    if rehydrate_namespace is not None:
        sql_conditions.append(f"m.namespace=${idx}")
        sql_params.append(rehydrate_namespace)
        idx += 1
    if request.category is not None:
        sql_conditions.append(f"m.category=${idx}")
        sql_params.append(request.category)
        idx += 1
    # Secret vault (release-blocking 2026-06-13): rehydrate enumerates the
    # corpus into a Claude context window — it has NO include_secrets opt-in,
    # so the vault namespace is excluded for EVERYONE (incl. root, whose
    # rehydrate_namespace is None / unpinned). Parameterized to avoid any
    # SQL-interpolation precedent (ngc-review 2026-06-13). NULL-safe so
    # legitimate NULL-namespace rows (never secret — vault rows always carry
    # the non-NULL "vault" namespace) are preserved, not dropped by SQL
    # three-valued logic (ngc-review 2026-06-13 round 4).
    sql_conditions.append(f"(m.namespace IS NULL OR m.namespace <> ${idx})")
    sql_params.append(VAULT_NAMESPACE)
    idx += 1

    where_sql = " AND ".join(sql_conditions)
    sql = (
        "SELECT m.id, m.category, m.created, m.quality_rating, "
        "       m.content AS raw_content, "
        "       v.compressed_content AS compressed_content, "
        "       v.compressed_content IS NOT NULL AS variant_used, "
        "       ts_rank(to_tsvector('english', m.content), "
        "               plainto_tsquery('english', $1)) AS rank "
        "FROM memories m "
        "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
        f"WHERE {where_sql} "
        "ORDER BY rank DESC LIMIT $2"
    )

    pool = require_postgres_pool_or_503(route_label="POST /v1/memories/rehydrate")
    async with pool.acquire() as conn:
        async with _rls_context(conn, user):
            rows = await conn.fetch(sql, *sql_params)

    if not rows:
        return RehydrationResponse(
            context="",
            tokens_used=0,
            original_tokens=0,
            compression_ratio=1.0,
            quality_score=100,
            memories_included=0,
            compression_applied=False,
        )
    context_parts = []
    raw_size = 0
    variant_hits = 0
    from mnemos.core.secret_detection import redact_content as _redact_content
    from mnemos.core.injection_defense import quarantine_injections as _quarantine, frame_untrusted as _frame

    for row in rows:
        # Prefer contest winner (variant_used=True), else raw.
        effective = row["compressed_content"] or row["raw_content"]
        raw_size += len(row["raw_content"] or "")
        if row["variant_used"]:
            variant_hits += 1
        # Redact-at-retrieval: rehydrate has no include_secrets opt-in, so
        # any credential span (vaulted-miss or incidental) is masked before
        # it enters the Claude context blob.
        effective = _redact_content(effective)
        # Prompt-injection defense (release-gate 2026-06-13): rehydrate is
        # THE canonical inject-into-agent-context path, so neutralize any
        # AI-targeting injection meta-instruction in each memory before it
        # enters the context blob. Legit operational prose passes through.
        effective = _quarantine(effective)
        created_str = row["created"].strftime("%Y-%m-%d") if row["created"] else "unknown"
        context_parts.append(f"[{row['category']} / {created_str}]\n{effective[:2000]}")
    combined_context = "\n\n---\n\n".join(context_parts)
    # Wrap the whole rehydrated blob in an untrusted-data boundary so the
    # consuming agent treats injected memories as reference DATA.
    combined_context = _frame(combined_context)
    original_tokens = int(len(combined_context) / 4)

    tokens_used = min(original_tokens, request.budget_tokens) if request.budget_tokens else original_tokens
    compression_applied = variant_hits > 0
    # Only report a non-1.0 ratio when variants were actually used;
    # otherwise the context size is dominated by category/date
    # prefixes added by the rehydrator and the "ratio" is misleading.
    if compression_applied and raw_size > 0:
        compression_ratio = len(combined_context) / raw_size
    else:
        compression_ratio = 1.0

    logger.info(
        f"[REHYDRATE] query='{request.query[:30]}' | memories={len(rows)} | "
        f"variant_hits={variant_hits} | original_tokens={original_tokens} | "
        f"tokens_used={tokens_used} | compression_applied={compression_applied} | "
        f"compression_ratio={compression_ratio:.3f}"
    )
    return RehydrationResponse(
        context=combined_context[: request.budget_tokens * 4] if request.budget_tokens else combined_context,
        tokens_used=tokens_used,
        original_tokens=original_tokens,
        compression_ratio=compression_ratio,
        quality_score=100,
        memories_included=len(rows),
        compression_applied=compression_applied,
    )
