"""Write-path side effects: audit, outbox, NATS and the deletion fence.

The write half of the memory API has a handful of side effects that are not
part of the row itself — the per-memory audit-chain entry, the transactional
webhook outbox, the NATS ``memory.created`` intents, the read-side cache and
visibility-epoch invalidation, and the fence that refuses to write onto a
scope whose deletion grace window is running. They lived in
``mnemos/api/routes/memories.py`` and were imported from there by ``dag.py``,
the CLI and the MCP tools, which made one route module the shared library for
the others. They live here instead: routes import from this module, and
routes do not import each other's private names (enforced by the
import-linter contract in ``pyproject.toml``).

Nothing here has been changed in behaviour; the functions moved verbatim.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.read_policy import _redacted_for_webhook
from mnemos.core.ids import new_memory_id  # noqa: F401  (re-exported: routes patch it here)
from mnemos.domain.deletion_fence import (
    assert_no_active_deletion,
    assert_no_active_deletion_for_scopes,
)
from mnemos.persistence.nats_events import safe_subject_segment

logger = logging.getLogger(__name__)

NatsPublishIntent = tuple[str, dict, str]


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
    embedding=None,
) -> None:
    """Append a route-side audit-chain entry for one memory mutation.

    This keeps the single create/update/delete and bulk-create paths on the
    same helper so new write paths do not accidentally bypass the Ed25519
    per-memory chain. The helper intentionally preserves the legacy API
    behavior: when audit is disabled, unsupported by the backend, or missing
    a session secret, the data write still commits.
    """
    from mnemos.audit.route_helper import write_configured_audit_entry

    await write_configured_audit_entry(
        backend,
        tx,
        op=op,
        memory_id_str=memory_id,
        snapshot={
            "content": content,
            "category": category,
            "subcategory": subcategory,
            "metadata": metadata,
            "embedding": embedding,
        },
        writer_id=writer_id,
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


# The fence is domain logic, not route policy: the MPF/CHARON importers write
# memories from below the API layer and must run the same probe, and
# ``mnemos.domain`` may not import ``mnemos.api`` under the layered
# import-linter contract. Re-exported under the historical private name as the
# SAME object -- not a wrapper -- because route modules and the tests that
# patch ``memory_mutations._assert_no_active_deletion`` resolve this name.
_assert_no_active_deletion = assert_no_active_deletion
_assert_no_active_deletion_for_scopes = assert_no_active_deletion_for_scopes


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


async def _publish_bulk_created_events(events: list[dict], source_node: str) -> None:
    """Publish bulk-create events concurrently without flooding NATS."""
    if not events:
        return
    from mnemos.core.config import get_settings

    semaphore = asyncio.Semaphore(get_settings().nats.bulk_publish_concurrency)

    async def _publish_one(event: dict) -> None:
        safe_ns = safe_subject_segment(event["namespace"])
        async with semaphore:
            try:
                await _publish_nats_with_timeout(
                    f"mnemos.memory.created.{safe_ns}",
                    {**event, "source_node": source_node},
                    msg_id=f"{event['memory_id']}.created",
                )
            except Exception as exc:
                logger.warning(
                    "NATS bulk-create publish failed for %s: %s",
                    event["memory_id"],
                    exc,
                )

    await asyncio.gather(*(_publish_one(event) for event in events))


async def _invalidate_caches_after_mutation() -> None:
    """Invalidate read-side caches after any memory write.

    The ``/stats`` cache entry (``stats:global:v2``) is still dropped:
    it belongs to the stats surface, not the search surface. There is no
    cached search response to evict — ``_lc._cache`` is hard-wired to
    ``None`` (no Redis in the runtime) and the search handler reads no
    cache.

    The visibility epoch is STILL bumped here: the MCP
    principal-context cache (role / namespace — authorization inputs)
    subscribes to it, so a revoke must advance the epoch.
    """
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global:v2")
        except Exception:
            pass
    try:
        await _lc._vis_epoch_get_incr()  # bump; errors silently
    except Exception:
        pass


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
            from mnemos.core.config import audit_chain_required_flag

            if audit_chain_required_flag():
                raise
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
