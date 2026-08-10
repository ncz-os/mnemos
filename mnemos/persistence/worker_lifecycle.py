"""Backend-neutral durable operations for deletion and PERSEPHONE workers.

The workers call this repository when handed a persistence backend instead of
an asyncpg pool.  Every operation runs inside ``backend.transactional()``;
SQLite's ``BEGIN IMMEDIATE`` and the server backends' row locks make claims
atomic across processes rather than relying on process-local worker state.
"""

from __future__ import annotations

import inspect
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def backend_dialect(backend: Any) -> str:
    name = type(backend).__name__.lower()
    if "sqlite" in name:
        return "sqlite"
    if "mariadb" in name:
        return "mysql"
    if "mysql" in name:
        return "mysql"
    if "db2" in name:
        return "db2"
    if "oracle" in name:
        return "oracle"
    raise TypeError(f"unsupported lifecycle worker backend: {type(backend).__name__}")


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _Ops:
    def __init__(self, tx: Any, dialect: str) -> None:
        self.conn = tx.conn
        self.dialect = dialect

    def sql(self, template: str) -> str:
        marker = "?" if self.dialect == "sqlite" else "%s" if self.dialect == "mysql" else ":{}"
        index = 0
        pieces: list[str] = []
        for piece in template.split("?")[:-1]:
            pieces.append(piece)
            index += 1
            pieces.append(marker.format(index) if "{}" in marker else marker)
        pieces.append(template.split("?")[-1])
        return "".join(pieces)

    async def _cursor(self, sql: str, params: tuple[Any, ...]) -> Any:
        if self.dialect == "sqlite":
            return await _await(self.conn.execute(self.sql(sql), params))
        cursor = self.conn.cursor()
        cursor = await _await(cursor)
        await _await(cursor.execute(self.sql(sql), params))
        return cursor

    async def execute(self, sql: str, *params: Any) -> int:
        cursor = await self._cursor(sql, tuple(params))
        try:
            return max(0, int(getattr(cursor, "rowcount", 0) or 0))
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _await(close())

    async def fetchone(self, sql: str, *params: Any) -> dict[str, Any] | None:
        cursor = await self._cursor(sql, tuple(params))
        try:
            row = await _await(cursor.fetchone())
            if row is None:
                return None
            if isinstance(row, dict):
                return row
            keys = [str(col[0]).lower() for col in (cursor.description or ())]
            return dict(zip(keys, row))
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _await(close())

    async def scalar(self, sql: str, *params: Any) -> Any:
        row = await self.fetchone(sql, *params)
        return next(iter(row.values())) if row else None

    async def fetchall(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cursor = await self._cursor(sql, tuple(params))
        try:
            rows = await _await(cursor.fetchall())
            if not rows:
                return []
            keys = [str(col[0]).lower() for col in (cursor.description or ())]
            return [row if isinstance(row, dict) else dict(zip(keys, row)) for row in rows]
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _await(close())


_OWNER_TABLES = (
    ("memories", "owner_id"),
    ("memory_versions", "owner_id"),
    ("kg_triples", "owner_id"),
    ("journal", "owner_id"),
    ("entities", "owner_id"),
    ("state", "owner_id"),
    ("graeae_consultations", "owner_id"),
    ("sessions", "user_id"),
)

_RELATED_TABLES = (
    ("memory_branches", "memory_id", "memories", "id", "owner_id"),
    ("session_messages", "session_id", "sessions", "id", "user_id"),
    ("session_memory_injections", "session_id", "sessions", "id", "user_id"),
    ("graeae_audit_log", "consultation_id", "graeae_consultations", "id", "owner_id"),
)


def _scope(column: str) -> str:
    return f"{column} = ? AND (? IS NULL OR namespace = ?)"


async def _claim(ops: _Ops, *, hard: bool) -> dict[str, Any] | None:
    if hard:
        where = "status = 'soft_deleted' AND restore_by < ?"
        order = "restore_by ASC"
        params: tuple[Any, ...] = (datetime.now(timezone.utc),)
    else:
        where = "status = 'confirmed'"
        order = "confirmed_at ASC, requested_at ASC"
        params = ()
    base_sql = f"SELECT * FROM deletion_requests WHERE {where} ORDER BY {order}"
    if ops.dialect == "mysql":
        sql = base_sql + " LIMIT 1 FOR UPDATE SKIP LOCKED"
    elif ops.dialect in {"oracle", "db2"}:
        # Oracle rejects FETCH FIRST combined with FOR UPDATE (ORA-02014).
        # Db2's Oracle-compatibility cursor supports the same ROWNUM form.
        sql = f"SELECT * FROM ({base_sql}) WHERE ROWNUM <= 1 FOR UPDATE SKIP LOCKED"
    else:
        sql = base_sql + " LIMIT 1"
    row = await ops.fetchone(sql, *params)
    if row is None:
        return None
    expected = "soft_deleted" if hard else "confirmed"
    next_status = "hard_deleting" if hard else "sweep_verifying"
    changed = await ops.execute(
        "UPDATE deletion_requests SET status = ? WHERE id = ? AND status = ?",
        next_status,
        row["id"],
        expected,
    )
    return row if changed == 1 else None


async def _scope_counts(ops: _Ops, user_id: str, namespace: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, owner_col in _OWNER_TABLES:
        counts[table] = int(
            await ops.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {_scope(owner_col)} AND deleted_at IS NULL",
                user_id,
                namespace,
                namespace,
            )
            or 0
        )
    for table, fk, parent, parent_id, owner_col in _RELATED_TABLES:
        counts[table] = int(
            await ops.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {fk} IN "
                f"(SELECT {parent_id} FROM {parent} WHERE {_scope(owner_col)}) "
                "AND deleted_at IS NULL",
                user_id,
                namespace,
                namespace,
            )
            or 0
        )
    return counts


