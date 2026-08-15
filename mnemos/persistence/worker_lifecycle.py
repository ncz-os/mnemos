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


def transaction_dialect(tx: Any) -> str:
    """Resolve a lifecycle dialect from a backend transaction wrapper."""
    tx_name = type(tx).__name__.lower()
    conn_name = type(getattr(tx, "conn", None)).__name__.lower()
    conn_module = type(getattr(tx, "conn", None)).__module__.lower()
    if "postgres" in tx_name:
        return "postgres"
    if "sqlite" in tx_name or "sqlite" in conn_name:
        return "sqlite"
    if "mysql" in tx_name or "mariadb" in tx_name:
        return "mysql"
    if "db2" in conn_name or "db2" in conn_module:
        return "db2"
    if "oracle" in tx_name or "oracle" in conn_name or "oracledb" in conn_module:
        return "oracle"
    raise TypeError(f"unsupported lifecycle transaction: {type(tx).__name__}")


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _Ops:
    def __init__(self, tx: Any, dialect: str) -> None:
        self.conn = tx.conn
        self.dialect = dialect

    def sql(self, template: str) -> str:
        marker = "?" if self.dialect in {"sqlite", "db2"} else "%s" if self.dialect == "mysql" else ":{}"
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


# Identity / credential tables. These DO NOT carry the ``owner_id`` /
# ``namespace`` / ``deleted_at`` triple that the rest of the deletion
# scope uses, so they are deleted directly during the hard-delete phase
# (no soft-delete fence). On Postgres the column is ``user_id``; on
# Oracle / Db2 it is ``owner_id``. The backend-neutral path dispatches
# via ``_identity_user_column`` so the same call shape works on every
# dialect.
_IDENTITY_TABLES: tuple[str, ...] = (
    "api_keys",
    "oauth_sessions",
    "oauth_identities",
    "user_groups",
)


def _identity_user_column(dialect: str) -> str:
    """Postgres uses ``user_id`` on api_keys / oauth_* / user_groups; the
    Oracle / Db2 ports use ``owner_id`` (with a parallel ``namespace``
    column). MySQL / MariaDB carry neither -- the OAUTH capability is not
    advertised on those backends and the deletion worker should never
    reach this code path there."""
    if dialect in {"oracle", "db2"}:
        return "owner_id"
    return "user_id"


async def _hard_delete_identity_rows(
    ops: _Ops, user_id: str, namespace: str | None
) -> dict[str, int]:
    """Revoke / delete every subject-owned credential and identity row.

    GDPR erasure must not stop at the memory graph: email, raw OAuth
    claims, IP addresses, user-agent strings, and active API keys belong
    to the subject and must be removed before the request is marked
    complete. The api_keys row is revoked AND deleted (the revoke protects
    any in-flight auth check that has already loaded the row from
    accepting the credential after the DELETE commits).
    """
    counts: dict[str, int] = {}
    user_col = _identity_user_column(ops.dialect)
    # Revoke api_keys first so any concurrent auth check that has already
    # read the row sees ``revoked = TRUE`` before the DELETE runs.
    revoke_sql = f"UPDATE api_keys SET revoked = TRUE, last_used = NULL WHERE {user_col} = ?"
    if ops.dialect in {"oracle", "db2"} and namespace is not None:
        revoke_sql += " AND namespace = ?"
    params: tuple[Any, ...] = (user_id,)
    if ops.dialect in {"oracle", "db2"} and namespace is not None:
        params = params + (namespace,)
    counts["api_keys_revoked"] = await ops.execute(revoke_sql, *params)
    for table in _IDENTITY_TABLES:
        sql = f"DELETE FROM {table} WHERE {user_col} = ?"
        params = (user_id,)
        if ops.dialect in {"oracle", "db2"} and namespace is not None:
            sql += " AND namespace = ?"
            params = params + (namespace,)
        counts[table] = await ops.execute(sql, *params)
    # The user row itself is only removed on an all-namespace deletion.
    if namespace is None:
        counts["users"] = await ops.execute(
            "DELETE FROM users WHERE id = ?",
            user_id,
        )
    return counts


async def _scope_live_counts_identity(
    ops: _Ops, user_id: str, namespace: str | None
) -> dict[str, int]:
    """Count identity-table rows that would still leak subject data if
    we marked the request ``hard_deleted`` now. Must return zero on every
    entry before the request can advance to ``hard_deleted``.
    """
    counts: dict[str, int] = {}
    user_col = _identity_user_column(ops.dialect)
    for table in _IDENTITY_TABLES:
        sql = f"SELECT COUNT(*) FROM {table} WHERE {user_col} = ?"
        params: tuple[Any, ...] = (user_id,)
        if ops.dialect in {"oracle", "db2"} and namespace is not None:
            sql += " AND namespace = ?"
            params = params + (namespace,)
        counts[table] = int(await ops.scalar(sql, *params) or 0)
    # The user row only counts when the request is an all-namespace one.
    if namespace is None:
        counts["users"] = int(
            await ops.scalar("SELECT COUNT(*) FROM users WHERE id = ?", user_id) or 0
        )
    return counts


def _scope(column: str) -> str:
    """``owner_col = ? AND (? IS NULL OR namespace = ?)`` -- the canonical
    ``WHERE`` shape used across the deletion worker's owner/namespace
    scope queries."""
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


