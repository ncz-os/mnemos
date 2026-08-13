"""Admin lifecycle persistence helpers.

Routes in ``mnemos.api.routes.admin`` should not acquire raw driver pools for
compression, PERSEPHONE, GRAEAE, or deletion lifecycle work. This module keeps
those backend-specific operations behind a repository boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from mnemos.db.deletion_log import fetch_deletion_log
from mnemos.persistence.base import PersistenceBackend, Transaction
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect


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
    @staticmethod
    def _portable_ops(tx: Transaction) -> _Ops | None:
        try:
            dialect = transaction_dialect(tx)
        except TypeError:
            # Test doubles and legacy Postgres transaction shims expose only
            # the asyncpg-shaped connection. Supported concrete backends are
            # all resolved explicitly by transaction_dialect().
            return None
        return None if dialect == "postgres" else _Ops(tx, dialect)

    @staticmethod
    async def _portable_archive_memory(
        ops: _Ops,
        memory_id: str,
        archived_by: str,
    ) -> None:
        from mnemos.domain.persephone.runner import (
            ARCHIVE_CONTENT_PREFIX,
            ARCHIVE_SCHEMA_VERSION,
            _archive_payload,
            _compress_payload,
        )

        row = await ops.fetchone(
            "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL",
            memory_id,
        )
        if row is None:
            raise ValueError(f"memory {memory_id!r} not found")
        if row.get("archived_at") is not None:
            return
        payload = _archive_payload(row)
        compressed, original_size = _compress_payload(payload)
        if ops.dialect in {"oracle", "db2"}:
            await ops.execute(
                "INSERT INTO memory_archive "
                "(id, original_memory_id, content, metadata, archived_by, compressed_content, "
                "compression_algo, original_size_bytes, compressed_size_bytes, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                memory_id,
                memory_id,
                row.get("content") or "",
                "{}",
                archived_by,
                compressed,
                "zstd",
                original_size,
                len(compressed),
                ARCHIVE_SCHEMA_VERSION,
            )
        else:
            await ops.execute(
                "INSERT INTO memory_archive "
                "(id, archived_by, compressed_content, compression_algo, original_size_bytes, "
                "compressed_size_bytes, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                memory_id,
                archived_by,
                compressed,
                "zstd",
                original_size,
                len(compressed),
                ARCHIVE_SCHEMA_VERSION,
            )
        now = datetime.now(timezone.utc)
        changed = await ops.execute(
            "UPDATE memories SET content = ?, verbatim_content = NULL, archived_at = ?, updated = ? "
            "WHERE id = ? AND archived_at IS NULL AND deleted_at IS NULL",
            f"{ARCHIVE_CONTENT_PREFIX}{memory_id}",
            now,
            now,
            memory_id,
        )
        if changed != 1:
            raise RuntimeError(f"memory {memory_id!r} was not archived")

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

        ops = self._portable_ops(tx)
        if ops is not None:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=archive_after_days)
            base = (
                "SELECT id FROM memories WHERE deleted_at IS NULL AND archived_at IS NULL "
                "AND consolidated_into IS NULL AND (last_recalled_at IS NULL OR last_recalled_at < ?) "
                "AND created < ? AND namespace = ? ORDER BY created ASC"
            )
            if ops.dialect == "mysql":
                sql = base + " LIMIT ? FOR UPDATE SKIP LOCKED"
            elif ops.dialect in {"oracle", "db2"}:
                sql = f"SELECT id FROM ({base}) WHERE ROWNUM <= ? FOR UPDATE SKIP LOCKED"
            else:
                sql = base + " LIMIT ?"
            rows = await ops.fetchall(sql, cutoff_dt, cutoff_dt, namespace, int(batch_size))
            for row in rows:
                await self._portable_archive_memory(ops, row["id"], DEFAULT_ARCHIVED_BY)
            return len(rows)

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

        ops = self._portable_ops(tx)
        if ops is not None:
            await self._portable_archive_memory(ops, memory_id, archived_by)
            return
        await archive_memory(_conn(tx), memory_id, archived_by)

    async def fetch_memory_archive_state(self, tx: Transaction, memory_id: str) -> Any | None:
        ops = self._portable_ops(tx)
        if ops is not None:
            return await ops.fetchone(
                "SELECT id, owner_id, namespace, archived_at FROM memories "
                "WHERE id = ? AND deleted_at IS NULL",
                memory_id,
            )
        return await _conn(tx).fetchrow(
            """
            SELECT id, owner_id, namespace, archived_at
              FROM memories
             WHERE id = $1
               AND deleted_at IS NULL
            """,
            memory_id,
        )

    async def fetch_memory_archive_snapshot(self, tx: Transaction, memory_id: str) -> Any | None:
        ops = self._portable_ops(tx)
        if ops is not None:
            return await ops.fetchone(
                "SELECT content, category, subcategory, metadata FROM memories "
                "WHERE id = ? AND archived_at IS NULL AND deleted_at IS NULL",
                memory_id,
            )
        return await _conn(tx).fetchrow(
            "SELECT content, category, subcategory, metadata FROM memories "
            "WHERE id = $1 AND archived_at IS NULL AND deleted_at IS NULL",
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

        ops = self._portable_ops(tx)
        if ops is not None:
            row = await ops.fetchone(
                "SELECT m.id, m.archived_at, a.compressed_content, a.compression_algo "
                "FROM memories m JOIN memory_archive a ON a.id = m.id "
                "WHERE m.id = ? AND m.deleted_at IS NULL",
                memory_id,
            )
            if row is None or row.get("archived_at") is None:
                raise ValueError(f"memory {memory_id!r} is not archived")
            if row.get("compression_algo") != "zstd":
                raise ValueError("unsupported memory archive compression_algo")
            from mnemos.domain.persephone.runner import _decompress_payload

            memory = _decompress_payload(row["compressed_content"])["memory"]
            predicates = ["id = ?", "archived_at IS NOT NULL", "deleted_at IS NULL"]
            params: list[Any] = [
                memory["content"],
                memory.get("verbatim_content"),
                json.dumps(memory.get("metadata") or {}, sort_keys=True, separators=(",", ":")),
                datetime.now(timezone.utc),
                memory_id,
            ]
            if expected_owner_id is not None:
                predicates.append("owner_id = ?")
                params.append(expected_owner_id)
            if expected_namespace is not None:
                predicates.append("namespace = ?")
                params.append(expected_namespace)
            changed = await ops.execute(
                "UPDATE memories SET content = ?, verbatim_content = ?, metadata = ?, "
                f"archived_at = NULL, updated = ? WHERE {' AND '.join(predicates)}",
                *params,
            )
            if changed != 1:
                raise RuntimeError(f"memory {memory_id!r} was not restored")
            await ops.execute("DELETE FROM memory_archive WHERE id = ?", memory_id)
            return
        await restore_memory(
            _conn(tx),
            memory_id,
            restored_by,
            expected_owner_id=expected_owner_id,
            expected_namespace=expected_namespace,
        )

    async def fetch_persephone_status(self, tx: Transaction, *, namespace: str | None) -> tuple[int, Any, Any]:
        ops = self._portable_ops(tx)
        if ops is not None:
            scope = " WHERE m.namespace = ?" if namespace is not None else ""
            args = (namespace,) if namespace is not None else ()
            archive_row = await ops.fetchone(
                "SELECT COUNT(*) AS archived_count, MAX(a.archived_at) AS last_run_at "
                f"FROM memory_archive a JOIN memories m ON m.id = a.id{scope}",
                *args,
            )
            oldest_where = "deleted_at IS NULL AND archived_at IS NULL AND consolidated_into IS NULL"
            if namespace is not None:
                oldest_where += " AND namespace = ?"
            oldest = await ops.scalar(
                f"SELECT MIN(COALESCE(last_recalled_at, created)) FROM memories WHERE {oldest_where}",
                *args,
            )
            return (
                int((archive_row or {}).get("archived_count") or 0),
                (archive_row or {}).get("last_run_at"),
                oldest,
            )
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
        ops = self._portable_ops(tx)
        if ops is not None:
            clauses = ["requested_at >= ?", "requested_at <= ?"]
            args: list[Any] = [from_ts, to_ts]
            if owner_id:
                clauses.append("owner_id = ?")
                args.append(owner_id)
            where = " AND ".join(clauses)
            total = int(await ops.scalar(f"SELECT COUNT(*) FROM deletion_log WHERE {where}", *args) or 0)
            offset = (page - 1) * page_size
            ordered = f"SELECT * FROM deletion_log WHERE {where} ORDER BY requested_at DESC, executed_at DESC, id DESC"
            if ops.dialect in {"oracle", "db2"}:
                rows = await ops.fetchall(
                    "SELECT * FROM (SELECT q.*, ROW_NUMBER() OVER (ORDER BY requested_at DESC, "
                    f"executed_at DESC, id DESC) AS rn FROM deletion_log q WHERE {where}) "
                    "WHERE rn > ? AND rn <= ?",
                    *args,
                    offset,
                    offset + page_size,
                )
            else:
                rows = await ops.fetchall(ordered + " LIMIT ? OFFSET ?", *args, page_size, offset)
            for row in rows:
                if isinstance(row.get("source"), str):
                    try:
                        row["source"] = json.loads(row["source"])
                    except json.JSONDecodeError:
                        row["source"] = []
            return rows, total
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
        ops = self._portable_ops(tx)
        if ops is not None:
            if ops.dialect == "mysql":
                lock_name = f"mnemos:deletion:{hashlib.sha256(target_user_id.encode()).hexdigest()}"
                acquired = await ops.scalar("SELECT GET_LOCK(?, ?)", lock_name, 10)
                if int(acquired or 0) != 1:
                    raise RuntimeError("timed out acquiring deletion-request scope lock")
                hold = getattr(tx, "hold_named_lock", None)
                if callable(hold):
                    hold(lock_name)
            elif ops.dialect in {"oracle", "db2"}:
                # These backends have no transaction-scoped advisory-lock
                # capability. Request creation is rare; a short table lock is
                # the portable way to serialize NULL-vs-specific overlap
                # checks that a simple unique key cannot represent.
                await ops.execute("LOCK TABLE deletion_requests IN EXCLUSIVE MODE")
            overlap = await ops.fetchone(
                "SELECT id, target_namespace, status FROM deletion_requests "
                "WHERE target_user_id = ? "
                "AND status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted', 'hard_deleting') "
                "AND (target_namespace IS NULL OR TRIM(target_namespace) = '' "
                "OR ? IS NULL OR target_namespace = ?)",
                target_user_id,
                target_namespace,
                target_namespace,
            )
            if overlap is not None:
                raise DeletionRequestOverlapError(overlap)
            request_id = str(uuid.uuid4())
            try:
                await ops.execute(
                    "INSERT INTO deletion_requests "
                    "(id, target_user_id, target_namespace, requested_by, notes, status) "
                    "VALUES (?, ?, ?, ?, ?, 'requested')",
                    request_id,
                    target_user_id,
                    target_namespace,
                    requested_by,
                    notes,
                )
            except Exception as exc:
                if "Unique" in type(exc).__name__ or "Integrity" in type(exc).__name__ or "UNIQUE" in str(exc).upper():
                    raise DeletionRequestActiveDuplicateError from exc
                raise
            row = await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id)
            if row is None:
                raise RuntimeError("created deletion request could not be read back")
            return row
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
        ops = self._portable_ops(tx)
        if ops is not None:
            clauses: list[str] = []
            args: list[Any] = []
            if status:
                clauses.append("status = ?")
                args.append(status)
            if target_user_id:
                clauses.append("target_user_id = ?")
                args.append(target_user_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            ordered = f"SELECT * FROM deletion_requests{where} ORDER BY requested_at DESC"
            if ops.dialect in {"oracle", "db2"}:
                return await ops.fetchall(f"SELECT * FROM ({ordered}) WHERE ROWNUM <= ?", *args, limit)
            return await ops.fetchall(ordered + " LIMIT ?", *args, limit)
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
        ops = self._portable_ops(tx)
        if ops is not None:
            return await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id)
        return await _conn(tx).fetchrow(
            "SELECT * FROM deletion_requests WHERE id = $1::uuid",
            request_id,
        )

    async def confirm_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        ops = self._portable_ops(tx)
        if ops is not None:
            changed = await ops.execute(
                "UPDATE deletion_requests SET status = 'confirmed', "
                "confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ? "
                "AND status IN ('requested', 'confirmed')",
                datetime.now(timezone.utc),
                request_id,
            )
            return await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id) if changed else None
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
        ops = self._portable_ops(tx)
        if ops is not None:
            changed = await ops.execute(
                "UPDATE deletion_requests SET status = 'cancelled' WHERE id = ? "
                "AND status IN ('requested', 'confirmed')",
                request_id,
            )
            return await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id) if changed else None
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
        ops = self._portable_ops(tx)
        if ops is not None:
            return await ops.fetchone("SELECT status FROM deletion_requests WHERE id = ?", request_id)
        return await _conn(tx).fetchrow(
            "SELECT status FROM deletion_requests WHERE id = $1::uuid",
            request_id,
        )

    async def lock_deletion_request(self, tx: Transaction, request_id: str) -> Any | None:
        ops = self._portable_ops(tx)
        if ops is not None:
            suffix = "" if ops.dialect == "sqlite" else " FOR UPDATE"
            return await ops.fetchone(f"SELECT * FROM deletion_requests WHERE id = ?{suffix}", request_id)
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

        ops = self._portable_ops(tx)
        if ops is not None:
            from mnemos.persistence.worker_lifecycle import restore_soft_deleted_target as restore_portable

            await restore_portable(
                tx,
                user_id=existing["target_user_id"],
                namespace=existing["target_namespace"],
                soft_deleted_at=existing["soft_deleted_at"],
            )
            changed = await ops.execute(
                "UPDATE deletion_requests SET status = 'restored', restored_at = ? "
                "WHERE id = ? AND status = 'soft_deleted'",
                datetime.now(timezone.utc),
                request_id,
            )
            if changed != 1:
                return None
            return await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id)
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

        ops = self._portable_ops(tx)
        if ops is not None:
            from mnemos.persistence.worker_lifecycle import hard_delete_target as hard_delete_portable

            request = dict(existing)
            request.update(
                requested_by=requested_by,
                request_kind="admin_purge",
                notes=reason,
                source=["admin.force_purge_deletion_request", request_id],
            )
            await hard_delete_portable(tx, request)
            changed = await ops.execute(
                "UPDATE deletion_requests SET status = 'hard_deleted', hard_deleted_at = ? "
                "WHERE id = ? AND status = 'soft_deleted'",
                datetime.now(timezone.utc),
                request_id,
            )
            if changed != 1:
                return None
            return await ops.fetchone("SELECT * FROM deletion_requests WHERE id = ?", request_id)
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