async def _soft_delete(ops: _Ops, user_id: str, namespace: str | None, at: datetime) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, owner_col in _OWNER_TABLES:
        counts[table] = await ops.execute(
            f"UPDATE {table} SET deleted_at = ? WHERE {_scope(owner_col)} AND deleted_at IS NULL",
            at,
            user_id,
            namespace,
            namespace,
        )
    for table, fk, parent, parent_id, owner_col in _RELATED_TABLES:
        counts[table] = await ops.execute(
            f"UPDATE {table} SET deleted_at = ? WHERE {fk} IN "
            f"(SELECT {parent_id} FROM {parent} WHERE {_scope(owner_col)}) AND deleted_at IS NULL",
            at,
            user_id,
            namespace,
            namespace,
        )
    return counts


async def process_one_deletion_request(backend: Any, *, verify_attempts: int, restore_days: int) -> dict[str, Any] | None:
    dialect = backend_dialect(backend)
    async with backend.transactional() as tx:
        ops = _Ops(tx, dialect)
        request = await _claim(ops, hard=False)
        if request is None:
            return None
        now = datetime.now(timezone.utc)
        counts = await _soft_delete(ops, request["target_user_id"], request.get("target_namespace"), now)
        remaining: dict[str, int] = {}
        for attempt in range(1, verify_attempts + 1):
            remaining = await _scope_counts(ops, request["target_user_id"], request.get("target_namespace"))
            if not any(remaining.values()):
                restore_by = now + timedelta(days=restore_days)
                await ops.execute(
                    "UPDATE deletion_requests SET status = 'soft_deleted', soft_deleted_at = ?, restore_by = ? "
                    "WHERE id = ? AND status = 'sweep_verifying'",
                    now,
                    restore_by,
                    request["id"],
                )
                return {
                    "request_id": str(request["id"]),
                    "target_user_id": request["target_user_id"],
                    "target_namespace": request.get("target_namespace"),
                    "status": "soft_deleted",
                    "row_counts": counts,
                    "soft_deleted_at": now,
                    "restore_by": restore_by,
                    "verification_attempts": attempt,
                    "remaining_counts": remaining,
                }
            retry = await _soft_delete(ops, request["target_user_id"], request.get("target_namespace"), now)
            for label, count in retry.items():
                counts[label] = counts.get(label, 0) + count
        await ops.execute(
            "UPDATE deletion_requests SET status = 'confirmed' WHERE id = ? AND status = 'sweep_verifying'",
            request["id"],
        )
        return {
            "request_id": str(request["id"]),
            "target_user_id": request["target_user_id"],
            "target_namespace": request.get("target_namespace"),
            "status": "confirmed",
            "row_counts": counts,
            "soft_deleted_at": None,
            "restore_by": None,
            "verification_attempts": verify_attempts,
            "remaining_counts": remaining,
        }


