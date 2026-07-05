"""Admin lifecycle persistence helpers.

Routes in ``mnemos.api.routes.admin`` should not acquire raw driver pools for
compression, PERSEPHONE, GRAEAE, or deletion lifecycle work. This module keeps
those backend-specific operations behind a repository boundary.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from mnemos.db.deletion_log import fetch_deletion_log
from mnemos.persistence.base import PersistenceBackend, Transaction


def _conn(tx: Transaction) -> Any:
    conn = getattr(tx, "conn", None)
    if conn is None:
        raise TypeError(f"{type(tx).__name__} does not expose a repository connection")
    return conn


class DeletionRequestOverlapError(ValueError):
    def __init__(self, row: Any):
        super().__init__("overlapping deletion request")
        self.row = row


class DeletionRequestActiveDuplicateError(ValueError):
    pass


class AdminLifecycleRepository:
    async def sweep_for_archival(
        self,
        tx: Transaction,
        *,
        namespace: str,
        archive_after_days: int,
        batch_size: int,
    ) -> int:
        from mnemos.domain.persephone.runner import (
            DEFAULT_ARCHIVED_BY,
            _ELIGIBLE_SQL,
            archive_memory,
        )

        if archive_after_days < 1:
            raise ValueError("archive_after_days must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        conn = _conn(tx)
        rows = await conn.fetch(
            _ELIGIBLE_SQL,
            namespace,
            int(archive_after_days),
            int(batch_size),
        )
        archived = 0
        for row in rows:
            await archive_memory(conn, row["id"], DEFAULT_ARCHIVED_BY)
            archived += 1
        return archived

    async def archive_memory(self, tx: Transaction, memory_id: str, archived_by: str) -> None:
        from mnemos.domain.persephone.runner import archive_memory

        await archive_memory(_conn(tx), memory_id, archived_by)

    async def fetch_memory_archive_state(self, tx: Transaction, memory_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            """
            SELECT id, owner_id, namespace, archived_at
              FROM memories
             WHERE id = $1
               AND deleted_at IS NULL
            """,
            memory_id,
        )

    async def restore_memory(
        self,
        tx: Transaction,
        memory_id: str,
        restored_by: str,
        *,
        expected_owner_id: str | None = None,
        expected_namespace: str | None = None,
    ) -> None:
        from mnemos.domain.persephone.runner import restore_memory

        await restore_memory(
            _conn(tx),
            memory_id,
            restored_by,
            expected_owner_id=expected_owner_id,
            expected_namespace=expected_namespace,
        )

    async def fetch_persephone_status(self, tx: Transaction, *, namespace: str | None) -> tuple[int, Any, Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if namespace is not None:
            args.append(namespace)
            clauses.append(f"m.namespace = ${len(args)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        oldest_clauses = [
            "deleted_at IS NULL",
            "archived_at IS NULL",
            "consolidated_into IS NULL",
        ]
        oldest_args: list[Any] = []
        if namespace is not None:
            oldest_args.append(namespace)
            oldest_clauses.append(f"namespace = ${len(oldest_args)}")

        conn = _conn(tx)
        archive_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS archived_count,
                   MAX(a.archived_at) AS last_run_at
              FROM memory_archive a
              JOIN memories m ON m.id = a.id
              {where}
            """,
            *args,
        )
        oldest_unrecalled = await conn.fetchval(
            f"""
            SELECT MIN(COALESCE(last_recalled_at, created))
              FROM memories
             WHERE {" AND ".join(oldest_clauses)}
            """,
            *oldest_args,
        )
        last_run_at = archive_row["last_run_at"] if archive_row else None
        archived_count = int(archive_row["archived_count"] or 0) if archive_row else 0
        return archived_count, last_run_at, oldest_unrecalled

    async def reload_graeae_providers(self, backend: PersistenceBackend, engine: Any) -> dict[str, str]:
        pool = getattr(backend, "pool", None) or getattr(backend, "_pool", None)
        if pool is None:
            raise NotImplementedError(f"{type(backend).__name__} does not expose a pool for GRAEAE registry reload")
        return await engine.reload_from_registry(pool)

    async def fetch_deletion_log(
        self,
        tx: Transaction,
        *,
        from_ts: datetime,
        to_ts: datetime,
        owner_id: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Any], int]:
        return await fetch_deletion_log(
            _conn(tx),
            from_ts=from_ts,
            to_ts=to_ts,
            owner_id=owner_id,
            page=page,
            page_size=page_size,
        )

    async def create_deletion_request(
        self,
        tx: Transaction,
        *,
        target_user_id: str,
        target_namespace: str | None,
        requested_by: str,
        notes: str | None,
    ) -> Any:
        digest = hashlib.blake2b(target_user_id.encode("utf-8"), digest_size=8).digest()
        lock_key_unsigned = int.from_bytes(digest, "big", signed=False)
        lock_key = lock_key_unsigned - (1 << 63)
        conn = _conn(tx)
        try:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
            overlap = await conn.fetchrow(
                """
                SELECT id, target_namespace, status
                  FROM deletion_requests
                 WHERE target_user_id = $1
                   AND status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted')
                   AND (
                        mnemos_is_blank_namespace(target_namespace)
                     OR $2::text IS NULL
                     OR target_namespace = $2::text
                   )
                 LIMIT 1
                """,
                target_user_id,
                target_namespace,
            )
            if overlap is not None:
                raise DeletionRequestOverlapError(overlap)
            return await conn.fetchrow(
                """
                INSERT INTO deletion_requests
                  (target_user_id, target_namespace, requested_by, notes)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                target_user_id,
                target_namespace,
                requested_by,
                notes,
            )
        except Exception as exc:
            # Backend-agnostic duplicate-key guard. Postgres raises
            # asyncpg.UniqueViolationError; Oracle raises
            # oracledb.IntegrityError (ORA-00001); SQLite raises
            # sqlite3.IntegrityError. The advisory-lock + SELECT check
            # above makes the race window narrow — any duplicate-key
            # error that escapes is an active duplicate.
            if "UniqueViolation" in type(exc).__name__ or "IntegrityError" in type(exc).__name__ or "UNIQUE constraint" in str(exc):
                raise DeletionRequestActiveDuplicateError from exc
            raise

    async def list_deletion_requests(
        self,
        tx: Transaction,
        *,
        status: str | None,
        target_user_id: str | None,
        limit: int,
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if target_user_id:
            args.append(target_user_id)
            clauses.append(f"target_user_id = ${len(args)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        sql = f"SELECT * FROM deletion_requests{where} ORDER BY requested_at DESC LIMIT ${len(args)}"
        return await _conn(tx).fetch(sql, *args)

    async def get_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            "SELECT * FROM deletion_requests WHERE id = $1::uuid",
            request_id,
        )

    async def confirm_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'confirmed',
                   confirmed_at = COALESCE(confirmed_at, NOW())
             WHERE id = $1::uuid
               AND status IN ('requested', 'confirmed')
            RETURNING *
            """,
            request_id,
        )

    async def cancel_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'cancelled'
             WHERE id = $1::uuid
               AND status IN ('requested', 'confirmed')
            RETURNING *
            """,
            request_id,
        )

    async def fetch_deletion_request_status(self, tx: Transaction, request_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            "SELECT status FROM deletion_requests WHERE id = $1::uuid",
            request_id,
        )

    async def lock_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        return await _conn(tx).fetchrow(
            """
            SELECT *
              FROM deletion_requests
             WHERE id = $1::uuid
             FOR UPDATE
            """,
            request_id,
        )

    async def restore_soft_deleted_request(self, tx: Transaction, *, request_id: str, existing: Any) -> Any:
        from mnemos.workers.deletion_request_worker import restore_soft_deleted_target

        conn = _conn(tx)
        await restore_soft_deleted_target(
            conn,
            existing["target_user_id"],
            existing["target_namespace"],
            existing["soft_deleted_at"],
            invalidate_cache=False,
        )
        return await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'restored',
                   restored_at = NOW()
             WHERE id = $1::uuid
               AND status = 'soft_deleted'
            RETURNING *
            """,
            request_id,
        )

    async def force_purge_soft_deleted_request(
        self,
        tx: Transaction,
        *,
        request_id: str,
        existing: Any,
        requested_by: str,
        reason: str | None,
    ) -> Any:
        from mnemos.workers.deletion_request_worker import hard_delete_target

        conn = _conn(tx)
        await hard_delete_target(
            conn,
            existing["target_user_id"],
            existing["target_namespace"],
            requested_by=requested_by,
            requested_at=None,
            request_kind="admin_purge",
            reason=reason,
            source=["admin.force_purge_deletion_request", request_id],
            invalidate_cache=False,
        )
        return await conn.fetchrow(
            """
            UPDATE deletion_requests
               SET status = 'hard_deleted',
                   hard_deleted_at = NOW()
             WHERE id = $1::uuid
               AND status = 'soft_deleted'
            RETURNING *
            """,
            request_id,
        )