async def restore_soft_deleted_target(
    tx: Any,
    *,
    user_id: str,
    namespace: str | None,
    soft_deleted_at: Any,
) -> dict[str, int]:
    """Restore only rows changed by the matching soft-delete sweep."""
    ops = _Ops(tx, transaction_dialect(tx))
    counts: dict[str, int] = {}
    for table, owner_col in _OWNER_TABLES:
        counts[table] = await ops.execute(
            f"UPDATE {table} SET deleted_at = NULL WHERE {_scope(owner_col)} AND deleted_at = ?",
            user_id,
            namespace,
            namespace,
            soft_deleted_at,
        )
    for table, fk, parent, parent_id, owner_col in _RELATED_TABLES:
        counts[table] = await ops.execute(
            f"UPDATE {table} SET deleted_at = NULL WHERE {fk} IN "
            f"(SELECT {parent_id} FROM {parent} WHERE {_scope(owner_col)}) AND deleted_at = ?",
            user_id,
            namespace,
            namespace,
            soft_deleted_at,
        )
    return counts


async def _hard_delete_scope(ops: _Ops, request: dict[str, Any]) -> dict[str, int]:
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
            request.get("request_kind") or "tombstone_collected", request.get("notes"),
            json.dumps(request.get("source") or ["deletion_request_worker", str(request["id"])]),
        )
        columns = (
            "memory_id, content_hash, owner_id, namespace, requested_by, requested_at, "
            "request_kind, reason, source"
        )
        if ops.dialect in {"oracle", "db2"}:
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
    # Subject-owned identity / credential rows. These don't carry
    # ``deleted_at`` so they're handled separately. They run last so any
    # in-flight auth check that already loaded an api_keys row sees
    # ``revoked = TRUE`` before the credential is deleted.
    identity_counts = await _hard_delete_identity_rows(ops, user_id, namespace)
    counts.update(identity_counts)
    return counts


async def hard_delete_target(tx: Any, request: dict[str, Any]) -> dict[str, int]:
    """Hard-delete one already-soft-deleted scope in the caller's transaction."""
    return await _hard_delete_scope(_Ops(tx, transaction_dialect(tx)), request)


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
        # GDPR fence (resweep+verify): the 30-day grace window lets
        # writes land on the scope after the soft-delete phase. The
        # hard-delete below only removes rows already carrying
        # ``deleted_at IS NOT NULL`` -- so a row inserted during grace
        # would otherwise be skipped while the request was still marked
        # complete. Re-run the soft-delete sweep and verify there are no
        # live rows remaining before allowing the irreversible delete.
        await _resweep_and_verify_scope(ops, user_id, namespace)
        counts = await _hard_delete_scope(ops, request)
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


async def _resweep_and_verify_scope(
    ops: _Ops, user_id: str, namespace: str | None
) -> None:
    """Re-run the soft-delete sweep on the scope and refuse to advance
    the request if any live rows remain.

    The backend-neutral soft-delete pass uses
    ``UPDATE ... SET deleted_at = ? WHERE owner_col = ? AND deleted_at IS NULL``,
    which catches every memory-graph / session / kg / journal / entity /
    state row that landed after the original sweep. Identity-table rows
    (api_keys / oauth_* / user_groups) don't carry ``deleted_at`` so the
    active-deletion fence at write time is what keeps them off the
    scope; this resweep only needs to handle the soft-deletable tables.
    Raises ``RuntimeError`` when the scope still has live rows after
    ``DEFAULT_VERIFY_ATTEMPTS`` retries -- the caller must leave the
    request in its current state and try again next tick.
    """
    from mnemos.workers.deletion_request_worker import (
        DEFAULT_VERIFY_ATTEMPTS,
        count_live_target_rows,
        soft_delete_target,
        _has_live_rows,
    )
    for _ in range(max(1, DEFAULT_VERIFY_ATTEMPTS)):
        await soft_delete_target(ops.conn, user_id, namespace, invalidate_cache=False)
        remaining = await count_live_target_rows(ops.conn, user_id, namespace)
        if not _has_live_rows(remaining):
            return
    raise RuntimeError(
        f"refusing to hard-delete scope user_id={user_id} namespace={namespace}: "
        f"live rows remain after resweep+verify: {remaining}"
    )


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


async def active_deletion_for_scope(
    backend: Any,
    *,
    target_user_id: str,
    target_namespace: str | None,
) -> dict[str, Any] | None:
    """Return the active deletion row for a (user, namespace) scope, if any.

    An "active" deletion is one whose status has progressed past
    ``confirmed`` (sweep_verifying / soft_deleted) but has not yet been
    hard_deleted. Memory / session / kg / journal write paths call this
    before inserting a new row so a live write cannot race past the
    30-day grace window: without this fence, a memory created during
    grace would not be soft-deleted by the next sweep, and the
    subsequent hard-delete would skip it (it only removes rows that
    already carry ``deleted_at IS NOT NULL``), so the request would be
    marked ``hard_deleted`` while leaving that row behind forever.

    Returns ``None`` when no active deletion targets the scope.
    """
    sql = (
        "SELECT id, status, soft_deleted_at, restore_by "
        "FROM deletion_requests "
        "WHERE target_user_id = ? "
        "  AND (? IS NULL OR (target_namespace IS NULL OR target_namespace = ?)) "
        "  AND status IN ('sweep_verifying', 'soft_deleted') "
        "ORDER BY COALESCE(soft_deleted_at, confirmed_at) DESC "
        "LIMIT 1"
    )
    dialect = backend_dialect(backend)
    async with backend.transactional() as tx:
        ops = _Ops(tx, dialect)
        return await ops.fetchone(
            sql,
            target_user_id,
            target_namespace,
            target_namespace,
        )