async def process_one_hard_deletion_request(backend: Any) -> dict[str, Any] | None:
    dialect = backend_dialect(backend)
    async with backend.transactional() as tx:
        ops = _Ops(tx, dialect)
        request = await _claim(ops, hard=True)
        if request is None:
            return None
        user_id = request["target_user_id"]
        namespace = request.get("target_namespace")
        counts: dict[str, int] = {}
        memories = await ops.fetchall(
            f"SELECT id, content, owner_id, namespace FROM memories WHERE {_scope('owner_id')} "
            "AND deleted_at IS NOT NULL",
            user_id,
            namespace,
            namespace,
        )
        for memory in memories:
            content_hash = hashlib.sha256(str(memory.get("content") or "").encode()).hexdigest()
            values = (
                memory["id"], content_hash, memory.get("owner_id"), memory.get("namespace"),
                request.get("requested_by") or "deletion_request_worker",
                request.get("requested_at") or datetime.now(timezone.utc),
                "tombstone_collected", request.get("notes"),
                json.dumps(["deletion_request_worker", str(request["id"])]),
            )
            columns = (
                "memory_id, content_hash, owner_id, namespace, requested_by, requested_at, "
                "request_kind, reason, source"
            )
            if dialect in {"oracle", "db2"}:
                await ops.execute(
                    f"INSERT INTO deletion_log ({columns}, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    *values,
                    "completed",
                )
            else:
                await ops.execute(
                    f"INSERT INTO deletion_log (id, {columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    uuid.uuid4().hex,
                    *values,
                )
        # Children first for FK safety. Archive rows are also children of memories.
        for table, fk, parent, parent_id, owner_col in _RELATED_TABLES:
            counts[table] = await ops.execute(
                f"DELETE FROM {table} WHERE {fk} IN "
                f"(SELECT {parent_id} FROM {parent} WHERE {_scope(owner_col)}) AND deleted_at IS NOT NULL",
                user_id,
                namespace,
                namespace,
            )
        counts["memory_archive"] = await ops.execute(
            "DELETE FROM memory_archive WHERE id IN "
            f"(SELECT id FROM memories WHERE {_scope('owner_id')} AND deleted_at IS NOT NULL)",
            user_id,
            namespace,
            namespace,
        )
        for table, owner_col in _OWNER_TABLES:
            counts[table] = await ops.execute(
                f"DELETE FROM {table} WHERE {_scope(owner_col)} AND deleted_at IS NOT NULL",
                user_id,
                namespace,
                namespace,
            )
        now = datetime.now(timezone.utc)
        await ops.execute(
            "UPDATE deletion_requests SET status = 'hard_deleted', hard_deleted_at = ? "
            "WHERE id = ? AND status = 'hard_deleting'",
            now,
            request["id"],
        )
        return {
            "request_id": str(request["id"]),
            "target_user_id": user_id,
            "target_namespace": namespace,
            "status": "hard_deleted",
            "row_counts": counts,
            "soft_deleted_at": request.get("soft_deleted_at"),
            "restore_by": request.get("restore_by"),
            "hard_deleted_at": now,
        }


async def sweep_for_archival(
    backend: Any,
    *,
    namespace: str,
    archive_after_days: int,
    batch_size: int,
) -> int:
    """Archive a cold batch using the active backend transaction."""
    if archive_after_days < 1 or batch_size < 1:
        raise ValueError("archive_after_days and batch_size must be >= 1")
    from mnemos.domain.persephone.runner import (
        ARCHIVE_CONTENT_PREFIX,
        ARCHIVE_SCHEMA_VERSION,
        DEFAULT_ARCHIVED_BY,
        _archive_payload,
        _compress_payload,
    )

    dialect = backend_dialect(backend)
    cutoff = datetime.now(timezone.utc) - timedelta(days=archive_after_days)
    async with backend.transactional() as tx:
        ops = _Ops(tx, dialect)
        # Fetch ids one at a time through the common cursor wrapper so result
        # metadata is normalized to lower-case mapping keys on enterprise DBs.
        base_sql = (
            "SELECT id FROM memories WHERE deleted_at IS NULL AND archived_at IS NULL "
            "AND consolidated_into IS NULL AND (last_recalled_at IS NULL OR last_recalled_at < ?) "
            "AND created < ? AND namespace = ? ORDER BY created ASC"
        )
        if dialect == "mysql":
            claim_sql = base_sql + " LIMIT ? FOR UPDATE SKIP LOCKED"
        elif dialect in {"oracle", "db2"}:
            claim_sql = f"SELECT id FROM ({base_sql}) WHERE ROWNUM <= ? FOR UPDATE SKIP LOCKED"
        else:
            claim_sql = base_sql + " LIMIT ?"
        cursor = await ops._cursor(
            claim_sql,
            (cutoff, cutoff, namespace, int(batch_size)),
        )
        try:
            raw_rows = await _await(cursor.fetchall())
            keys = [str(col[0]).lower() for col in (cursor.description or ())]
            rows = [row if isinstance(row, dict) else dict(zip(keys, row)) for row in raw_rows]
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _await(close())

        archived = 0
        for item in rows:
            row = await ops.fetchone(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL",
                item["id"],
            )
            if row is None:
                continue
            payload = _archive_payload(row)
            compressed, original_size = _compress_payload(payload)
            if dialect in {"oracle", "db2"}:
                # The forward migration preserves legacy NOT NULL columns while
                # adding the canonical compressed payload. Populate both until
                # operators complete the documented legacy-column cleanup.
                await ops.execute(
                    "INSERT INTO memory_archive "
                    "(id, original_memory_id, content, metadata, archived_by, compressed_content, "
                    "compression_algo, original_size_bytes, compressed_size_bytes, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row["id"],
                    row["id"],
                    row.get("content") or "",
                    "{}",
                    DEFAULT_ARCHIVED_BY,
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
                    row["id"],
                    DEFAULT_ARCHIVED_BY,
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
                f"{ARCHIVE_CONTENT_PREFIX}{row['id']}",
                now,
                now,
                row["id"],
            )
            if changed != 1:
                raise RuntimeError(f"memory {row['id']!r} was not archived")
            archived += 1
        return archived
