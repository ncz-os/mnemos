"""Deletion primitives shared by the deletion worker and persistence.

These are the low-level operations a deletion sweep performs against a
connection: soft-deleting a target scope, counting rows that are still live,
and invalidating the caches that scope touches.

They live in the persistence layer because both layers need them and
``mnemos.persistence`` is forbidden from importing ``mnemos.workers``
(import-linter contract "persistence has no upward deps"). Previously
``persistence.worker_lifecycle`` reached up into
``workers.deletion_request_worker`` for exactly these, which also pulled
``mnemos.db`` in transitively and broke a second contract.

Operating on a connection is a persistence concern, so this is their correct
home; the worker imports them from here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Retries a verification sweep makes before declaring a scope un-drainable.
DEFAULT_VERIFY_ATTEMPTS = 5


_OWNER_NAMESPACE_SOFT_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
    (
        "memories",
        "memories",
        """
        UPDATE memories
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_versions",
        "memory_versions",
        """
        UPDATE memory_versions
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "kg_triples",
        "kg_triples",
        """
        UPDATE kg_triples
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "journal",
        "journal",
        """
        UPDATE journal
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "entities",
        "entities",
        """
        UPDATE entities
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "state",
        "state",
        """
        UPDATE state
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_consultations",
        "graeae_consultations",
        """
        UPDATE graeae_consultations
           SET deleted_at = NOW()
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
)

_SOFT_DELETE_SQL: tuple[tuple[str, str, str], ...] = (
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
        UPDATE memory_branches mb
           SET deleted_at = NOW()
          FROM target_memories tm
         WHERE mb.memory_id = tm.id
           AND mb.deleted_at IS NULL
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
        UPDATE session_messages sm
           SET deleted_at = NOW()
          FROM target_sessions ts
         WHERE sm.session_id = ts.id
           AND sm.deleted_at IS NULL
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
        UPDATE session_memory_injections smi
           SET deleted_at = NOW()
          FROM target_sessions ts
         WHERE smi.session_id = ts.id
           AND smi.deleted_at IS NULL
        """,
    ),
    (
        "sessions",
        "sessions",
        """
        UPDATE sessions
           SET deleted_at = NOW()
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
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
        UPDATE graeae_audit_log al
           SET deleted_at = NOW()
          FROM target_consultations tc
         WHERE al.consultation_id = tc.id
           AND al.deleted_at IS NULL
        """,
    ),
)

_LIVE_ROW_COUNT_SQL: tuple[tuple[str, str], ...] = (
    (
        "memories",
        """
        SELECT COUNT(*)
          FROM memories
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_versions",
        """
        SELECT COUNT(*)
          FROM memory_versions
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "kg_triples",
        """
        SELECT COUNT(*)
          FROM kg_triples
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "journal",
        """
        SELECT COUNT(*)
          FROM journal
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "entities",
        """
        SELECT COUNT(*)
          FROM entities
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "state",
        """
        SELECT COUNT(*)
          FROM state
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_consultations",
        """
        SELECT COUNT(*)
          FROM graeae_consultations
         WHERE owner_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "memory_branches",
        """
        WITH target_memories AS (
            SELECT id
              FROM memories
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM memory_branches mb
          JOIN target_memories tm ON tm.id = mb.memory_id
         WHERE mb.deleted_at IS NULL
        """,
    ),
    (
        "session_messages",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM session_messages sm
          JOIN target_sessions ts ON ts.id = sm.session_id
         WHERE sm.deleted_at IS NULL
        """,
    ),
    (
        "session_memory_injections",
        """
        WITH target_sessions AS (
            SELECT id
              FROM sessions
             WHERE user_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM session_memory_injections smi
          JOIN target_sessions ts ON ts.id = smi.session_id
         WHERE smi.deleted_at IS NULL
        """,
    ),
    (
        "sessions",
        """
        SELECT COUNT(*)
          FROM sessions
         WHERE user_id = $1
           AND ($2::text IS NULL OR namespace = $2::text)
           AND deleted_at IS NULL
        """,
    ),
    (
        "graeae_audit_log",
        """
        WITH target_consultations AS (
            SELECT id
              FROM graeae_consultations
             WHERE owner_id = $1
               AND ($2::text IS NULL OR namespace = $2::text)
        )
        SELECT COUNT(*)
          FROM graeae_audit_log al
          JOIN target_consultations tc ON tc.id = al.consultation_id
         WHERE al.deleted_at IS NULL
        """,
    ),
    # Identity / credential tables -- must also verify zero live rows before
    # marking a request hard_deleted, otherwise an all-namespace request could
    # leave raw OAuth claims / active sessions / unrevoked API keys behind
    # (audit trail for incomplete erasure).
    (
        "api_keys",
        """
        SELECT COUNT(*)
          FROM api_keys
         WHERE user_id = $1
           AND NOT revoked
        """,
    ),
    (
        "oauth_sessions",
        """
        SELECT COUNT(*)
          FROM oauth_sessions
         WHERE user_id = $1
        """,
    ),
    (
        "oauth_identities",
        """
        SELECT COUNT(*)
          FROM oauth_identities
         WHERE user_id = $1
        """,
    ),
    (
        "user_groups",
        """
        SELECT COUNT(*)
          FROM user_groups
         WHERE user_id = $1
        """,
    ),
    (
        "users",
        """
        SELECT COUNT(*)
          FROM users
         WHERE id = $1
           AND $2::text IS NULL
        """,
    ),
)


def _parse_update_count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


async def invalidate_deletion_scope_caches(
    target_user_id: str,
    target_namespace: str | None,
) -> None:
    """Evict cached search/stat responses that may include this target.

    Also bumps the visibility epoch so in-flight search writes land under
    the old epoch (orphaned) rather than leaking stale visibility.
    """
    import mnemos.core.lifecycle as _lc

    if not _lc._cache:
        return
    try:
        await _lc._cache.delete("stats:global")
        await _lc._cache.delete("stats:global:v2")
        try:
            async for key in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                await _lc._cache.delete(key)
        except Exception:
            pass
    except Exception:
        logger.warning(
            "failed to invalidate deletion caches for target_user_id=%s target_namespace=%s",
            target_user_id,
            target_namespace,
            exc_info=True,
        )
    try:
        await _lc._vis_epoch_get_incr()  # bump; errors silently
    except Exception:
        pass


def _has_live_rows(counts: dict[str, int]) -> bool:
    return any(count > 0 for count in counts.values())


async def count_live_target_rows(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, sql in _LIVE_ROW_COUNT_SQL:
        counts[label] = int(await conn.fetchval(sql, target_user_id, target_namespace) or 0)
    return counts


async def soft_delete_target(
    conn: Any,
    target_user_id: str,
    target_namespace: str | None,
    *,
    invalidate_cache: bool = True,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, _table, sql in (*_OWNER_NAMESPACE_SOFT_DELETE_SQL, *_SOFT_DELETE_SQL):
        result = await conn.execute(sql, target_user_id, target_namespace)
        counts[label] = _parse_update_count(result)
    if invalidate_cache:
        await invalidate_deletion_scope_caches(target_user_id, target_namespace)
    return counts
