"""GDPR deletion-request workers.

Consumes ``deletion_requests.status='confirmed'`` rows, sweeps the
target user's rows, verifies that no live rows escaped the first pass,
and then marks the request ``soft_deleted`` inside one transaction.
If the transaction aborts, the request remains ``confirmed`` and the
next worker pass retries it.

The hard-delete phase consumes expired ``soft_deleted`` rows and
permanently removes rows already marked with ``deleted_at``. The
``deletion_requests`` row is never deleted; it is the audit breadcrumb
proving the wipe completed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from mnemos.db.deletion_log import log_target_memory_deletions
from mnemos.persistence.base import PersistenceBackend
from mnemos.persistence.deletion_ops import (
    _LIVE_ROW_COUNT_SQL,  # noqa: F401  re-exported: tests read it off this module
    _OWNER_NAMESPACE_SOFT_DELETE_SQL,
    _SOFT_DELETE_SQL,
    DEFAULT_VERIFY_ATTEMPTS,
    _parse_update_count,
    invalidate_deletion_scope_caches,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10
DEFAULT_CHECK_INTERVAL_SECONDS = 30.0
RESTORE_GRACE_DAYS = 30

# Hard-delete order is intentional for FK safety. Child tables go first
# (memory_versions, memory_branches, session_messages,
# session_memory_injections, graeae_audit_log), then parent tables
# (memories, sessions, graeae_consultations), then the remaining
# owner/namespace-scoped tables. The worker also SET LOCALs
# mnemos.suppress_version_snapshot before the DELETEs so the memory
# versioning trigger does not synthesize a fresh delete-version row
# during GDPR erasure.
_HARD_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
    (
        "memory_versions",
        "memory_versions",
        """
        DELETE FROM memory_versions
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "memory_branches",
        "memory_branches",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM memory_branches mb
         USING target_memories tm
         WHERE mb.memory_id = tm.id
           AND mb.deleted_at IS NOT NULL
        """,
    ),
    (
        "session_messages",
        "session_messages",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM session_messages sm
         USING target_sessions ts
         WHERE sm.session_id = ts.id
           AND sm.deleted_at IS NOT NULL
        """,
    ),
    (
        "session_memory_injections",
        "session_memory_injections",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM session_memory_injections smi
         USING target_sessions ts
         WHERE smi.session_id = ts.id
           AND smi.deleted_at IS NOT NULL
        """,
    ),
    (
        "graeae_audit_log",
        "graeae_audit_log",
        """
        WITH target_consultations AS (
            SELECT id
              FROM graeae_consultations
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        DELETE FROM graeae_audit_log al
         USING target_consultations tc
         WHERE al.consultation_id = tc.id
           AND al.deleted_at IS NOT NULL
        """,
    ),
    (
        "memory_archive",
        "memory_archive",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
               AND deleted_at IS NOT NULL
        )
        DELETE FROM memory_archive ma
         USING target_memories tm
         WHERE ma.id = tm.id
        """,
    ),
    (
        "memories",
        "memories",
        """
        DELETE FROM memories
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "sessions",
        "sessions",
        """
        DELETE FROM sessions
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "graeae_consultations",
        "graeae_consultations",
        """
        DELETE FROM graeae_consultations
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "kg_triples",
        "kg_triples",
        """
        DELETE FROM kg_triples
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "journal",
        "journal",
        """
        DELETE FROM journal
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "entities",
        "entities",
        """
        DELETE FROM entities
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    (
        "state",
        "state",
        """
        DELETE FROM state
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NOT NULL
        """,
    ),
    # Identity / credential tables. These do NOT carry an ``owner_id`` /
    # ``namespace`` / ``deleted_at`` triple like the memory graph does, so the
    # WHERE clauses differ. The whole point of GDPR erasure is that subject-
    # owned identity data (email, raw OAuth claims, IP addresses, user
    # agents, active credentials) must not outlive the request -- leaving
    # these rows means a hard-deleted user could still authenticate, get
    # contacted by their old email, or have their raw claims recovered
    # later. api_keys is revoked AND deleted (so any in-flight auth check
    # that raced past this DELETE still sees ``revoked = TRUE``).
    (
        "api_keys",
        "api_keys",
        """
        UPDATE api_keys
           SET revoked = TRUE,
               last_used = NULL
         WHERE user_id = $1
        """,
    ),
    (
        "oauth_sessions",
        "oauth_sessions",
        """
        DELETE FROM oauth_sessions
         WHERE user_id = $1
        """,
    ),
    (
        "oauth_identities",
        "oauth_identities",
        """
        DELETE FROM oauth_identities
         WHERE user_id = $1
        """,
    ),
    (
        "user_groups",
        "user_groups",
        """
        DELETE FROM user_groups
         WHERE user_id = $1
        """,
    ),
    # The user row itself is only removed on an all-namespace deletion. For
    # a per-namespace deletion the same logical user may own data in other
    # namespaces, so removing the row would orphan those references and
    # break auth for the surviving namespaces.
    (
        "users",
        "users",
        """
        DELETE FROM users
         WHERE id = $1
           AND $2::text IS NULL
        """,
    ),
)

_RESTORE_OWNER_NAMESPACE_SQL: tuple[tuple[str, str, str], ...] = tuple(
    (
        label,
        table,
        sql.replace("SET deleted_at = NOW()", "SET deleted_at = NULL")
        .replace("AND deleted_at IS NULL", "AND deleted_at = $3::timestamptz")
        .replace("AND mb.deleted_at IS NULL", "AND mb.deleted_at = $3::timestamptz")
        .replace("AND sm.deleted_at IS NULL", "AND sm.deleted_at = $3::timestamptz")
        .replace("AND smi.deleted_at IS NULL", "AND smi.deleted_at = $3::timestamptz")
        .replace("AND al.deleted_at IS NULL", "AND al.deleted_at = $3::timestamptz"),
    )
    for label, table, sql in (*_OWNER_NAMESPACE_SOFT_DELETE_SQL, *_SOFT_DELETE_SQL)
)


@dataclass(frozen=True)
class DeletionRequestResult:
    request_id: str
    target_user_id: str
    target_namespace: str | None
    status: str
    row_counts: dict[str, int]
    soft_deleted_at: Any
    restore_by: Any
    hard_deleted_at: Any = None
    verification_attempts: int = 0
    remaining_counts: dict[str, int] | None = None


async def restore_soft_deleted_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    soft_deleted_at: Any,
    *,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, _table, sql in _RESTORE_OWNER_NAMESPACE_SQL:
        result = await conn.execute(sql, target_user_id, target_namespace, soft_deleted_at)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts


async def hard_delete_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    *,
    requested_by: str = "deletion_request_worker",
    requested_at: Any = None,
    request_kind: str = "tombstone_collected",
    reason: str | None = None,
    source: list[str] | tuple[str, ...] | None = None,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")
    await log_target_memory_deletions(
        conn,
        target_user_id,
        target_namespace,
        requested_by=requested_by,
        requested_at=requested_at,
        request_kind=request_kind,
        reason=reason,
        source=source,
    )
    for label, _table, sql in _HARD_DELETE_SQL:
        result = await conn.execute(sql, target_user_id, target_namespace)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts


async def process_one_deletion_request(
    backend: PersistenceBackend,
) -> DeletionRequestResult | None:
    """Process one confirmed request through the lifecycle-worker ABC."""
    from mnemos.persistence.worker_lifecycle import process_one_deletion_request as process_backend

    payload = await process_backend(
        backend,
        verify_attempts=DEFAULT_VERIFY_ATTEMPTS,
        restore_days=RESTORE_GRACE_DAYS,
    )
    if payload is None:
        return None
    result = DeletionRequestResult(**payload)
    await invalidate_deletion_scope_caches(result.target_user_id, result.target_namespace)
    return result


async def process_one_hard_deletion_request(
    backend: PersistenceBackend,
) -> DeletionRequestResult | None:
    """Hard-delete one expired soft-deleted request through the lifecycle-worker ABC."""
    from mnemos.persistence.worker_lifecycle import (
        process_one_hard_deletion_request as process_backend,
    )

    payload = await process_backend(backend)
    if payload is None:
        return None
    result = DeletionRequestResult(**payload)
    await invalidate_deletion_scope_caches(result.target_user_id, result.target_namespace)
    return result


async def process_deletion_requests(
    backend: PersistenceBackend,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    aggregate: Counter[str] = Counter()
    processed = 0
    for _ in range(batch_size):
        result = await process_one_deletion_request(backend)
        if result is None:
            break
        processed += 1
        aggregate.update(result.row_counts)
        if result.status == "soft_deleted":
            logger.info(
                "soft-deleted deletion_request=%s target_user_id=%s target_namespace=%s rows=%s "
                "restore_by=%s verify_attempts=%s",
                result.request_id,
                result.target_user_id,
                result.target_namespace,
                result.row_counts,
                result.restore_by,
                result.verification_attempts,
            )
        else:
            logger.error(
                "deletion_request=%s left in sweep_verifying after %s verify attempts; remaining live rows=%s",
                result.request_id,
                result.verification_attempts,
                result.remaining_counts,
            )
    if processed:
        aggregate["requests"] = processed
    return dict(aggregate)


async def process_hard_deletion_requests(
    backend: PersistenceBackend,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    aggregate: Counter[str] = Counter()
    processed = 0
    for _ in range(batch_size):
        result = await process_one_hard_deletion_request(backend)
        if result is None:
            break
        processed += 1
        aggregate.update(result.row_counts)
        logger.info(
            "hard-deleted deletion_request=%s target_user_id=%s target_namespace=%s rows=%s hard_deleted_at=%s",
            result.request_id,
            result.target_user_id,
            result.target_namespace,
            result.row_counts,
            result.hard_deleted_at,
        )
    if processed:
        aggregate["requests"] = processed
    return dict(aggregate)


async def deletion_request_worker_loop(
    backend: PersistenceBackend,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    phase: str = "soft_delete",
    on_started: Any = None,
    on_success: Any = None,
    on_error: Any = None,
) -> None:
    """Perpetual lifecycle worker loop."""
    if phase not in {"soft_delete", "hard_delete"}:
        raise ValueError("phase must be 'soft_delete' or 'hard_delete'")
    process_batch = process_hard_deletion_requests if phase == "hard_delete" else process_deletion_requests
    if on_started is not None:
        on_started()
    while True:
        try:
            counts = await process_batch(backend, batch_size=batch_size)
            if on_success is not None:
                on_success()
            if counts:
                logger.info("deletion request worker phase=%s batch: %s", phase, counts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            logger.exception("deletion request worker phase=%s batch failed", phase)
        await asyncio.sleep(check_interval_seconds)


async def main(*, phase: str = "soft_delete") -> None:
    """Run against the configured backend, including PostgreSQL's timeout-wrapped pool."""
    from mnemos.core.lifecycle import build_configured_persistence_backend

    _backend_type, backend = await build_configured_persistence_backend()
    try:
        await deletion_request_worker_loop(backend, phase=phase)
    finally:
        await backend.close()


def _parse_cli_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Run the GDPR deletion-request worker.")
    parser.add_argument(
        "--phase",
        choices=("soft_delete", "hard_delete"),
        default="soft_delete",
        help="Deletion-request worker phase to run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    asyncio.run(main(phase=args.phase))
