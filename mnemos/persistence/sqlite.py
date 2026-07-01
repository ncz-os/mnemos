"""SQLite persistence backend for the MNEMOS persistence interface.

Requires SQLite 3.35.0 or newer for UPDATE ... RETURNING support.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised when the sqlite extra is installed.
    import aiosqlite
except ImportError:  # pragma: no cover - local CI can run without optional extra.
    aiosqlite = None

from mnemos.core.auth_context import UserContext
from mnemos.core.config import embed_http_model_override, hot_rs_enabled
from mnemos.core.native_accel import load_hot_rs
from mnemos.persistence.base import (
    AuditChainRepository,
    BranchRepository,
    CompressionQueueRepository,
    CompressionRepository,
    CompressionStatsRow,
    ConsultationAuditRepository,
    ConsultationsRepository,
    DuplicateMemoryError,
    FederationRepository,
    FULL_STORAGE_CAPABILITY_DETAILS,
    KGRepository,
    MemoryRepository,
    MemoryStatsRow,
    OAuthRepository,
    SessionsRepository,
    StateRepository,
    Transaction,
    UsageLedgerRecord,
    UsageLedgerResult,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import MEMORY_COLS as _MEMORY_COLS, Row
from mnemos.core.visibility import ACL_READ_BIT, acl_principals
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core import eligibility as _eligibility
from mnemos.core import webhook_constants

logger = logging.getLogger(__name__)

MIN_SQLITE_VERSION = (3, 35, 0)
_RECENCY_E_FOLD_SECONDS = 7 * 24 * 60 * 60


SQLITE_MIGRATION_FILES = [
    "migrations.sql",
    "migrations_v1_multiuser.sql",
    "migrations_v2_versioning.sql",
    "migrations_v2_sessions.sql",
    "migrations_model_registry.sql",
    "migrations_v3_dag.sql",
    "migrations_v3_graeae_unified.sql",
    "migrations_v3_webhooks.sql",
    "migrations_v3_oauth.sql",
    "migrations_v3_federation.sql",
    "migrations_v3_ownership.sql",
    "migrations_v3_1_compression.sql",
    "migrations_v3_1_versioning_fix.sql",
    "migrations_v3_1_2_kg_tenancy.sql",
    "migrations_v3_1_2_audit_log_columns.sql",
    "migrations_v3_2_user_namespace.sql",
    "migrations_v3_2_entities_namespace.sql",
    "migrations_v3_2_2_version_snapshot_new_values.sql",
    "migrations_v3_3_morpheus.sql",
    "migrations_v3_3_morpheus_namespace.sql",
    "migrations_v3_3_recall_tracking.sql",
    "migrations_charon_trigger_guard.sql",
    "migrations_v3_4_federation_compat.sql",
    "migrations_v3_5_trigger_same_memory_parent.sql",
    "migrations_v3_5_rls_group_select_unix_bits.sql",
    "migrations_v3_5_webhook_retry_terminal_state.sql",
    "migrations_v3_5_webhook_attempt_lease.sql",
    "migrations_v3_5_webhook_writer_revision.sql",
    "migrations_v3_5_webhook_status_updated_at.sql",
    "migrations_v3_5_webhook_superseded_marker.sql",
    "migrations_v3_5_webhook_attempt_unique.sql",
    "migrations_v3_5_webhook_succeeded_unique.sql",
    "migrations_v3_5_webhook_succeeded_terminal_trigger.sql",
    "migrations_v3_5_entities_namespace_unique.sql",
    "migrations_v3_5_state_journal_namespace.sql",
    "migrations_v3_5_session_compression_ratio_drop.sql",
    "migrations_v3_5_session_compression_legacy_drop.sql",
    "migrations_v3_5_sessions_consultations_namespace.sql",
    "migrations_v4_2_compression_candidates_reject_reason.sql",
    "migrations_v4_2_morpheus_consolidate_sqlite.sql",
    "migrations_v4_2_morpheus_extract_sqlite.sql",
    "migrations_v4_2_persephone.sql",
    "migrations_v4_2_pantheon_routing_audit_sqlite.sql",
    "migrations_v5_0_consolidated_at_sqlite.sql",
    "migrations_v5_0_morpheus_extract_run_memories_sqlite.sql",
    "migrations_v5_0_2_artemis_dedup_sqlite.sql",
    "migrations_v5_0_3_timestamp_tz_upgrade_sqlite.sql",
    "migrations_v5_1_0_deletion_log_sqlite.sql",
    "migrations_v5_2_0_nats_outbox_idempotency_sqlite.sql",
    "migrations_v5_3_4_mcp_audit_log_sqlite.sql",
    "migrations_v6_2_audit_chain_sqlite.sql",
    "migrations_v6_2_category_decay_sqlite.sql",
    "0038_oauth_sessions_consultations.sql",
    "0039_subscription_plan_current_limits.sql",
    "0043_memory_acl.sql",
]


def _dict_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def _is_root(user: UserContext) -> bool:
    return user.role == "root"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json_text(value: Any, *, default: Any = None) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else default)


def _json_array_text(value: Sequence[Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value))


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _json_value(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _uuid_to_blob(value: str | bytes | uuid.UUID | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) != 16:
            raise ValueError("UUID BLOB values must be exactly 16 bytes")
        return value
    if isinstance(value, uuid.UUID):
        return value.bytes
    return uuid.UUID(str(value)).bytes


def _blob_to_uuid(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(uuid.UUID(bytes=bytes(value)))


def _token_blob(value: str | bytes, *, length: int = 32) -> bytes:
    if isinstance(value, bytes):
        if len(value) != length:
            raise ValueError(f"token BLOB values must be exactly {length} bytes")
        return value
    text = str(value)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raw = text.encode("utf-8")
    if len(raw) != length:
        raise ValueError(f"token BLOB values must be exactly {length} bytes")
    return raw


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _in_clause(column: str, values: Sequence[Any], params: list[Any]) -> str:
    if not values:
        return "0"
    params.extend(values)
    return f"{column} IN ({_placeholders(values)})"


def _read_visibility_clause(
    user: UserContext,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    p = f"{table_alias}." if table_alias else ""
    params.append(user.user_id)
    group_ids = list(user.group_ids)
    if group_ids:
        group_clause = f"{p}group_id IN ({_placeholders(group_ids)})"
        params.extend(group_ids)
    else:
        group_clause = "0"
    acl_clause = _acl_exists_clause(acl_principals(user.user_id, group_ids), params, table_alias=table_alias)
    return (
        "("
        f"{p}owner_id = ?"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        f"{acl_clause}"
        ")"
    )


def _acl_exists_clause(
    principals: Sequence[str],
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    """SQLite per-principal ACL read disjunct (qmark style).

    Returns a leading-``" OR EXISTS (…)"`` fragment when ``principals``
    is non-empty (appending the principal binds to ``params`` in
    placeholder order), or ``""`` when there is nothing to match — an
    unauthenticated caller with no groups can never satisfy an ACL.
    """
    if not principals:
        return ""
    p = f"{table_alias}." if table_alias else ""
    params.extend(principals)
    return (
        f" OR EXISTS (SELECT 1 FROM memory_acl macl "
        f"WHERE macl.memory_id = {p}id "
        f"AND macl.principal IN ({_placeholders(principals)}) "
        f"AND (macl.perm & {ACL_READ_BIT}) <> 0)"
    )


def _version_visibility_clause(
    user: UserContext,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    p = f"{table_alias}." if table_alias else ""
    params.append(user.user_id)
    return f"({p}owner_id = ? OR ({p}permission_mode % 10) >= 4)"


def _sqlite_memory_cols(table_alias: str = "") -> str:
    """Return ``_MEMORY_COLS``-equivalent SELECT list for SQLite.

    The SQLite ``memories`` table lacks the Postgres-only
    ``compressed_content`` column, so emit ``NULL AS compressed_content``
    in its place. Timestamp columns are normalized to ISO-shaped TEXT
    so handler serialization sees the same wire shape as Postgres
    ``datetime.isoformat()`` output. Other ``_MEMORY_COLS`` columns are
    present on both backends and pass through with the optional
    ``table_alias.`` prefix so the result is safe to JOIN.
    """
    p = f"{table_alias}." if table_alias else ""
    out: list[str] = []
    for raw in _MEMORY_COLS.split(","):
        col = raw.strip()
        if col == "compressed_content":
            out.append("NULL AS compressed_content")
        elif col in {"created", "updated"}:
            out.append(f"replace(datetime({p}{col}), ' ', 'T') AS {col}")
        else:
            out.append(f"{p}{col}")
    return ", ".join(out)


def _render_sqlite_visibility(
    visibility: VisibilityFilter,
    params: list[Any],
    *,
    table_alias: str = "",
) -> str:
    """SQLite analog of ``mnemos.persistence.postgres._render_postgres_visibility``.

    Appends parameters to ``params`` (qmark style — SQLite has no ``$N``)
    and returns the WHERE fragment. Returns an empty string for
    ``ROOT_BYPASS`` with no namespace pin so callers can omit the WHERE
    entirely.

    Mirrors the existing ``_read_visibility_clause`` shape (the
    v1_multiuser RLS read predicate, expanded inline because SQLite has
    no RLS), but takes a backend-neutral ``VisibilityFilter`` so the
    repository surface stays dialect-agnostic.
    """
    p = f"{table_alias}." if table_alias else ""

    # Secret vault (release-blocking 2026-06-13): exclude_namespaces is
    # subtracted for EVERY scope, incl. ROOT_BYPASS with namespace=None —
    # mirrors the Oracle _render_visibility contract. NULL namespace is
    # never a secret (vault rows always carry a non-NULL "vault"), so it is
    # preserved. Without this a root default search on SQLite returned the
    # whole vault (the same bug already fixed in the Oracle backend).
    def _excl_clause() -> str:
        excl = tuple(visibility.exclude_namespaces or ())
        if not excl:
            return ""
        ph = _placeholders(excl)
        params.extend(excl)
        return f"({p}namespace IS NULL OR {p}namespace NOT IN ({ph}))"

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return _excl_clause()
        params.append(visibility.namespace)
        base = f"{p}namespace = ?"
        excl = _excl_clause()
        return f"{base} AND {excl}" if excl else base

    if visibility.namespace is None:
        return "1=0"

    if visibility.scope == VisibilityScope.OWN_ONLY:
        # Mutation path: strict owner_id + namespace, with the same
        # namespace subtraction applied to every visibility scope.
        clauses: list[str] = [f"{p}owner_id = ?", f"{p}namespace = ?"]
        params.append(visibility.user_id)
        params.append(visibility.namespace)
        excl = _excl_clause()
        if excl:
            clauses.append(excl)
        return " AND ".join(clauses)

    # READABLE: full v1_multiuser predicate (own / federation / world /
    # group), namespace pin appended after.
    user_id = visibility.user_id or ""
    group_ids = list(visibility.group_ids)
    params.append(user_id)
    if group_ids:
        group_clause = f"{p}group_id IN ({_placeholders(group_ids)})"
        params.extend(group_ids)
    else:
        group_clause = "0"
    acl_clause = _acl_exists_clause(acl_principals(user_id, group_ids), params, table_alias=table_alias)
    clause = (
        "("
        f"{p}owner_id = ?"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        f"{acl_clause}"
        ")"
    )
    clause = f"{clause} AND {p}namespace = ?"
    params.append(visibility.namespace)
    excl = _excl_clause()
    if excl:
        clause = f"{clause} AND {excl}"
    return clause


def _parse_embedding(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part for part in raw.strip("[]").split(",") if part]
    if isinstance(raw, Iterable):
        out: list[float] = []
        for item in raw:
            try:
                out.append(float(item))
            except (TypeError, ValueError):
                return []
        return out
    return []


# Optional Rust hot-path accelerator. Loaded lazily so the absence of
# the wheel on a given build host does NOT break the import — the
# Python implementation below stays the source of truth.
# Opt-in via env var MNEMOS_HOT_RS_ENABLED=1; default off until soak.
_HOT_RS = None
_HOT_RS_ENABLED = hot_rs_enabled()
if _HOT_RS_ENABLED:
    _HOT_RS = load_hot_rs(logger, "SQLite cosine UDF")


def _cosine_similarity(left: Any, right: Any) -> float:
    if _HOT_RS is not None:
        # Rust path: ~12× faster on 384-dim batches per
        # /private/tmp/mnemos-hot-rs/bench_vs_python.py. The Rust
        # parse_embedding mirrors the Python semantics 1:1 (None →
        # [], list → float-extract, str → JSON-array hand-parse,
        # length mismatch → 0.0, zero norm → 0.0).
        try:
            a = _HOT_RS.parse_embedding(left)
            b = _HOT_RS.parse_embedding(right)
        except Exception:
            # Defensive: an unexpected input shape (e.g., bytes) would
            # raise ValueError out of pyo3. Fall back to Python.
            a = _parse_embedding(left)
            b = _parse_embedding(right)
        return _HOT_RS.cosine(a, b)
    a = _parse_embedding(left)
    b = _parse_embedding(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _content_hash_for_sqlite(content: Any) -> str:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call(method: Any, *args: Any) -> Any:
    return await _maybe_await(method(*args))


async def _execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    normalized = tuple(_sqlite_value(value) for value in params)
    return await _maybe_await(conn.execute(sql, normalized))


async def _execute_count(conn: Any, sql: str, params: Sequence[Any] = ()) -> int:
    cursor = await _execute(conn, sql, params)
    count = int(getattr(cursor, "rowcount", 0) or 0)
    close = getattr(cursor, "close", None)
    if close is not None:
        await _maybe_await(close())
    return count


# #184: removed `_executemany` — dead. No call sites; SQLite
# multi-row writes go through individual `await conn.execute(...)`
# calls in the migration runner and test helpers. The sibling
# `_executescript` IS still used (migration script application).


async def _executescript(conn: Any, sql: str) -> Any:
    return await _maybe_await(conn.executescript(sql))


async def _fetch_all(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Row]:
    cursor = await _execute(conn, sql, params)
    try:
        rows = await _maybe_await(cursor.fetchall())
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            await _maybe_await(close())
    return list(rows)


async def _fetch_one(conn: Any, sql: str, params: Sequence[Any] = ()) -> Row | None:
    cursor = await _execute(conn, sql, params)
    try:
        row = await _maybe_await(cursor.fetchone())
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            await _maybe_await(close())
    return row


async def _fetch_val(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    row = await _fetch_one(conn, sql, params)
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


async def _commit(conn: Any) -> None:
    await _maybe_await(conn.commit())


async def _rollback(conn: Any) -> None:
    await _maybe_await(conn.rollback())


class SqliteTransaction:
    """Transaction wrapper that keeps SQLite connection details private."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._closed = False

    @property
    def conn(self) -> Any:
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    async def commit(self) -> None:
        if self._closed:
            return
        await _commit(self._conn)
        self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        await _rollback(self._conn)
        self._closed = True


def _sqlite_tx(tx: Transaction) -> SqliteTransaction:
    if not isinstance(tx, SqliteTransaction):
        raise TypeError("SQLite repositories require a SqliteTransaction")
    return tx


class _SqliteRepository:
    @staticmethod
    def _conn(tx: Transaction) -> Any:
        return _sqlite_tx(tx).conn


class SqliteMemoryRepository(_SqliteRepository, MemoryRepository):
    # semantic_search emits ``similarity`` = mnemos_cosine_similarity (already
    # cosine similarity in [0,1], higher = better).
    SEMANTIC_SCORE_COLUMN = "similarity"
    SEMANTIC_SCORE_METRIC = "cosine_similarity"

    # Set by SqliteBackend on construction so search/upsert paths can
    # enforce the configured embedding dim end-to-end. None disables the
    # check (e.g. tests that bypass the backend).
    _expected_embedding_dim: int | None = None

    def _require_dim(self, embedding: Sequence[float], op: str) -> None:
        """Fail loudly if the embedding length doesn't match the configured dim.

        Without this guard, mnemos_cosine_similarity would return 0.0 on every
        length mismatch, silently degrading search to "rank by recency" and
        letting wrong-dim writes poison the table until the next restart-time
        guard fires. We want loud failure on every call.
        """
        expected = self._expected_embedding_dim
        if expected is None:
            return
        actual = len(embedding)
        if actual != expected:
            raise ValueError(
                f"SQLite embedding dim mismatch on {op}: got {actual}-D vector "
                f"but the configured MNEMOS_EMBEDDING_DIM is {expected}. The "
                f"embedding endpoint may have been switched to a different "
                f"model. Verify `INFERENCE_EMBED_HOST` / model selection and "
                f"either restart with the matching MNEMOS_EMBEDDING_DIM or "
                f"swap the embedding endpoint back to the model the DB was "
                f"sized for."
            )

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        conn = self._conn(tx)
        if _is_root(user):
            row = await _fetch_one(conn, "SELECT 1 FROM memory_versions WHERE memory_id = ? LIMIT 1", (memory_id,))
        else:
            params: list[Any] = [memory_id]
            vis_clause = _read_visibility_clause(user, params)
            params.append(user.namespace)
            row = await _fetch_one(
                conn,
                f"SELECT 1 FROM memories WHERE id = ? AND {vis_clause} AND namespace = ? LIMIT 1",
                params,
            )
        if not row:
            raise PermissionError(f"Memory {memory_id} not found")

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        rows = await _fetch_all(
            self._conn(tx),
            """
            WITH RECURSIVE commit_walk AS (
                SELECT
                    mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                    mv.version_num, mv.branch, mv.content, mv.category,
                    mv.change_type, mv.snapshot_at, mv.snapshot_by,
                    mv.owner_id, mv.namespace, mv.permission_mode,
                    1 AS depth
                FROM memory_versions mv
                INNER JOIN memory_branches mb ON (
                    mb.memory_id = mv.memory_id AND
                    mb.name = ? AND
                    mb.head_version_id = mv.id
                )
                WHERE mv.memory_id = ?
                UNION ALL
                SELECT
                    mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                    mv.version_num, mv.branch, mv.content, mv.category,
                    mv.change_type, mv.snapshot_at, mv.snapshot_by,
                    mv.owner_id, mv.namespace, mv.permission_mode,
                    cw.depth + 1
                FROM memory_versions mv
                INNER JOIN commit_walk cw
                    ON mv.id = cw.parent_version_id
                   AND mv.memory_id = cw.memory_id
                WHERE cw.depth < ?
            )
            SELECT
                commit_hash, version_num, branch, category, change_type,
                snapshot_at, snapshot_by, owner_id, namespace, permission_mode
            FROM commit_walk
            ORDER BY depth ASC
            LIMIT ?
            """,
            (branch, memory_id, limit, limit),
        )
        if _is_root(user):
            return rows
        return [
            row
            for row in rows
            if row["namespace"] == user.namespace
            and (row["owner_id"] == user.user_id or (row["permission_mode"] % 10) >= 4)
        ]

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        conn = self._conn(tx)
        if _is_root(user):
            sql = "SELECT content, version_num FROM memory_versions WHERE memory_id = ? AND commit_hash = ?"
            return (
                await _fetch_one(conn, sql, (memory_id, commit_a)),
                await _fetch_one(conn, sql, (memory_id, commit_b)),
            )
        params_a: list[Any] = [memory_id, commit_a]
        vis_clause = _version_visibility_clause(user, params_a)
        params_a.append(user.namespace)
        sql = (
            "SELECT content, version_num FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?"
        )
        params_b: list[Any] = [memory_id, commit_b]
        vis_clause_b = _version_visibility_clause(user, params_b)
        params_b.append(user.namespace)
        sql_b = (
            "SELECT content, version_num FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause_b} AND namespace = ?"
        )
        return (await _fetch_one(conn, sql, params_a), await _fetch_one(conn, sql_b, params_b))

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        conn = self._conn(tx)
        select = (
            "SELECT commit_hash, version_num, branch, category, subcategory, "
            "content, change_type, snapshot_at, snapshot_by "
            "FROM memory_versions "
        )
        if _is_root(user):
            return await _fetch_one(conn, select + "WHERE memory_id = ? AND commit_hash = ?", (memory_id, commit_hash))
        params: list[Any] = [memory_id, commit_hash]
        vis_clause = _version_visibility_clause(user, params)
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            select + f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?",
            params,
        )

    async def fetch_memory_export(
        self,
        tx: Transaction,
        *,
        effective_owner: str | None,
        effective_ns: str | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        # Exclude soft-deleted rows (matches oracle/postgres/db2/mysql and the
        # normal read paths) so export never resurrects tombstoned memories.
        conditions: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if effective_owner:
            conditions.append("owner_id = ?")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = ?")
            params.append(effective_ns)
        if category:
            conditions.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        return await _fetch_all(
            self._conn(tx),
            # Provenance columns for MPF v0.2 emission. SQLite uses
            # `source_memory_ids` and `provenance` (JSON-text) where
            # postgres uses `source_memories` (text[]) and `provenance`
            # (text). The serializer reads either key with fallback —
            # we still alias `provenance` to `prov_kind` so it doesn't
            # collide with the v0.2 record-level `provenance` field
            # name in serializer logic.
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, group_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            "metadata, verbatim_content, archived_at, consolidated_into, embedding, "
            "provenance AS prov_kind, morpheus_run_id, "
            "source_memory_ids, federation_source "
            f"FROM memories {where} ORDER BY created ASC, id ASC LIMIT ? OFFSET ?",
            params,
        )

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        if not referenced_ids:
            return []
        params: list[Any] = []
        conditions = [_in_clause("id", list(referenced_ids), params)]
        if scope_owner is not None:
            conditions.append("owner_id = ?")
            params.append(scope_owner)
        if scope_namespace is not None:
            conditions.append("namespace = ?")
            params.append(scope_namespace)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT id, owner_id, namespace FROM memories WHERE {' AND '.join(conditions)}",
            params,
        )

    async def insert_memory(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        quality_rating: int,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        verbatim_content: str | None,
        embedding: Sequence[float] | None = None,
        created: Any,
        updated: Any,
    ) -> str:
        # Format embedding as JSON text for SQLite; NULL when absent.
        # Inlining it in the INSERT keeps the vector co-transactional
        # with the row so semantic_search sees it immediately.
        embedding_json: str | None = None
        if embedding:
            self._require_dim(embedding, "insert_memory")
            embedding_json = json.dumps([float(value) for value in embedding])
        inserted = await _fetch_one(
            self._conn(tx),
            """
            INSERT INTO memories (
                id, content, category, subcategory, metadata,
                content_hash, quality_rating, verbatim_content, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                embedding, created, updated
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP)
            )
            ON CONFLICT(id) DO NOTHING
            RETURNING id
            """,
            (
                memory_id,
                content,
                category,
                subcategory,
                metadata_json,
                _content_hash_for_sqlite(content),
                quality_rating,
                verbatim_content,
                owner_id,
                namespace,
                permission_mode,
                source_model,
                source_provider,
                source_session,
                source_agent,
                embedding_json,
                created,
                updated,
            ),
        )
        if inserted is None:
            raise DuplicateMemoryError(f"memory id already exists: {memory_id}")
        return "INSERT 0 1"

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT content, category, subcategory, metadata, quality_rating, owner_id, "
            "namespace, permission_mode, source_model, source_provider, source_session, "
            "source_agent, created, updated FROM memories WHERE id = ?",
            (memory_id,),
        )

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        await _execute(self._conn(tx), "CREATE TEMP TABLE IF NOT EXISTS mnemos_tx_flags (key TEXT PRIMARY KEY)")
        await _execute(
            self._conn(tx), "INSERT OR IGNORE INTO mnemos_tx_flags(key) VALUES ('suppress_version_snapshot')"
        )

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("memory_id", list(memory_ids), params)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT DISTINCT memory_id FROM memory_versions WHERE {condition}",
            params,
        )

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("m.id", list(memory_ids), params)
        return await _fetch_all(
            self._conn(tx),
            "SELECT m.id, m.content AS memory_content, mv.content AS head_content "
            "FROM memories m "
            "LEFT JOIN memory_branches b ON b.memory_id = m.id AND b.name = 'main' "
            "LEFT JOIN memory_versions mv ON mv.id = b.head_version_id "
            f"WHERE {condition}",
            params,
        )

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        conn = self._conn(tx)
        categories = ("solutions", "patterns", "decisions", "infrastructure")
        category_placeholders = _placeholders(categories)
        like_q = f"%{query}%"
        params: list[Any] = []
        if getattr(user, "role", None) == "root":
            params.extend([like_q, *categories, limit])
            rows = await _fetch_all(
                conn,
                "SELECT m.id, COALESCE(v.compressed_content, m.content) AS content "
                "FROM memories m "
                "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
                f"WHERE lower(m.content) LIKE lower(?) OR m.category IN ({category_placeholders}) "
                "ORDER BY m.updated DESC LIMIT ?",
                params,
            )
        else:
            vis_clause = _read_visibility_clause(user, params, table_alias="m")
            params.extend([user.namespace, like_q, *categories, limit])
            rows = await _fetch_all(
                conn,
                "SELECT m.id, COALESCE(v.compressed_content, m.content) AS content "
                "FROM memories m "
                "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
                f"WHERE {vis_clause} AND m.namespace = ? "
                f"AND (lower(m.content) LIKE lower(?) OR m.category IN ({category_placeholders})) "
                "ORDER BY m.updated DESC LIMIT ?",
                params,
            )
        return [{"id": row["id"], "content": row["content"]} for row in rows]

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        self._require_dim(embedding, "upsert_memory_embedding")
        embedding_json = json.dumps([float(value) for value in embedding])
        conn = self._conn(tx)
        await _execute(conn, "UPDATE memories SET embedding = ? WHERE id = ?", (embedding_json, memory_id))
        # On conflict, bump updated_at too — without it, an embedding
        # refresh (federation re-pull, re-embed worker, manual backfill)
        # leaves updated_at stale + the only signal that the row was
        # touched is the embedding column itself, which is hard to
        # diff. Surfaced 2026-05-24 during F-1 e2e verification.
        await _execute(
            conn,
            "INSERT INTO memory_embeddings(memory_id, embedding) VALUES (?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET "
            "  embedding = excluded.embedding, "
            "  updated_at = strftime('%s', 'now')",
            (memory_id, embedding_json),
        )

    async def semantic_search(
        self,
        tx: Transaction,
        *,
        embedding: Sequence[float],
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
        boost_recency: bool = False,
        recency_weight: float = 0.15,
        exclude_superseded: bool = False,
    ) -> list[Row]:
        self._require_dim(embedding, "semantic_search")

        def _recency_date(row: Row) -> date:
            def _coerce_date(value: Any) -> date | None:
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
                if isinstance(value, str):
                    try:
                        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
                    except ValueError:
                        return None
                return None

            return (
                _coerce_date(row.get("last_recalled_at"))
                or _coerce_date(row.get("updated"))
                or _coerce_date(row.get("created"))
                or date.min
            )

        embedding_json = json.dumps([float(value) for value in embedding])
        conditions: list[str] = ["me.embedding IS NOT NULL"]
        if not include_archived:
            conditions.append("m.archived_at IS NULL")
        if exclude_superseded:
            conditions.append("m.consolidated_into IS NULL")
        params: list[Any] = [embedding_json]
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                conditions.append(f"m.{col} = ?")
                params.append(val)
        vis_clause = _render_sqlite_visibility(visibility, params, table_alias="m")
        if vis_clause:
            conditions.append(vis_clause)
        # Bounded approximation matching PostgresMemoryRepository: recency
        # reranks only within this native similarity candidate window.
        candidate_limit = max(limit, min(limit * 4, 200)) if boost_recency else limit
        params.append(candidate_limit)
        # SELECT ``_MEMORY_COLS`` (with the ``m.`` alias) so the row
        # shape matches what the handler's ``row_to_memory`` consumes —
        # parity with PostgresMemoryRepository.semantic_search.
        select_cols = _sqlite_memory_cols("m")
        rows = await _fetch_all(
            self._conn(tx),
            f"SELECT {select_cols}, "
            "replace(datetime(m.last_recalled_at), ' ', 'T') AS last_recalled_at, "
            "mnemos_cosine_similarity(me.embedding, ?) AS similarity "
            "FROM memory_embeddings me "
            "JOIN memories m ON m.id = me.memory_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY similarity DESC, m.updated DESC LIMIT ?",
            params,
        )
        if boost_recency and rows:
            today = datetime.now(timezone.utc).date()

            def _recency_score(row: Row) -> float:
                try:
                    similarity = float(row.get("similarity"))
                except (TypeError, ValueError):
                    return -math.inf
                if not math.isfinite(similarity):
                    return -math.inf
                recency_date = _recency_date(row)
                age_days = max(0, (today - recency_date).days)
                # Align with Postgres' exponential recency signal. This is
                # an ordering key only; the raw ``similarity`` column remains
                # untouched for route-level min_score/OOD gates.
                recency = math.exp(-age_days * 86400.0 / float(_RECENCY_E_FOLD_SECONDS))
                if row.get("superseded_by") or row.get("consolidated_into"):
                    recency = 0.0
                return similarity + recency_weight * recency

            rows.sort(
                key=lambda row: (
                    bool(row.get("superseded_by") or row.get("consolidated_into")),
                    -_recency_score(row),
                )
            )
            rows = rows[:limit]
        return rows

    async def fts_search(
        self,
        tx: Transaction,
        *,
        query: str,
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
        exclude_superseded: bool = False,
    ) -> list[Row]:
        conn = self._conn(tx)
        # FTS path: $1=query (MATCH), filter+visibility params in the
        # middle, $LAST=limit. Mirrors the legacy shape but with the
        # full _MEMORY_COLS row so the handler can pass results
        # straight to row_to_memory.
        params: list[Any] = [query]
        conditions: list[str] = []
        if not include_archived:
            conditions.append("m.archived_at IS NULL")
        if exclude_superseded:
            conditions.append("m.consolidated_into IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                conditions.append(f"m.{col} = ?")
                params.append(val)
        vis_clause = _render_sqlite_visibility(visibility, params, table_alias="m")
        if vis_clause:
            conditions.append(vis_clause)
        where_extra = f" AND {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        select_cols = _sqlite_memory_cols("m")

        async def _like_search() -> list[Row]:
            like_params: list[Any] = [f"%{query}%"]
            like_conditions: list[str] = ["lower(m.content) LIKE lower(?)"]
            if not include_archived:
                like_conditions.append("m.archived_at IS NULL")
            if exclude_superseded:
                like_conditions.append("m.consolidated_into IS NULL")
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    like_conditions.append(f"m.{col} = ?")
                    like_params.append(val)
            like_vis_clause = _render_sqlite_visibility(
                visibility,
                like_params,
                table_alias="m",
            )
            if like_vis_clause:
                like_conditions.append(like_vis_clause)
            like_params.append(limit)
            return await _fetch_all(
                conn,
                f"SELECT {select_cols} FROM memories m "
                f"WHERE {' AND '.join(like_conditions)} "
                "ORDER BY m.updated DESC LIMIT ?",
                like_params,
            )

        try:
            rows = await _fetch_all(
                conn,
                f"SELECT {select_cols}, bm25(memories_fts) AS rank "
                "FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.id "
                f"WHERE memories_fts MATCH ?{where_extra} "
                "ORDER BY rank ASC, m.updated DESC LIMIT ?",
                params,
            )
            if rows:
                return rows
            return await _like_search()
        except sqlite3.Error:
            # ILIKE-equivalent fallback when FTS5 isn't available or
            # the query is malformed for tsquery purposes. We also use
            # the same rescue when FTS returns no rows, which can happen
            # after legacy installs with a stale/missing FTS trigger.
            return await _like_search()

    # --- v4.1 handler-through-backend impls -----------------------------------

    async def list_memories(
        self,
        tx: Transaction,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
        exclude_superseded: bool = False,
    ) -> tuple[list[Row], int]:
        conn = self._conn(tx)
        where_parts: list[str] = []
        if not include_archived:
            where_parts.append("archived_at IS NULL")
        if exclude_superseded:
            where_parts.append("consolidated_into IS NULL")
        params: list[Any] = []
        if category is not None:
            where_parts.append("category = ?")
            params.append(category)
        if subcategory is not None:
            where_parts.append("subcategory = ?")
            params.append(subcategory)
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            where_parts.append(vis_clause)
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        select_sql = f"SELECT {_sqlite_memory_cols()} FROM memories{where_sql} ORDER BY created DESC LIMIT ? OFFSET ?"
        # COUNT(*) first (without limit/offset params), then paged
        # SELECT with the same predicate params plus limit/offset.
        count_sql = f"SELECT COUNT(*) FROM memories{where_sql}"
        total = await _fetch_val(conn, count_sql, params)
        rows = await _fetch_all(conn, select_sql, [*params, limit, offset])
        return rows, int(total or 0)

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = self._conn(tx)
        archived_clause = "" if include_archived else " AND archived_at IS NULL"
        if visibility.scope == VisibilityScope.ROOT_BYPASS and visibility.namespace is None:
            return await _fetch_one(
                conn,
                f"SELECT {_sqlite_memory_cols()} FROM memories WHERE id = ?{archived_clause}",
                (memory_id,),
            )
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        sql = f"SELECT {_sqlite_memory_cols()} FROM memories WHERE id = ?{archived_clause} AND {vis_clause}"
        return await _fetch_one(conn, sql, params)

    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        if not fields:
            return None
        conn = self._conn(tx)
        keys = [
            k
            for k in fields.keys()
            if k
            in {
                "content",
                "category",
                "subcategory",
                "metadata",
                "quality_rating",
                "compressed_content",
                "verbatim_content",
                "permission_mode",
                "source_model",
                "source_provider",
                "source_session",
                "source_agent",
                "group_id",
                "archived_at",
                "consolidated_into",
                "namespace",
                "updated",
            }
        ]
        if not keys:
            return await self.get_memory(tx, memory_id, visibility=visibility)
        set_clauses = [f"{col} = ?" for col in keys if col != "updated"]
        values: list[Any] = [fields[k] for k in keys if k != "updated"]
        if "content" in fields:
            set_clauses.append("content_hash = ?")
            values.append(_content_hash_for_sqlite(fields["content"]))
        if "updated" in keys and fields.get("updated") is not None:
            set_clauses.append("updated = ?")
            values.append(fields["updated"])
        else:
            set_clauses.append("updated = CURRENT_TIMESTAMP")
        set_sql = ", ".join(set_clauses)
        # WHERE id=? + visibility predicate. Authorization folded into
        # the same UPDATE/RETURNING — same TOCTOU-safe shape as the
        # Postgres impl.
        params: list[Any] = [*values, memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = f"UPDATE memories SET {set_sql} WHERE id = ? AND {vis_clause} RETURNING {_sqlite_memory_cols()}"
        else:
            sql = f"UPDATE memories SET {set_sql} WHERE id = ? RETURNING {_sqlite_memory_cols()}"
        return await _fetch_one(conn, sql, params)

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conditions = [
            "owner_id = ?",
            "deleted_at IS NULL",
            "archived_at IS NULL",
            "consolidated_into IS NULL",
            "content_hash = ?",
        ]
        params: list[Any] = [owner_id, content_hash]
        if not cross_namespace:
            conditions.insert(1, "namespace = ?")
            params.insert(1, namespace)
        return await _fetch_one(
            self._conn(tx),
            "SELECT id, last_recalled_at FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY created ASC, id ASC LIMIT 1",
            params,
        )

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = self._conn(tx)
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP "
                f"WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL AND {vis_clause} "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        else:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND deleted_at IS NULL AND archived_at IS NULL "
                f"RETURNING {_sqlite_memory_cols()}"
            )
        return await _fetch_one(conn, sql, params)

    async def backfill_missing_content_hashes(
        self,
        tx: Transaction,
        *,
        batch_size: int = 500,
        apply: bool = False,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        conn = self._conn(tx)
        if not apply:
            return int(await _fetch_val(conn, "SELECT COUNT(*) FROM memories WHERE content_hash IS NULL") or 0)
        return await _execute_count(
            conn,
            """
            UPDATE memories
               SET content_hash = mnemos_content_sha256(content),
                   updated = CURRENT_TIMESTAMP
             WHERE id IN (
                SELECT id
                  FROM memories
                 WHERE content_hash IS NULL
                 ORDER BY created ASC, id ASC
                 LIMIT ?
             )
               AND content_hash IS NULL
            """,
            [int(batch_size)],
        )

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        params: list[Any] = []
        namespace_clause = ""
        if namespace is not None:
            namespace_clause = "AND namespace = ?"
            params.append(namespace)
        rows = await _fetch_all(
            self._conn(tx),
            """
            SELECT
                owner_id,
                namespace,
                content_hash,
                COUNT(*) AS duplicate_count,
                GROUP_CONCAT(id, char(31)) AS memory_ids,
                substr(GROUP_CONCAT(id, char(31)), 1, instr(GROUP_CONCAT(id, char(31)) || char(31), char(31)) - 1)
                    AS keep_id,
                substr(GROUP_CONCAT(id, char(31)), 1, instr(GROUP_CONCAT(id, char(31)) || char(31), char(31)) - 1)
                    AS canonical_id
            FROM (
                SELECT id, owner_id, namespace, content_hash, created, quality_rating,
                       replace(replace(COALESCE(content, ''), char(13) || char(10), char(10)), char(13), char(10))
                           AS normalized_content
                FROM memories
                WHERE deleted_at IS NULL
                  AND archived_at IS NULL
                  AND consolidated_into IS NULL
                  AND content_hash IS NOT NULL
                  {namespace_clause}
                ORDER BY owner_id ASC, namespace ASC, content_hash ASC,
                         created DESC, quality_rating DESC, id DESC
            )
            GROUP BY owner_id, namespace, content_hash, normalized_content
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
            """.format(namespace_clause=namespace_clause),
            params,
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            raw_ids = values.get("memory_ids") or ""
            values["memory_ids"] = [part for part in str(raw_ids).split("\x1f") if part]
            values["duplicate_count"] = int(values.get("duplicate_count") or 0)
            normalized.append(values)
        return normalized

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        params: list[Any] = [canonical_id, *duplicate_ids, canonical_id, canonical_id]
        placeholders = _placeholders(duplicate_ids)
        return await _execute_count(
            self._conn(tx),
            f"""
            UPDATE memories
            SET consolidated_into = ?,
                consolidated_at = CURRENT_TIMESTAMP,
                deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP),
                updated = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
              AND id <> ?
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = ?
                    AND deleted_at IS NULL
                    AND archived_at IS NULL
                    AND consolidated_into IS NULL
              )
            """,
            params,
        )

    async def soft_delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        requested_by: str | None = None,
        requested_at: Any = None,
        request_kind: str = "admin_purge",
        reason: str | None = None,
        source: Sequence[str] | None = None,
    ) -> Row | None:
        _ = (requested_by, requested_at, request_kind, reason, source)
        conn = self._conn(tx)
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                "UPDATE memories SET deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP), updated = CURRENT_TIMESTAMP "
                f"WHERE id = ? AND deleted_at IS NULL AND {vis_clause} "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
        else:
            sql = (
                "UPDATE memories SET deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP), updated = CURRENT_TIMESTAMP "
                "WHERE id = ? AND deleted_at IS NULL RETURNING owner_id, namespace, id, content, category, subcategory"
            )
        return await _fetch_one(conn, sql, params)

    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        requested_by: str | None = None,
        requested_at: Any = None,
        request_kind: str = "admin_purge",
        reason: str | None = None,
        source: Sequence[str] | None = None,
    ) -> Row | None:
        conn = self._conn(tx)
        params: list[Any] = [memory_id]
        vis_clause = _render_sqlite_visibility(visibility, params)
        if vis_clause:
            sql = (
                "DELETE FROM memories "
                f"WHERE id = ? AND {vis_clause} "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
        else:
            sql = "DELETE FROM memories WHERE id = ? RETURNING owner_id, namespace, id, content, category, subcategory"
        return await _fetch_one(conn, sql, params)

    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        conn = self._conn(tx)
        total = await _fetch_val(conn, "SELECT COUNT(*) FROM memories")
        native = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NULL",
        )
        federated = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL",
        )
        peer_rows = await _fetch_all(
            conn,
            "SELECT federation_source, COUNT(*) AS cnt FROM memories "
            "WHERE federation_source IS NOT NULL "
            "GROUP BY federation_source ORDER BY cnt DESC",
        )
        cat_rows = await _fetch_all(
            conn,
            "SELECT category, COUNT(*) AS cnt FROM memories GROUP BY category",
        )
        sub_rows = await _fetch_all(
            conn,
            "SELECT category, subcategory, COUNT(*) AS cnt FROM memories "
            "WHERE subcategory IS NOT NULL "
            "GROUP BY category, subcategory ORDER BY cnt DESC",
        )
        avg_quality = await _fetch_val(
            conn,
            "SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL",
        )
        memories_by_subcategory: dict[str, dict[str, int]] = {}
        for r in sub_rows:
            memories_by_subcategory.setdefault(r["category"], {})[r["subcategory"]] = r["cnt"]
        return MemoryStatsRow(
            total_memories=int(total or 0),
            native_memories=int(native or 0),
            federated_memories=int(federated or 0),
            memories_by_peer={r["federation_source"]: r["cnt"] for r in peer_rows},
            memories_by_category={r["category"]: r["cnt"] for r in cat_rows},
            memories_by_subcategory=memories_by_subcategory,
            avg_quality_rating=float(avg_quality) if avg_quality is not None else None,
        )


class SqliteKGRepository(_SqliteRepository, KGRepository):
    async def fetch_kg_triples_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        include_unattached: bool,
        hard_limit: int,
    ) -> list[Row]:
        conditions: list[str] = []
        params: list[Any] = []
        if memory_ids:
            memory_condition = _in_clause("memory_id", list(memory_ids), params)
            if include_unattached:
                conditions.append(f"(memory_id IS NULL OR {memory_condition})")
            else:
                conditions.append(memory_condition)
        elif include_unattached:
            conditions.append("memory_id IS NULL")
        else:
            return []
        if effective_owner:
            conditions.append("owner_id = ?")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = ?")
            params.append(effective_ns)
        params.append(hard_limit + 1)
        return await _fetch_all(
            self._conn(tx),
            "SELECT id, subject, predicate, object, subject_type, object_type, "
            "valid_from, valid_until, memory_id, confidence, created, owner_id, namespace "
            f"FROM kg_triples WHERE {' AND '.join(conditions)} LIMIT ?",
            params,
        )

    async def insert_kg_triple(
        self,
        tx: Transaction,
        *,
        triple_id: str,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str | None,
        object_type: str | None,
        valid_from: Any,
        valid_until: Any,
        memory_id: str | None,
        confidence: float | None,
        created: Any,
        owner_id: str,
        namespace: str | None,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO kg_triples (
                id, subject, predicate, object,
                subject_type, object_type,
                valid_from, valid_until,
                memory_id, confidence, created,
                owner_id, namespace
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), ?,
                ?, COALESCE(?, 1.0),
                COALESCE(?, CURRENT_TIMESTAMP),
                ?, COALESCE(?, 'default')
            )
            """,
            (
                triple_id,
                subject,
                predicate,
                obj,
                subject_type,
                object_type,
                valid_from,
                valid_until,
                memory_id,
                confidence,
                created,
                owner_id,
                namespace,
            ),
        )
        return "INSERT 0 1"

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT subject, predicate, object, subject_type, object_type, memory_id, "
            "confidence, owner_id, namespace, valid_from, valid_until, created "
            "FROM kg_triples WHERE id = ?",
            (triple_id,),
        )

    async def search_triples(
        self,
        tx: Transaction,
        query: str,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
        limit: int = 20,
    ) -> list[Row]:
        params: list[Any] = [f"%{query}%", f"%{query}%", f"%{query}%"]
        conditions = ["(lower(subject) LIKE lower(?) OR lower(predicate) LIKE lower(?) OR lower(object) LIKE lower(?))"]
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = ?")
            params.append(namespace)
        params.append(limit)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT * FROM kg_triples WHERE {' AND '.join(conditions)} ORDER BY valid_from ASC, created ASC LIMIT ?",
            params,
        )


async def _fetch_sidecar(
    conn: Any,
    *,
    table: str,
    columns: str,
    memory_id_column: str,
    memory_ids: Sequence[str],
    effective_owner: str | None,
    effective_ns: str | None,
    bound_to_memories: bool,
    hard_limit: int,
    order_by: str | None = None,
) -> list[Row]:
    conditions: list[str] = []
    params: list[Any] = []
    if bound_to_memories:
        if not memory_ids:
            return []
        conditions.append(_in_clause(memory_id_column, list(memory_ids), params))
    if effective_owner:
        conditions.append("owner_id = ?")
        params.append(effective_owner)
    if effective_ns:
        conditions.append("namespace = ?")
        params.append(effective_ns)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = f"ORDER BY {order_by}" if order_by else ""
    params.append(hard_limit + 1)
    return await _fetch_all(conn, f"SELECT {columns} FROM {table} {where} {order} LIMIT ?", params)


class SqliteVersionRepository(_SqliteRepository, VersionRepository):
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await _fetch_sidecar(
            self._conn(tx),
            table="memory_versions",
            columns=(
                "id, memory_id, version_num, content, category, "
                "subcategory, metadata, verbatim_content, owner_id, "
                "namespace, permission_mode, source_model, source_provider, "
                "source_session, source_agent, snapshot_at, snapshot_by, "
                "change_type, commit_hash, parent_version_id, branch, merge_parents"
            ),
            memory_id_column="memory_id",
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            bound_to_memories=True,
            hard_limit=hard_limit,
            order_by="memory_id ASC, branch ASC, version_num ASC",
        )

    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        if not version_ids:
            return []
        params: list[Any] = []
        condition = _in_clause("id", list(version_ids), params)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT id, memory_id, owner_id, namespace FROM memory_versions WHERE {condition}",
            params,
        )

    async def insert_memory_version(
        self,
        tx: Transaction,
        *,
        version_id: str,
        memory_id: str,
        version_num: int,
        content: str,
        category: str | None,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str | None,
        owner_id: str,
        namespace: str | None,
        permission_mode: int | None,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        snapshot_at: Any,
        snapshot_by: str | None,
        change_type: str | None,
        commit_hash: str | None,
        parent_version_id: str | None,
        branch: str | None,
        merge_parents: Any,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO memory_versions (
                id, memory_id, version_num, content,
                category, subcategory, metadata, verbatim_content,
                owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                snapshot_at, snapshot_by, change_type,
                commit_hash, parent_version_id, branch, merge_parents
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, COALESCE(?, 600),
                ?, ?, ?, ?,
                COALESCE(?, CURRENT_TIMESTAMP), ?, COALESCE(?, 'create'),
                ?, ?, COALESCE(?, 'main'), ?
            )
            """,
            (
                version_id,
                memory_id,
                version_num,
                content,
                category,
                subcategory,
                metadata_json,
                verbatim_content,
                owner_id,
                namespace,
                permission_mode,
                source_model,
                source_provider,
                source_session,
                source_agent,
                snapshot_at,
                snapshot_by,
                change_type,
                commit_hash,
                parent_version_id,
                branch,
                _json_text(merge_parents, default=[]),
            ),
        )
        return "INSERT 0 1"

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT memory_id, owner_id, namespace, version_num, content, commit_hash, "
            "parent_version_id, branch, merge_parents, category, subcategory, metadata, "
            "verbatim_content, permission_mode, source_model, source_provider, source_session, "
            "source_agent, snapshot_at, snapshot_by, change_type "
            "FROM memory_versions WHERE id = ?",
            (version_id,),
        )


class SqliteBranchRepository(_SqliteRepository, BranchRepository):
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]:
        conn = self._conn(tx)
        if _is_root(user):
            live = await _fetch_one(conn, "SELECT 1 FROM memories WHERE id = ?", (memory_id,))
        else:
            live = await _fetch_one(
                conn,
                "SELECT 1 FROM memories WHERE id = ? AND owner_id = ? AND namespace = ?",
                (memory_id, user.user_id, user.namespace),
            )
        if not live:
            return {"success": False, "error": f"Memory {memory_id} not found"}

        if from_commit:
            start = await self._fetch_branch_start_by_commit(conn, memory_id, from_commit, user)
            if not start:
                return {"success": False, "error": "Commit not found"}
        else:
            start = await self._fetch_main_branch_start(conn, memory_id, user)
            if not start:
                return {"success": False, "error": "main branch not found"}

        await _execute(
            conn,
            "INSERT OR IGNORE INTO memory_branches (memory_id, name, head_version_id, created_by) VALUES (?, ?, ?, ?)",
            (memory_id, name, start["id"], user.user_id),
        )
        existing = await self._fetch_existing_branch(conn, memory_id, name, user)
        if existing is None:
            return {
                "success": False,
                "error": (
                    "branch exists but its head is not visible or points at a foreign memory version; "
                    "reconciliation required"
                ),
            }
        if existing["head_version_id"] == start["id"]:
            return {
                "success": True,
                "memory_id": memory_id,
                "branch": name,
                "commit_hash": existing["commit_hash"],
                "created_by": user.user_id,
                "idempotent": existing["head_version_id"] != start["id"],
            }
        return {
            "success": False,
            "error": f"branch '{name}' already exists at a different head; refusing to silently move it",
        }

    async def _fetch_branch_start_by_commit(
        self,
        conn: Any,
        memory_id: str,
        from_commit: str,
        user: UserContext,
    ) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT id, commit_hash FROM memory_versions WHERE memory_id = ? AND commit_hash = ?",
                (memory_id, from_commit),
            )
        params: list[Any] = [memory_id, from_commit]
        vis_clause = _version_visibility_clause(user, params)
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT id, commit_hash FROM memory_versions "
            f"WHERE memory_id = ? AND commit_hash = ? AND {vis_clause} AND namespace = ?",
            params,
        )

    async def _fetch_main_branch_start(self, conn: Any, memory_id: str, user: UserContext) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT mv.id, mv.commit_hash FROM memory_versions mv "
                "INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id "
                "WHERE mv.memory_id = ? AND mb.name = 'main'",
                (memory_id,),
            )
        params: list[Any] = [memory_id]
        vis_clause = _version_visibility_clause(user, params, table_alias="mv")
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT mv.id, mv.commit_hash FROM memory_versions mv "
            "INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id "
            f"WHERE mv.memory_id = ? AND mb.name = 'main' AND {vis_clause} AND mv.namespace = ?",
            params,
        )

    async def _fetch_existing_branch(
        self,
        conn: Any,
        memory_id: str,
        name: str,
        user: UserContext,
    ) -> Row | None:
        if _is_root(user):
            return await _fetch_one(
                conn,
                "SELECT mb.head_version_id, mv.commit_hash FROM memory_branches mb "
                "INNER JOIN memory_versions mv ON mv.id = mb.head_version_id AND mv.memory_id = mb.memory_id "
                "WHERE mb.memory_id = ? AND mb.name = ?",
                (memory_id, name),
            )
        params: list[Any] = [memory_id, name]
        vis_clause = _version_visibility_clause(user, params, table_alias="mv")
        params.append(user.namespace)
        return await _fetch_one(
            conn,
            "SELECT mb.head_version_id, mv.commit_hash FROM memory_branches mb "
            "INNER JOIN memory_versions mv ON mv.id = mb.head_version_id AND mv.memory_id = mb.memory_id "
            f"AND {vis_clause} AND mv.namespace = ? "
            "WHERE mb.memory_id = ? AND mb.name = ?",
            params[2:] + params[:2],
        )

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return
        params: list[Any] = []
        condition = _in_clause("memory_id", list(memory_ids), params)
        await _execute(self._conn(tx), f"DELETE FROM memory_branches WHERE {condition}", params)

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = []
        conditions = [_in_clause("memory_id", list(memory_ids), params)]
        if authorized_version_uuids is not None:
            conditions.append(_in_clause("id", list(authorized_version_uuids), params))
        return await _fetch_all(
            self._conn(tx),
            "SELECT memory_id, branch, id AS head_version_id "
            "FROM ("
            "  SELECT memory_id, branch, id, version_num, "
            "         ROW_NUMBER() OVER (PARTITION BY memory_id, branch ORDER BY version_num DESC) AS rn "
            "  FROM memory_versions "
            f"  WHERE {' AND '.join(conditions)}"
            ") ranked WHERE rn = 1",
            params,
        )

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(memory_id, name) DO UPDATE SET head_version_id = excluded.head_version_id",
            (memory_id, branch, head_version_id),
        )


class SqliteCompressionRepository(_SqliteRepository, CompressionRepository):
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        return await _fetch_sidecar(
            self._conn(tx),
            table="memory_compressed_variants",
            columns=(
                "memory_id, owner_id, winner_candidate_id, engine_id, engine_version, "
                "compressed_content, compressed_tokens, compression_ratio, quality_score, "
                "composite_score, scoring_profile, judge_model, selected_at"
            ),
            memory_id_column="memory_id",
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=None,
            bound_to_memories=True,
            hard_limit=hard_limit,
        )

    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        exists = await _fetch_val(
            self._conn(tx),
            "SELECT 1 FROM memory_compression_candidates WHERE id = ? AND memory_id = ? AND owner_id = ?",
            (candidate_id, memory_id, owner_id),
        )
        return bool(exists)

    async def insert_compressed_variant(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        owner_id: str,
        winner_candidate_id: str | None,
        engine_id: str,
        engine_version: str | None,
        compressed_content: str | None,
        compressed_tokens: int | None,
        compression_ratio: float | None,
        quality_score: float | None,
        composite_score: float | None,
        scoring_profile: str | None,
        judge_model: str | None,
        selected_at: Any,
    ) -> str:
        await _execute(
            self._conn(tx),
            """
            INSERT OR IGNORE INTO memory_compressed_variants (
                memory_id, owner_id, winner_candidate_id,
                engine_id, engine_version, compressed_content,
                compressed_tokens, compression_ratio,
                quality_score, composite_score,
                scoring_profile, judge_model, selected_at
            )
            VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                COALESCE(?, 'balanced'), ?,
                COALESCE(?, CURRENT_TIMESTAMP)
            )
            """,
            (
                memory_id,
                owner_id,
                winner_candidate_id,
                engine_id,
                engine_version,
                compressed_content,
                compressed_tokens,
                compression_ratio,
                quality_score,
                composite_score,
                scoring_profile,
                judge_model,
                selected_at,
            ),
        )
        return "INSERT 0 1"

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT owner_id, winner_candidate_id, engine_id, engine_version, compressed_content, "
            "compressed_tokens, compression_ratio, quality_score, composite_score, scoring_profile, "
            "judge_model, selected_at FROM memory_compressed_variants WHERE memory_id = ?",
            (memory_id,),
        )

    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        conn = self._conn(tx)
        total = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memory_compressed_variants",
        )
        avg_ratio = await _fetch_val(
            conn,
            "SELECT AVG(compression_ratio) FROM memory_compressed_variants",
        )
        unreviewed = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM memory_compressed_variants WHERE quality_score IS NULL",
        )
        return CompressionStatsRow(
            total_compressions=int(total or 0),
            average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
            unreviewed_compressions=int(unreviewed or 0),
        )


class SqliteCompressionQueueRepository(_SqliteRepository, CompressionQueueRepository):
    """SQLite impl of the v3.1 compression queue (job 019e7049 CHILD E).

    ABC-completeness only — SQLite is not a hive contest target. SQLite has
    no ``FOR UPDATE SKIP LOCKED``; the backend's ``transactional()`` opens
    ``BEGIN IMMEDIATE`` (a reserved write lock) and SQLite is single-writer,
    so the dequeue select+claim is already serialised against peers. Same
    schema + feature set + terminalization semantics as the other backends.
    """

    async def enqueue_compression(
        self,
        tx: Transaction,
        *,
        memory_ids: list[str],
        reason: str,
        priority: int,
        scoring_profile: str,
    ) -> list[str]:
        if not memory_ids:
            return []
        conn = self._conn(tx)
        placeholders = ",".join("?" for _ in memory_ids)
        known = await _fetch_all(
            conn,
            f"SELECT id, owner_id FROM memories WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(memory_ids),
        )
        owner_by_id = {r["id"]: r["owner_id"] for r in known}
        enqueued: list[str] = []
        for mid in memory_ids:
            if mid not in owner_by_id:
                continue
            # Dup-pending dedup: skip if this memory already has a
            # 'pending' queue row — avoids flooding the queue with
            # duplicate work for the same memory across multiple
            # enqueue calls (e.g. rapid on_write triggers).
            existing = await _fetch_val(
                conn,
                "SELECT 1 FROM memory_compression_queue WHERE memory_id = ? AND status = 'pending' LIMIT 1",
                (mid,),
            )
            if existing:
                continue
            await _execute(
                conn,
                "INSERT INTO memory_compression_queue "
                "(memory_id, owner_id, reason, priority, scoring_profile) "
                "VALUES (?, ?, ?, ?, ?)",
                (mid, owner_by_id[mid], reason, priority, scoring_profile),
            )
            enqueued.append(mid)
        return enqueued

    async def enqueue_all_compression(
        self,
        tx: Transaction,
        *,
        reason: str,
        priority: int,
        scoring_profile: str,
        category: str | None,
        only_uncompressed: bool,
        limit: int,
    ) -> int:
        conn = self._conn(tx)
        where_parts = ["m.deleted_at IS NULL"]
        params: list[Any] = [reason, priority, scoring_profile]
        if only_uncompressed:
            where_parts.append("NOT EXISTS (SELECT 1 FROM memory_compressed_variants v WHERE v.memory_id = m.id)")
        if category is not None:
            where_parts.append("m.category = ?")
            params.append(category)
        params.append(int(limit))
        sql = (
            "INSERT INTO memory_compression_queue "
            "(memory_id, owner_id, reason, priority, scoring_profile) "
            "SELECT m.id, m.owner_id, ?, ?, ? "
            f"FROM memories m WHERE {' AND '.join(where_parts)} "
            "ORDER BY length(m.content) DESC "
            "LIMIT ?"
        )
        return await _execute_count(conn, sql, tuple(params))

    async def dequeue_compression(
        self,
        tx: Transaction,
        *,
        limit: int,
    ) -> list[Row]:
        if limit <= 0:
            return []
        conn = self._conn(tx)
        claimed = await _fetch_all(
            conn,
            "SELECT id, memory_id, owner_id, reason, scoring_profile, attempts "
            "FROM memory_compression_queue "
            "WHERE status = 'pending' "
            "ORDER BY priority DESC, enqueued_at "
            "LIMIT ?",
            (int(limit),),
        )
        if not claimed:
            return []
        ids = [row["id"] for row in claimed]
        placeholders = ",".join("?" for _ in ids)
        await _execute(
            conn,
            "UPDATE memory_compression_queue "
            "SET status = 'running', started_at = CURRENT_TIMESTAMP, "
            "    attempts = attempts + 1 "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        )
        for row in claimed:
            row["attempts"] = int(row.get("attempts") or 0) + 1
        return claimed

    async def mark_compression_done(
        self,
        tx: Transaction,
        *,
        queue_id: str,
    ) -> None:
        await _execute(
            self._conn(tx),
            "UPDATE memory_compression_queue "
            "SET status = 'done', finished_at = CURRENT_TIMESTAMP, error = NULL "
            "WHERE id = ?",
            (queue_id,),
        )

    async def mark_compression_failed(
        self,
        tx: Transaction,
        *,
        queue_id: str,
        error: str,
    ) -> None:
        await _execute(
            self._conn(tx),
            "UPDATE memory_compression_queue "
            "SET status = 'failed', finished_at = CURRENT_TIMESTAMP, error = ? "
            "WHERE id = ?",
            (error, queue_id),
        )

    async def sweep_stale_compression(
        self,
        tx: Transaction,
        *,
        stale_threshold_secs: int,
        max_attempts: int,
    ) -> int:
        conn = self._conn(tx)
        # Single-writer transaction makes the read+classify+update atomic vs
        # peers; no SKIP LOCKED needed. Epoch-seconds comparison avoids
        # SQLite text-datetime pitfalls.
        stale = await _fetch_all(
            conn,
            "SELECT id, attempts, error FROM memory_compression_queue "
            "WHERE status = 'running' "
            "  AND (started_at IS NULL "
            "       OR CAST(strftime('%s', started_at) AS INTEGER) "
            "          < CAST(strftime('%s', 'now') AS INTEGER) - ?)",
            (int(stale_threshold_secs),),
        )
        swept = 0
        for row in stale:
            qid = row["id"]
            attempts = int(row.get("attempts") or 0)
            err = row.get("error")
            terminalize = attempts >= max_attempts and err is not None and not str(err).startswith("infra_retry:")
            if terminalize:
                await _execute(
                    conn,
                    "UPDATE memory_compression_queue "
                    "SET status = 'failed', finished_at = CURRENT_TIMESTAMP, error = ? "
                    "WHERE id = ?",
                    (
                        f"stranded_running: exceeded stale threshold after {attempts} attempts",
                        qid,
                    ),
                )
            elif attempts >= max_attempts:
                await _execute(
                    conn,
                    "UPDATE memory_compression_queue "
                    "SET status = 'pending', started_at = NULL, finished_at = NULL, "
                    "    attempts = MAX(attempts - 1, 0), "
                    "    error = 'infra_retry: stale-recovered without content-failure breadcrumb' "
                    "WHERE id = ?",
                    (qid,),
                )
            else:
                await _execute(
                    conn,
                    "UPDATE memory_compression_queue "
                    "SET status = 'pending', started_at = NULL, finished_at = NULL, error = NULL "
                    "WHERE id = ?",
                    (qid,),
                )
            swept += 1
        return swept


class SqliteWebhookRepository(_SqliteRepository, WebhookRepository):
    async def insert_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str | None = None,
        url: str,
        events: Sequence[str],
        secret: str | None = None,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> str:
        subscription_id = subscription_id or str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            "INSERT INTO webhook_subscriptions (id, url, events, secret, owner_id, namespace) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (subscription_id, url, json.dumps(list(events)), secret, owner_id, namespace),
        )
        return subscription_id

    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        conn = self._conn(tx)
        conditions = ["revoked = 0"]
        params: list[Any] = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = ?")
            params.append(namespace)
        subscriptions = await _fetch_all(
            conn,
            f"SELECT id, events, url, owner_id, namespace FROM webhook_subscriptions WHERE {' AND '.join(conditions)}",
            params,
        )
        body = json.dumps(
            {"event": event_type, "timestamp": _now_iso(), "data": payload},
            separators=(",", ":"),
            sort_keys=True,
        )
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_ids: list[str] = []
        for sub in subscriptions:
            if event_type not in _json_list(sub["events"]):
                continue
            delivery_id = str(uuid.uuid4())
            await _execute(
                conn,
                "INSERT INTO webhook_deliveries "
                "(id, subscription_id, event_type, payload, payload_hash, status, writer_revision) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    delivery_id,
                    sub["id"],
                    event_type,
                    body,
                    body_hash,
                    webhook_constants.NEW_CODE_WRITER_REVISION,
                ),
            )
            from mnemos.nats.webhook_events import publish_delivery_queued

            await publish_delivery_queued(
                delivery_id=delivery_id,
                subscription_id=sub["id"],
                event_type=event_type,
                url=sub["url"],
                payload_hash=body_hash,
                namespace=sub["namespace"],
                owner_id=sub["owner_id"],
            )
            delivery_ids.append(delivery_id)
        return delivery_ids

    async def fetch_deliveries(self, tx: Transaction, subscription_id: str | None = None) -> list[Row]:
        if subscription_id is None:
            return await _fetch_all(self._conn(tx), "SELECT * FROM webhook_deliveries ORDER BY created_at ASC")
        return await _fetch_all(
            self._conn(tx),
            "SELECT * FROM webhook_deliveries WHERE subscription_id = ? ORDER BY created_at ASC",
            (subscription_id,),
        )


class SqliteConsultationAuditRepository(_SqliteRepository, ConsultationAuditRepository):
    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        from mnemos.core.recommendation import choose_recommended_model

        rows = await _fetch_all(
            self._conn(tx),
            "SELECT provider, model_id, display_name, input_cost_per_mtok, output_cost_per_mtok, "
            "capabilities, graeae_weight, context_window "
            "FROM model_registry WHERE available = 1 AND deprecated = 0",
        )
        return choose_recommended_model(rows, task_type, cost_budget, quality_floor)

    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        model, _required = await self.fetch_recommended_model(tx, task_type, cost_budget, quality_floor)
        return model

    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0",
            (model,),
        )
        if row is not None:
            return row["provider"]
        if "/" not in model:
            return None
        head, tail = model.split("/", 1)
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE provider = ? AND model_id = ? "
            "AND available = 1 AND deprecated = 0",
            (head, tail),
        )
        return row["provider"] if row is not None else None

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            "SELECT provider, model_id, display_name FROM model_registry "
            "WHERE available = 1 AND deprecated = 0 "
            "ORDER BY graeae_weight IS NULL, graeae_weight DESC, model_id ASC",
        )

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        row = await _fetch_one(
            self._conn(tx),
            "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0 LIMIT 1",
            (model_id,),
        )
        return row["provider"] if row is not None else None


class SqliteOAuthRepository(_SqliteRepository, OAuthRepository):
    async def list_enabled_providers(self, tx: Transaction) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            "SELECT COALESCE(name, id) AS name, COALESCE(display_name, name, id) AS display_name, "
            "kind, enabled FROM oauth_providers WHERE enabled=1 ORDER BY display_name",
        )

    async def get_provider(self, tx: Transaction, name: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT COALESCE(name, id) AS name, kind, issuer_url, client_id, client_secret, scope, "
            "authorize_url, token_url, userinfo_url, enabled FROM oauth_providers "
            "WHERE COALESCE(name, id) = ?",
            (name,),
        )

    async def provision_or_link_user(
        self,
        tx: Transaction,
        *,
        provider: str,
        external_id: str,
        claims: dict[str, Any],
    ) -> tuple[str, str]:
        conn = self._conn(tx)
        raw_claims = json.dumps(claims)
        existing = await _fetch_one(
            conn,
            "SELECT id, user_id FROM oauth_identities WHERE provider=? AND external_id=?",
            (provider, external_id),
        )
        if existing:
            await _execute(
                conn,
                "UPDATE oauth_identities SET last_login_at=CURRENT_TIMESTAMP, raw_claims=? WHERE id=?",
                (raw_claims, existing["id"]),
            )
            return existing["user_id"], str(existing["id"])

        email = claims.get("email")
        display_name = claims.get("name") or claims.get("preferred_username")
        email_verified_claim = claims.get("email_verified")
        email_verified = (
            email_verified_claim
            if isinstance(email_verified_claim, bool)
            else isinstance(email_verified_claim, str) and email_verified_claim.strip().lower() == "true"
        )
        user_id = None
        if email and email_verified:
            link_target = await _fetch_one(conn, "SELECT id FROM users WHERE email=?", (email,))
            if link_target:
                user_id = link_target["id"]
        if user_id is None:
            user_id = (
                re.sub(r"[^a-zA-Z0-9._:-]+", "", f"{provider}:{external_id}")[:64]
                or f"{provider}:{uuid.uuid4().hex[:12]}"
            )
            await _execute(
                conn,
                "INSERT OR IGNORE INTO users (id, display_name, email, role) VALUES (?, ?, ?, 'user')",
                (user_id, display_name, email),
            )
        identity_id = uuid.uuid4().hex
        await _execute(
            conn,
            "INSERT INTO oauth_identities "
            "(id, provider_id, user_id, subject, provider, external_id, email, display_name, raw_claims, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (identity_id, provider, user_id, external_id, provider, external_id, email, display_name, raw_claims),
        )
        return user_id, identity_id

    async def create_session(self, tx: Transaction, **kwargs: Any) -> str:
        await _execute(
            self._conn(tx),
            "INSERT INTO oauth_sessions "
            "(id, session_id, user_id, provider_id, identity_id, expires_at, user_agent, ip_address) "
            "VALUES (?, ?, ?, COALESCE(?, 'oauth'), ?, ?, ?, ?)",
            (
                kwargs["session_id"],
                kwargs["session_id"],
                kwargs["user_id"],
                kwargs["identity_id"],
                kwargs["identity_id"],
                kwargs["expires_at"].isoformat()
                if hasattr(kwargs["expires_at"], "isoformat")
                else kwargs["expires_at"],
                kwargs["user_agent"],
                kwargs["ip_address"],
            ),
        )
        return kwargs["session_id"]

    async def revoke_session(self, tx: Transaction, session_id: str) -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                "UPDATE oauth_sessions SET revoked=1, revoked_at=CURRENT_TIMESTAMP WHERE session_id=? AND revoked=0",
                (session_id,),
            )
            > 0
        )

    async def revoke_all_sessions(self, tx: Transaction, user_id: str) -> int:
        return await _execute_count(
            self._conn(tx),
            "UPDATE oauth_sessions SET revoked=1, revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked=0",
            (user_id,),
        )

    async def get_identity_for_session(self, tx: Transaction, session_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT i.id, i.user_id, COALESCE(i.provider, i.provider_id) AS provider, "
            "COALESCE(i.external_id, i.subject) AS external_id, i.email, i.display_name, "
            "i.last_login_at, i.created FROM oauth_sessions s "
            "JOIN oauth_identities i ON i.id = s.identity_id "
            "WHERE s.session_id=? AND s.revoked=0",
            (session_id,),
        )


class SqliteSessionsRepository(_SqliteRepository, SessionsRepository):
    async def create_session(
        self, tx: Transaction, *, user_id: str, namespace: str, model: str, initial_context: str | None
    ) -> Row:
        conn = self._conn(tx)
        session_id = uuid.uuid4().hex
        await _execute(
            conn,
            "INSERT INTO sessions (id, user_id, namespace, model, last_activity) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (session_id, user_id, namespace, model),
        )
        if initial_context:
            await _execute(
                conn,
                "INSERT INTO session_messages (session_id, role, content) VALUES (?, 'system', ?)",
                (session_id, initial_context),
            )
        return await _fetch_one(
            conn,
            "SELECT id, created_at, model FROM sessions WHERE id=? AND user_id=? AND namespace=? AND deleted_at IS NULL",
            (session_id, user_id, namespace),
        )

    async def get_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT * FROM sessions WHERE id=? AND user_id=? AND namespace=? AND deleted_at IS NULL",
            (session_id, user_id, namespace),
        )

    async def list_injected_memory_ids(self, tx: Transaction, session_id: str, limit: int = 10) -> list[str]:
        rows = await _fetch_all(
            self._conn(tx),
            "SELECT memory_id FROM session_memory_injections WHERE session_id=? AND deleted_at IS NULL "
            "GROUP BY memory_id ORDER BY MAX(COALESCE(injection_timestamp, injected_at)) DESC LIMIT ?",
            (session_id, limit),
        )
        return [row["memory_id"] for row in rows]

    async def add_message(self, tx: Transaction, **kwargs: Any) -> Any:
        message_id = uuid.uuid4().hex
        await _execute(
            self._conn(tx),
            "INSERT INTO session_messages (id, session_id, role, content, model, tokens_used, memories_injected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                kwargs["session_id"],
                kwargs["role"],
                kwargs["content"],
                kwargs.get("model"),
                kwargs.get("tokens_used"),
                kwargs.get("memories_injected"),
            ),
        )
        return message_id

    async def fetch_provider_history(self, tx: Transaction, session_id: str) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            WITH first_system AS (
                SELECT id, role, content, timestamp FROM session_messages
                WHERE session_id=? AND role='system' AND deleted_at IS NULL
                ORDER BY timestamp ASC, id ASC LIMIT 1
            ), later_system AS (
                SELECT s.id, s.role, s.content, s.timestamp FROM session_messages s
                WHERE s.session_id=? AND s.role='system' AND s.deleted_at IS NULL
                AND s.id <> (SELECT id FROM first_system)
                ORDER BY s.timestamp DESC, s.id DESC LIMIT 4
            ), pinned AS (
                SELECT id, role, content, timestamp, 0 AS k FROM first_system
                UNION ALL SELECT id, role, content, timestamp, 0 AS k FROM later_system
            ), recent AS (
                SELECT id, role, content, timestamp, 1 AS k FROM session_messages
                WHERE session_id=? AND role <> 'system' AND deleted_at IS NULL
                ORDER BY timestamp DESC, id DESC LIMIT 10
            )
            SELECT role, content FROM (SELECT * FROM pinned UNION ALL SELECT * FROM recent)
            ORDER BY k, timestamp ASC, id ASC
            """,
            (session_id, session_id, session_id),
        )

    async def add_memory_injections(
        self, tx: Transaction, *, session_id: str, message_id: Any, memory_ids: Sequence[str]
    ) -> None:
        conn = self._conn(tx)
        for i, memory_id in enumerate(memory_ids):
            await _execute(
                conn,
                "INSERT INTO session_memory_injections (session_id, message_id, memory_id, relevance_score) "
                "VALUES (?, ?, ?, ?)",
                (session_id, message_id, memory_id, 0.9 - (i * 0.1)),
            )

    async def update_metrics(
        self, tx: Transaction, *, session_id: str, user_id: str, namespace: str, tokens_used: int
    ) -> None:
        await _execute(
            self._conn(tx),
            "UPDATE sessions SET message_count=message_count+2, total_tokens=total_tokens+?, "
            "last_activity=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND namespace=? AND deleted_at IS NULL",
            (tokens_used, session_id, user_id, namespace),
        )

    async def fetch_history(self, tx: Transaction, session_id: str, limit: int, offset: int) -> tuple[list[Row], int]:
        conn = self._conn(tx)
        rows = await _fetch_all(
            conn,
            "SELECT role, content, timestamp, model FROM session_messages "
            "WHERE session_id=? AND deleted_at IS NULL ORDER BY timestamp ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        total = await _fetch_val(
            conn,
            "SELECT COUNT(*) FROM session_messages WHERE session_id=? AND deleted_at IS NULL",
            (session_id,),
        )
        return rows, int(total or 0)

    async def delete_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                "DELETE FROM sessions WHERE id=? AND user_id=? AND namespace=? AND deleted_at IS NULL",
                (session_id, user_id, namespace),
            )
            > 0
        )


class SqliteConsultationsRepository(_SqliteRepository, ConsultationsRepository):
    async def resolve_tier_lineup(self, tx: Transaction, tier: str) -> list[Row]:
        rows = await _fetch_all(
            self._conn(tx),
            "SELECT provider, model_id, input_cost_per_mtok, output_cost_per_mtok, graeae_weight, arena_rank "
            "FROM model_registry WHERE available=1 AND deprecated=0",
        )
        if tier == "frontier":
            rows = [
                r
                for r in rows
                if (r.get("arena_rank") is not None and r["arena_rank"] <= 5) or (r["graeae_weight"] or 0) >= 0.95
            ]
        elif tier == "premium":
            rows = [
                r
                for r in rows
                if (r.get("arena_rank") is not None and 6 <= r["arena_rank"] <= 15)
                or (0.85 <= (r["graeae_weight"] or 0) < 0.95)
            ]
        else:
            rows = [
                r
                for r in rows
                if (r["graeae_weight"] or 0) >= 0.75
                and r["input_cost_per_mtok"] is not None
                and r["output_cost_per_mtok"] is not None
            ]
            rows = sorted(rows, key=lambda r: (r["input_cost_per_mtok"] or 0) + (r["output_cost_per_mtok"] or 0))
        seen: set[str] = set()
        out: list[Row] = []
        for row in sorted(rows, key=lambda r: (r["provider"], -(r["graeae_weight"] or 0))):
            if row["provider"] not in seen:
                out.append(row)
                seen.add(row["provider"])
        return out

    async def resolve_models(self, tx: Transaction, model_ids: Sequence[str]) -> list[Row]:
        if not model_ids:
            return []
        placeholders = ",".join("?" for _ in model_ids)
        return await _fetch_all(
            self._conn(tx),
            f"SELECT provider, model_id FROM model_registry WHERE model_id IN ({placeholders}) "
            "AND available=1 AND deprecated=0",
            tuple(model_ids),
        )

    async def create_consultation_with_audit(self, tx: Transaction, **kwargs: Any) -> Any:
        conn = self._conn(tx)
        consultation_id = uuid.uuid4().hex
        await _execute(
            conn,
            "INSERT INTO graeae_consultations "
            "(id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, owner_id, namespace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                consultation_id,
                kwargs["prompt"],
                kwargs["task_type"],
                kwargs["consensus_response"],
                kwargs["consensus_score"],
                kwargs["winning_muse"],
                kwargs["cost"],
                kwargs["latency_ms"],
                kwargs["mode"],
                kwargs["owner_id"],
                kwargs["namespace"],
            ),
        )
        prompt_hash = hashlib.sha256(kwargs["prompt"].encode()).hexdigest()
        response_hash = hashlib.sha256(kwargs["consensus_response"].encode()).hexdigest()
        prev = await _fetch_one(conn, "SELECT id, chain_hash FROM graeae_audit_log ORDER BY sequence_num DESC LIMIT 1")
        prev_chain = prev["chain_hash"] if prev else kwargs["genesis_hash"]
        chain_hash = hashlib.sha256((prev_chain + prompt_hash + response_hash).encode()).hexdigest()
        await _execute(
            conn,
            "INSERT INTO graeae_audit_log "
            "(consultation_id, prompt, prompt_hash, provider, response_text, response_hash, chain_hash, "
            "prev_id, prev_chain_hash, task_type, quality_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                consultation_id,
                kwargs["prompt"],
                prompt_hash,
                kwargs["winning_muse"],
                kwargs["consensus_response"],
                response_hash,
                chain_hash,
                prev["id"] if prev else None,
                prev_chain,
                kwargs["task_type"] or "reasoning",
                kwargs["consensus_score"],
            ),
        )
        for memory_id in kwargs["memory_ids"]:
            await _execute(
                conn,
                "INSERT OR IGNORE INTO consultation_memory_refs (consultation_id, memory_id, injected_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (consultation_id, memory_id),
            )
        return consultation_id

    async def list_audit_log(
        self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None, limit: int, offset: int
    ) -> list[Row]:
        conn = self._conn(tx)
        if root and namespace is None:
            return await _fetch_all(
                conn,
                "SELECT id, sequence_num, consultation_id, prompt_hash, response_hash, chain_hash, prev_id, "
                "task_type, provider, quality_score, created_at FROM graeae_audit_log WHERE deleted_at IS NULL "
                "ORDER BY sequence_num DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        owner_sql = "" if root else "c.owner_id=? AND "
        params = (namespace, limit, offset) if root else (user_id, namespace, limit, offset)
        return await _fetch_all(
            conn,
            "SELECT al.id, al.sequence_num, al.consultation_id, al.prompt_hash, al.response_hash, "
            + ("al.chain_hash" if root else "NULL AS chain_hash")
            + ", al.prev_id, al.task_type, al.provider, al.quality_score, al.created_at "
            "FROM graeae_audit_log al JOIN graeae_consultations c ON c.id=al.consultation_id "
            f"WHERE {owner_sql}c.namespace=? AND c.deleted_at IS NULL AND al.deleted_at IS NULL "
            "ORDER BY al.sequence_num DESC LIMIT ? OFFSET ?",
            params,
        )

    async def fetch_audit_chain(self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None) -> list[Row]:
        conn = self._conn(tx)
        if root and namespace is None:
            return await _fetch_all(
                conn,
                "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id FROM graeae_audit_log ORDER BY sequence_num ASC",
            )
        owner_sql = "" if root else "c.owner_id=? AND "
        params = (namespace,) if root else (user_id, namespace)
        return await _fetch_all(
            conn,
            "SELECT al.sequence_num, ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
            "al.prompt_hash, al.response_hash, al.chain_hash, al.prev_id, al.prev_chain_hash, "
            "(SELECT prev.chain_hash FROM graeae_audit_log prev WHERE prev.sequence_num < al.sequence_num "
            "ORDER BY prev.sequence_num DESC LIMIT 1) AS expected_prev_hash "
            "FROM graeae_audit_log al JOIN graeae_consultations c ON c.id=al.consultation_id "
            f"WHERE {owner_sql}c.namespace=? AND c.deleted_at IS NULL AND al.deleted_at IS NULL "
            "ORDER BY al.sequence_num ASC",
            params,
        )

    async def get_consultation(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> Row | None:
        conn = self._conn(tx)
        if root and namespace is None:
            return await _fetch_one(
                conn,
                "SELECT id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id=? AND deleted_at IS NULL",
                (consultation_id,),
            )
        owner_sql = "" if root else "owner_id=? AND "
        params = (consultation_id, namespace) if root else (consultation_id, user_id, namespace)
        return await _fetch_one(
            conn,
            "SELECT id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, created "
            f"FROM graeae_consultations WHERE id=? AND {owner_sql}namespace=? AND deleted_at IS NULL",
            params,
        )

    async def get_consultation_artifacts(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> tuple[Row | None, list[Row]]:
        consultation = await self.get_consultation(
            tx, consultation_id=consultation_id, root=root, user_id=user_id, namespace=namespace
        )
        if not consultation:
            return None, []
        refs = await _fetch_all(
            self._conn(tx),
            "SELECT memory_id, injected_at FROM consultation_memory_refs WHERE consultation_id=? ORDER BY injected_at",
            (consultation_id,),
        )
        return consultation, refs


class SqliteFederationRepository(_SqliteRepository, FederationRepository):
    _ALLOWED_PEER_COLS = {
        "name",
        "base_url",
        "auth_token",
        "namespace_filter",
        "category_filter",
        "enabled",
        "sync_interval_secs",
        "compat_mode",
    }

    @staticmethod
    def _peer_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["enabled"] = bool(out.get("enabled"))
        out["namespace_filter"] = _json_list(out.get("namespace_filter")) or None
        out["category_filter"] = _json_list(out.get("category_filter")) or None
        out["created"] = out.get("created") or out.get("created_at")
        out["updated"] = out.get("updated") or out.get("updated_at")
        out["last_sync_cursor"] = out.get("last_sync_cursor") or out.get("cursor_updated")
        return out

    def _conn(self, tx: Transaction) -> Any:
        return super()._conn(tx)

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: str | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        params: list[Any] = []
        where = ""
        if updated_after is not None and id_after is not None:
            where = "WHERE updated > ? OR (updated = ? AND id > ?)"
            params.extend([updated_after, updated_after, id_after])
        params.append(limit)
        return await _fetch_all(
            self._conn(tx),
            "SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated "
            f"FROM memories {where} ORDER BY updated ASC, id ASC LIMIT ?",
            params,
        )

    async def create_peer(
        self,
        tx: Transaction,
        *,
        name: str,
        base_url: str,
        auth_token: str,
        namespace_filter: Sequence[str] | None,
        category_filter: Sequence[str] | None,
        enabled: bool,
        sync_interval_secs: int,
        compat_mode: str,
    ) -> Row:
        peer_id = str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            """
            INSERT INTO federation_peers
              (id, name, base_url, auth_token, api_key, namespace_filter,
               category_filter, enabled, sync_interval_secs, compat_mode,
               created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                peer_id,
                name,
                base_url,
                auth_token,
                auth_token,
                _json_array_text(namespace_filter),
                _json_array_text(category_filter),
                int(enabled),
                sync_interval_secs,
                compat_mode,
            ),
        )
        row = await self.get_peer(tx, peer_id)
        assert row is not None
        return row

    async def list_peers(self, tx: Transaction) -> list[Row]:
        rows = await _fetch_all(self._conn(tx), "SELECT * FROM federation_peers ORDER BY name")
        return [self._peer_row(row) for row in rows]  # type: ignore[list-item]

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return self._peer_row(
            await _fetch_one(
                self._conn(tx),
                "SELECT * FROM federation_peers WHERE id = ?",
                (peer_id,),
            )
        )

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        assignments: list[str] = []
        params: list[Any] = []
        for col, value in updates.items():
            assignments.append(f"{col} = ?")
            if col in {"namespace_filter", "category_filter"}:
                params.append(_json_array_text(value))
            elif col == "enabled":
                params.append(int(bool(value)))
            else:
                params.append(value)
        assignments.append("updated = CURRENT_TIMESTAMP")
        params.append(peer_id)
        await _execute(
            self._conn(tx),
            f"UPDATE federation_peers SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        return await self.get_peer(tx, peer_id)

    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        await _execute(
            self._conn(tx),
            "INSERT INTO federation_peers (id, base_url, name, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url, "
            "name = excluded.name, enabled = excluded.enabled",
            (peer_id, base_url, name, int(enabled)),
        )

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                "DELETE FROM federation_peers WHERE id = ?",
                (peer_id,),
            )
            > 0
        )

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            SELECT id, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (peer_id, limit),
        )

    async def feed_query(
        self,
        tx: Transaction,
        *,
        since_updated: Any | None,
        since_id: str | None,
        namespaces: Sequence[str],
        categories: Sequence[str],
        limit: int,
        prefer_compressed: bool,
        include_embedding: bool = False,
    ) -> list[Row]:
        if prefer_compressed:
            raise NotImplementedError(
                "SQLite federation feed does not support prefer_compressed; "
                "use the Postgres server profile for compressed federation feeds."
            )
        memory_query_parts = [_eligibility.eligible_for_federation("m")]
        tombstone_query_parts = [
            _eligibility.eligible_for_federation_tombstone("m"),
            "m.consolidated_at IS NOT NULL",
        ]
        memory_params: list[Any] = []
        tombstone_params: list[Any] = []
        if since_updated is not None:
            memory_query_parts.append("(m.updated > ? OR (m.updated = ? AND m.id > ?))")
            memory_params.extend([since_updated, since_updated, since_id])
            tombstone_query_parts.append("(m.consolidated_at > ? OR (m.consolidated_at = ? AND m.id > ?))")
            tombstone_params.extend([since_updated, since_updated, since_id])
        if namespaces:
            placeholders = _placeholders(namespaces)
            memory_query_parts.append(f"m.namespace IN ({placeholders})")
            tombstone_query_parts.append(f"m.namespace IN ({placeholders})")
            memory_params.extend(namespaces)
            tombstone_params.extend(namespaces)
        if categories:
            placeholders = _placeholders(categories)
            memory_query_parts.append(f"m.category IN ({placeholders})")
            tombstone_query_parts.append(f"m.category IN ({placeholders})")
            memory_params.extend(categories)
            tombstone_params.extend(categories)

        memory_where_clause = " AND ".join(memory_query_parts)
        tombstone_where_clause = " AND ".join(tombstone_query_parts)
        # v6.1 F-1.2: optional embedding + embedding_model literal columns.
        # See docs/v6.1-federation-embeddings-copy.md. SQLite reads
        # embeddings from memory_embeddings join table (not memories.embedding
        # which is a synced backup) — LEFT JOIN so rows with no embedding
        # still appear.
        if include_embedding:
            from mnemos.core.config import get_settings as _gs

            try:
                _http_model = embed_http_model_override()
                _model = _http_model or (_gs().providers.inference_embed_model or "").strip() or "unknown"
            except Exception:
                _model = "unknown"
            _model_escaped = _model.replace("'", "''")
            mem_embed = f", me.embedding AS embedding, '{_model_escaped}' AS embedding_model"
            mem_embed_join = "LEFT JOIN memory_embeddings me ON me.memory_id = m.id"
            tomb_embed = ", NULL AS embedding, NULL AS embedding_model"
        else:
            mem_embed = ""
            mem_embed_join = ""
            tomb_embed = ""
        return await _fetch_all(
            self._conn(tx),
            f"""
            SELECT *
            FROM (
                SELECT NULL AS type,
                       m.id,
                       m.content,
                       m.category,
                       m.subcategory,
                       m.metadata,
                       m.quality_rating,
                       m.verbatim_content,
                       m.owner_id,
                       m.namespace,
                       m.permission_mode,
                       m.source_model,
                       m.source_provider,
                       m.source_session,
                       m.source_agent,
                       m.created,
                       m.updated,
                       m.archived_at,
                       NULL AS consolidated_into,
                       NULL AS consolidated_at,
                       NULL AS compressed_content
                       {mem_embed}
                FROM memories m
                {mem_embed_join}
                WHERE {memory_where_clause}

                UNION ALL

                SELECT 'consolidation' AS type,
                       m.id,
                       NULL AS content,
                       NULL AS category,
                       NULL AS subcategory,
                       NULL AS metadata,
                       NULL AS quality_rating,
                       NULL AS verbatim_content,
                       NULL AS owner_id,
                       m.namespace,
                       NULL AS permission_mode,
                       NULL AS source_model,
                       NULL AS source_provider,
                       NULL AS source_session,
                       NULL AS source_agent,
                       m.created,
                       m.consolidated_at AS updated,
                       NULL AS archived_at,
                       m.consolidated_into,
                       m.consolidated_at,
                       NULL AS compressed_content
                       {tomb_embed}
                FROM memories m
                WHERE {tombstone_where_clause}
            ) feed
            ORDER BY updated ASC, id ASC
            LIMIT ?
            """,
            [*memory_params, *tombstone_params, limit],
        )

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        query_parts = [_eligibility.eligible_for_federation("m"), "m.id = ?"]
        params: list[Any] = [memory_id]
        if namespaces:
            query_parts.append(f"m.namespace IN ({_placeholders(namespaces)})")
            params.extend(namespaces)
        if categories:
            query_parts.append(f"m.category IN ({_placeholders(categories)})")
            params.extend(categories)
        return await _fetch_one(
            self._conn(tx),
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated, archived_at
            FROM memories m
            WHERE {" AND ".join(query_parts)}
            """,
            params,
        )

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        # v6.1 F-1: copy_embeddings flag (migration 0028) — COALESCE so
        # DB rows that pre-date the migration still return 0 instead of
        # KeyError on the consumer-side .get('copy_embeddings').
        return self._peer_row(
            await _fetch_one(
                self._conn(tx),
                """
            SELECT id, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor,
                   compat_mode,
                   COALESCE(copy_embeddings, 0) AS copy_embeddings
            FROM federation_peers WHERE id = ?
            """,
                (peer_id,),
            )
        )

    async def update_peer_schema_check(self, tx: Transaction, peer_id: str, peer_version: str | None) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET peer_mnemos_version = ?, last_schema_check_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (peer_version, peer_id),
        )

    async def record_schema_abort(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        peer_version: str | None,
        cursor_before: Any,
        error: str,
        is_transient: bool,
    ) -> None:
        await self.update_peer_schema_check(tx, peer_id, peer_version)
        log_id = await self.create_sync_log(tx, peer_id, cursor_before)
        await self.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=0,
            memories_new=0,
            memories_updated=0,
            error=error,
            cursor_after=cursor_before,
        )
        if is_transient:
            await _execute(
                self._conn(tx),
                """
                UPDATE federation_peers
                SET last_sync_at = datetime(
                        CURRENT_TIMESTAMP,
                        printf('-%d seconds', sync_interval_secs),
                        '+60 seconds'
                    ),
                    last_error = ?,
                    last_error_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, peer_id),
            )
        else:
            await self.record_sync_error(tx, peer_id, error)

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        log_id = str(uuid.uuid4())
        await _execute(
            self._conn(tx),
            """
            INSERT INTO federation_sync_log
              (id, peer_id, direction, status, started_at, cursor_before)
            VALUES (?, ?, 'pull', 'started', CURRENT_TIMESTAMP, ?)
            """,
            (log_id, peer_id, cursor_before),
        )
        return log_id

    async def finish_sync_log(
        self,
        tx: Transaction,
        *,
        log_id: Any,
        memories_pulled: int,
        memories_new: int,
        memories_updated: int,
        error: str | None,
        cursor_after: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_sync_log
            SET finished_at = CURRENT_TIMESTAMP,
                memories_pulled = ?,
                memories_new = ?,
                memories_updated = ?,
                records_seen = ?,
                records_written = ?,
                status = ?,
                error = ?,
                cursor_after = ?
            WHERE id = ?
            """,
            (
                memories_pulled,
                memories_new,
                memories_updated,
                memories_pulled,
                memories_new + memories_updated,
                "error" if error else "ok",
                error,
                cursor_after,
                str(log_id),
            ),
        )

    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET last_sync_at = CURRENT_TIMESTAMP,
                last_error = ?,
                last_error_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, peer_id),
        )

    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            UPDATE federation_peers
            SET last_sync_at = CURRENT_TIMESTAMP,
                last_sync_cursor = ?,
                cursor_updated = ?,
                last_error = NULL,
                last_error_at = NULL,
                total_pulled = total_pulled + ?
            WHERE id = ?
            """,
            (cursor, cursor, total_pulled, peer_id),
        )

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            SELECT id, name, sync_interval_secs, last_sync_at
            FROM federation_peers
            WHERE enabled = 1
              AND (
                last_sync_at IS NULL
                OR datetime(last_sync_at, printf('+%d seconds', sync_interval_secs)) <= CURRENT_TIMESTAMP
              )
            ORDER BY COALESCE(
                datetime(last_sync_at, printf('+%d seconds', sync_interval_secs)),
                '1970-01-01T00:00:00'
            )
            LIMIT ?
            """,
            (limit,),
        )

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT federation_remote_updated FROM memories WHERE id = ?",
            (local_id,),
        )

    async def insert_federated_memory(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        peer_name: str,
        remote_updated: Any,
    ) -> bool:
        try:
            await _execute(
                self._conn(tx),
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'federation', ?, 644,
                        ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    local_id,
                    content,
                    category,
                    subcategory,
                    metadata_json,
                    verbatim_content,
                    quality_rating,
                    namespace,
                    source_model,
                    source_provider,
                    source_session,
                    source_agent,
                    peer_name,
                    remote_updated,
                    remote_updated,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    async def update_federated_memory_if_newer(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        remote_updated: Any,
    ) -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                """
            UPDATE memories SET
              content = ?,
              category = ?,
              subcategory = ?,
              metadata = ?,
              verbatim_content = ?,
              quality_rating = ?,
              namespace = ?,
              federation_remote_updated = ?,
              updated = ?
            WHERE id = ?
              AND (
                  federation_remote_updated IS NULL
                  OR federation_remote_updated < ?
              )
            """,
                (
                    content,
                    category,
                    subcategory,
                    metadata_json,
                    verbatim_content,
                    quality_rating,
                    namespace,
                    remote_updated,
                    remote_updated,
                    local_id,
                    remote_updated,
                ),
            )
            > 0
        )

    async def apply_consolidation_tombstone(
        self,
        tx: Transaction,
        *,
        local_id: str,
        local_canonical_id: str,
        consolidated_at: Any,
        remote_id: str,
        canonical_remote_id: str,
        peer_name: str,
    ) -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                """
            UPDATE memories
            SET consolidated_into = ?,
                consolidated_at = COALESCE(?, CURRENT_TIMESTAMP),
                permission_mode = 400,
                metadata = json_set(
                    COALESCE(NULLIF(metadata, ''), '{}'),
                    '$.federation_consolidation',
                    json_object(
                        'remote_id', ?,
                        'remote_consolidated_into', ?,
                        'peer', ?
                    )
                )
            WHERE id = ?
              AND (consolidated_into IS NULL OR consolidated_into <> ?)
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = ?
              )
            """,
                (
                    local_canonical_id,
                    consolidated_at,
                    remote_id,
                    canonical_remote_id,
                    peer_name,
                    local_id,
                    local_canonical_id,
                    local_canonical_id,
                ),
            )
            > 0
        )

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        local_id = f"fed:{peer_name}:{memory_id}"
        return await _execute_count(
            self._conn(tx),
            """
            DELETE FROM memories
            WHERE id = ?
              AND federation_source = ?
            """,
            (local_id, peer_name),
        )


class SqliteStateRepository(_SqliteRepository, StateRepository):
    async def get(
        self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default"
    ) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            "SELECT key, value, updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = ? AND namespace = ? AND key = ? AND deleted_at IS NULL",
            (owner_id, namespace, key),
        )

    async def set(
        self,
        tx: Transaction,
        key: str,
        value: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        expires_at: Any | None = None,
    ) -> Row | None:
        _ = expires_at
        return await _fetch_one(
            self._conn(tx),
            "INSERT INTO state (owner_id, namespace, key, value, updated) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(owner_id, namespace, key) DO UPDATE SET "
            "value = excluded.value, updated = CURRENT_TIMESTAMP, version = state.version + 1 "
            "WHERE state.deleted_at IS NULL "
            "RETURNING key, value, updated, version, owner_id, namespace",
            (owner_id, namespace, key, value),
        )

    async def delete(self, tx: Transaction, key: str, *, owner_id: str = "default", namespace: str = "default") -> bool:
        return (
            await _execute_count(
                self._conn(tx),
                "DELETE FROM state WHERE owner_id = ? AND namespace = ? AND key = ? AND deleted_at IS NULL",
                (owner_id, namespace, key),
            )
            > 0
        )

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        params: list[Any] = [owner_id, namespace]
        sql = (
            "SELECT key, updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = ? AND namespace = ? AND deleted_at IS NULL ORDER BY key"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        return await _fetch_all(self._conn(tx), sql, params)

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        return await _execute_count(
            self._conn(tx),
            "DELETE FROM state WHERE owner_id = ? AND namespace = ? AND deleted_at IS NULL",
            (owner_id, namespace),
        )


class SqliteAuditChainRepository(_SqliteRepository, AuditChainRepository):
    """SQLite impl of v6.2 M-2.2.1 audit chain.

    Tables: ``memory_audit_chain`` + ``memory_audit_roots``
    (migration ``migrations_v6_2_audit_chain_sqlite.sql`` shipped
    alongside the protocol scaffold).

    No SKIP LOCKED in SQLite — the single-writer pattern (one
    serialized connection per backend) makes concurrent-sealer
    isolation unnecessary. Multiple sealer instances against the
    same DB would deadlock anyway; deploy at most one sealer per
    SQLite node.
    """

    async def get_latest_audit_entry(
        self,
        tx: Transaction,
        memory_id: bytes,
    ) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            """
            SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM memory_audit_chain
            WHERE memory_id = ?
            ORDER BY signed_at DESC
            LIMIT 1
            """,
            (memory_id,),
        )

    async def insert_audit_entry(
        self,
        tx: Transaction,
        *,
        entry_id: bytes,
        memory_id: bytes,
        prev_entry_id: bytes | None,
        prev_entry_hash: bytes | None,
        op: str,
        payload_hash: bytes,
        writer_id: str,
        writer_pubkey: bytes,
        signature: bytes,
        signed_at: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            INSERT INTO memory_audit_chain (
                entry_id, memory_id, prev_entry_id, prev_entry_hash,
                op, payload_hash, writer_id, writer_pubkey,
                signature, signed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                memory_id,
                prev_entry_id,
                prev_entry_hash,
                op,
                payload_hash,
                writer_id,
                writer_pubkey,
                signature,
                signed_at,
            ),
        )

    async def claim_unsealed_window(
        self,
        tx: Transaction,
        *,
        max_window_seconds: int,
        limit: int,
    ) -> list[Row]:
        """Claim oldest unsealed entries. No row lock — single-writer
        SQLite makes the SKIP-LOCKED dance moot.

        ``signed_at`` is stored as TEXT (ISO 8601); compare against
        ``datetime('now', '-N seconds')`` to pick rows older than the
        window cutoff.
        """
        # signed_at is stored as ISO 8601 TEXT. Route handler may emit
        # "2026-05-24T16:00:00+00:00" (Python datetime.isoformat()) or
        # the SQL-style "2026-05-24 16:00:00". Wrap both sides in
        # datetime() so SQLite normalizes the format before comparing.
        cutoff_expr = f"datetime('now', '-{int(max_window_seconds)} seconds')"
        return await _fetch_all(
            self._conn(tx),
            f"""
            SELECT entry_id, signature, signed_at
            FROM memory_audit_chain
            WHERE global_root IS NULL
              AND datetime(signed_at) <= {cutoff_expr}
            ORDER BY datetime(signed_at) ASC, entry_id ASC
            LIMIT ?
            """,
            (limit,),
        )

    async def stamp_window_with_root(
        self,
        tx: Transaction,
        *,
        entry_ids: list[bytes],
        global_root: bytes,
        starting_seq: int,
    ) -> None:
        if not entry_ids:
            return
        conn = self._conn(tx)
        for offset, eid in enumerate(entry_ids):
            await _execute(
                conn,
                """
                UPDATE memory_audit_chain
                SET global_root = ?, global_seq = ?
                WHERE entry_id = ?
                """,
                (global_root, starting_seq + offset, eid),
            )

    async def insert_audit_root(
        self,
        tx: Transaction,
        *,
        global_root: bytes,
        window_start: Any,
        window_end: Any,
        entry_count: int,
        root_signature: bytes,
        signer_pubkey: bytes,
        sealed_at: Any,
    ) -> None:
        await _execute(
            self._conn(tx),
            """
            INSERT INTO memory_audit_roots (
                global_root, window_start, window_end, entry_count,
                root_signature, signer_pubkey, sealed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                global_root,
                window_start,
                window_end,
                entry_count,
                root_signature,
                signer_pubkey,
                sealed_at,
            ),
        )

    async def list_window_entries(
        self,
        tx: Transaction,
        global_root: bytes,
    ) -> list[Row]:
        return await _fetch_all(
            self._conn(tx),
            """
            SELECT entry_id, memory_id, signature, signed_at,
                   global_seq, payload_hash, op
            FROM memory_audit_chain
            WHERE global_root = ?
            ORDER BY datetime(signed_at) ASC, entry_id ASC
            """,
            (global_root,),
        )

    async def get_audit_entry_by_id(
        self,
        tx: Transaction,
        entry_id: bytes,
    ) -> Row | None:
        return await _fetch_one(
            self._conn(tx),
            """
            SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM memory_audit_chain
            WHERE entry_id = ?
            """,
            (entry_id,),
        )

    async def get_chain_stats(self, tx: Transaction) -> dict:
        conn = self._conn(tx)
        chain = await _fetch_one(
            conn,
            """
            SELECT
                COUNT(*) AS total_entries,
                SUM(CASE WHEN global_root IS NULL THEN 1 ELSE 0 END) AS unsealed_count,
                MIN(CASE WHEN global_root IS NULL THEN signed_at END) AS oldest_unsealed_signed_at
            FROM memory_audit_chain
            """,
            (),
        )
        root = await _fetch_one(
            conn,
            """
            SELECT
                COUNT(*) AS sealed_root_count,
                MAX(sealed_at) AS last_sealed_at
            FROM memory_audit_roots
            """,
            (),
        )
        chain = chain or {}
        root = root or {}
        return {
            "total_entries": int(chain.get("total_entries") or 0),
            "unsealed_count": int(chain.get("unsealed_count") or 0),
            "oldest_unsealed_signed_at": chain.get("oldest_unsealed_signed_at") or None,
            "sealed_root_count": int(root.get("sealed_root_count") or 0),
            "last_sealed_at": root.get("last_sealed_at") or None,
        }

    async def get_latest_audit_entries_batch(
        self,
        tx: Transaction,
        memory_ids: list[bytes],
    ) -> dict[bytes, Row]:
        """Batch via CTE with ROW_NUMBER() OVER (PARTITION BY memory_id
        ORDER BY signed_at DESC). SQLite 3.25+ supports window
        functions. Single round-trip beats N+1 serial reads.
        """
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        rows = await _fetch_all(
            self._conn(tx),
            f"""
            WITH ranked AS (
              SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                     op, payload_hash, writer_id, writer_pubkey,
                     signature, signed_at, global_root, global_seq,
                     ROW_NUMBER() OVER (
                       PARTITION BY memory_id
                       ORDER BY datetime(signed_at) DESC, entry_id DESC
                     ) AS rn
              FROM memory_audit_chain
              WHERE memory_id IN ({placeholders})
            )
            SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM ranked
            WHERE rn = 1
            """,
            tuple(memory_ids),
        )
        return {r["memory_id"]: r for r in rows}


class SqliteBackend:
    """SQLite persistence facade backed by one serialized connection."""

    _supports_core_persistence = True
    _supports_oauth_persistence = True
    _supports_sessions_persistence = True
    _supports_consultations_persistence = True
    _supports_federation_persistence = True
    _supports_audit_persistence = True
    _supports_state_persistence = True

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    uses_sqlite_vec = True
    uses_fts5 = True
    # On SQLite, insert_memory writes memories.embedding but semantic_search
    # reads the separate memory_embeddings table, which is only populated by
    # upsert_memory_embedding / backfill — so an inline-embedded row is NOT
    # semantically searchable until then. Column-based backends (Oracle/DB2/
    # MySQL/Postgres) write+read the same memories.embedding and default this
    # to True via getattr. (issue #38 / review gate 2026-06-23)
    inline_embedding_searchable = False

    def __init__(self, db_path: Path | str, settings: Any):
        self._db_path = Path(db_path)
        self._settings = settings
        self._conn: Any | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._vec_loaded = False
        self._memories = SqliteMemoryRepository()
        self._kg_triples = SqliteKGRepository()
        self._memory_versions = SqliteVersionRepository()
        self._memory_branches = SqliteBranchRepository()
        self._compression = SqliteCompressionRepository()
        self._compression_queue = SqliteCompressionQueueRepository()
        self._webhooks = SqliteWebhookRepository()
        self._consultations_audit = SqliteConsultationAuditRepository()
        self._oauth = SqliteOAuthRepository()
        self._sessions = SqliteSessionsRepository()
        self._consultations = SqliteConsultationsRepository()
        self._federation = SqliteFederationRepository()
        self._state_kv = SqliteStateRepository()
        self._audit_chain = SqliteAuditChainRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def vec_loaded(self) -> bool:
        return self._vec_loaded

    @property
    def capabilities(self) -> set[str]:
        return {"core", "oauth", "sessions", "consultations", "federation", "audit", "state"}

    @property
    def capability_details(self) -> set[str]:
        return set(FULL_STORAGE_CAPABILITY_DETAILS)

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        return await _fetch_all(
            _sqlite_tx(tx).conn,
            "SELECT category, half_life_days, decay_kind, floor FROM memory_category_decay",
            (),
        )

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        await _execute(
            _sqlite_tx(tx).conn,
            """
            INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (category) DO UPDATE SET
                half_life_days = excluded.half_life_days,
                decay_kind = excluded.decay_kind,
                floor = excluded.floor
            """,
            (category, half_life_days, decay_kind, floor),
        )

    async def create_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
        entry_date: Any | None,
        topic: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> Row:
        conn = _sqlite_tx(tx).conn
        if entry_date is None:
            await _execute(
                conn,
                """
                INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                VALUES (?, ?, ?, date('now'), ?, ?, ?)
                """,
                (entry_id, owner_id, namespace, topic, content, _json_text(metadata, default={})),
            )
        else:
            await _execute(
                conn,
                """
                INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, owner_id, namespace, str(entry_date), topic, content, _json_text(metadata, default={})),
            )
        row = await _fetch_one(
            conn,
            """
            SELECT id, entry_date, topic, content, metadata, created
            FROM journal WHERE id = ?
            """,
            (entry_id,),
        )
        if row is None:
            raise RuntimeError("journal insert returned no row")
        return row

    async def list_journal_entries(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        entry_date: Any | None,
        topic: str | None,
        search: str | None,
        limit: int,
    ) -> list[Row]:
        conn = _sqlite_tx(tx).conn
        if entry_date is not None:
            return await _fetch_all(
                conn,
                """
                SELECT id, entry_date, topic, content, metadata, created
                FROM journal WHERE owner_id = ? AND namespace = ? AND entry_date = ?
                ORDER BY created DESC LIMIT ?
                """,
                (owner_id, namespace, str(entry_date), limit),
            )
        if topic:
            return await _fetch_all(
                conn,
                """
                SELECT id, entry_date, topic, content, metadata, created
                FROM journal WHERE owner_id = ? AND namespace = ? AND topic = ?
                ORDER BY created DESC LIMIT ?
                """,
                (owner_id, namespace, topic, limit),
            )
        if search:
            return await _fetch_all(
                conn,
                """
                SELECT id, entry_date, topic, content, metadata, created
                FROM journal
                WHERE owner_id = ? AND namespace = ?
                  AND (lower(content) LIKE lower(?) OR lower(topic) LIKE lower(?))
                ORDER BY created DESC LIMIT ?
                """,
                (owner_id, namespace, f"%{search}%", f"%{search}%", limit),
            )
        return await _fetch_all(
            conn,
            """
            SELECT id, entry_date, topic, content, metadata, created
            FROM journal WHERE owner_id = ? AND namespace = ?
            ORDER BY created DESC LIMIT ?
            """,
            (owner_id, namespace, limit),
        )

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        cursor = await _execute(
            _sqlite_tx(tx).conn,
            "DELETE FROM journal WHERE id = ? AND owner_id = ? AND namespace = ?",
            (entry_id, owner_id, namespace),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    async def register_oauth_token(
        self,
        tx: Transaction,
        *,
        token: str | bytes,
        user_id: str,
        provider: str,
        scopes: Sequence[str] | dict[str, Any] | None = None,
        expires_at: Any = None,
        refresh_token: str | None = None,
    ) -> Row:
        conn = _sqlite_tx(tx).conn
        await _execute(
            conn,
            "INSERT INTO oauth_tokens "
            "(token, user_id, provider, scopes, expires_at, refresh_token, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)",
            (_token_blob(token), user_id, provider, _json_text(scopes, default=[]), expires_at, refresh_token),
        )
        row = await self.lookup_oauth_token(tx, token=token, touch=False)
        assert row is not None
        return row

    async def lookup_oauth_token(self, tx: Transaction, *, token: str | bytes, touch: bool = True) -> Row | None:
        conn = _sqlite_tx(tx).conn
        raw = _token_blob(token)
        if touch:
            await _execute(conn, "UPDATE oauth_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE token = ?", (raw,))
        row = await _fetch_one(
            conn,
            "SELECT token, user_id, provider, scopes, expires_at, refresh_token, created_at, last_used_at "
            "FROM oauth_tokens WHERE token = ?",
            (raw,),
        )
        if row is None:
            return None
        out = dict(row)
        out["token"] = bytes(out["token"]).hex()
        out["scopes"] = _json_value(out.get("scopes"), [])
        return out

    async def revoke_oauth_token(self, tx: Transaction, *, token: str | bytes) -> bool:
        return (
            await _execute_count(_sqlite_tx(tx).conn, "DELETE FROM oauth_tokens WHERE token = ?", (_token_blob(token),))
            > 0
        )

    async def start_oauth_flow(
        self,
        tx: Transaction,
        *,
        state: str | bytes,
        provider: str,
        csrf_token: str,
        return_url: str | None,
        expires_at: Any,
    ) -> Row:
        conn = _sqlite_tx(tx).conn
        await _execute(
            conn,
            "INSERT INTO oauth_state (state, provider, csrf_token, return_url, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (_token_blob(state), provider, csrf_token, return_url, expires_at),
        )
        row = await _fetch_one(conn, "SELECT * FROM oauth_state WHERE state = ?", (_token_blob(state),))
        assert row is not None
        out = dict(row)
        out["state"] = bytes(out["state"]).hex()
        return out

    async def redeem_oauth_state(self, tx: Transaction, *, state: str | bytes) -> Row | None:
        conn = _sqlite_tx(tx).conn
        raw = _token_blob(state)
        row = await _fetch_one(
            conn,
            "SELECT state, provider, csrf_token, return_url, created_at, expires_at "
            "FROM oauth_state WHERE state = ? AND datetime(expires_at) > datetime('now')",
            (raw,),
        )
        await _execute(conn, "DELETE FROM oauth_state WHERE state = ?", (raw,))
        if row is None:
            return None
        out = dict(row)
        out["state"] = bytes(out["state"]).hex()
        return out

    async def create_session(
        self,
        tx: Transaction,
        *,
        session_id: str | bytes | uuid.UUID | None = None,
        user_id: str,
        expires_at: Any,
        metadata: dict[str, Any] | None = None,
    ) -> Row:
        sid = session_id or uuid.uuid4()
        conn = _sqlite_tx(tx).conn
        await _execute(
            conn,
            "INSERT INTO sessions (session_id, user_id, started_at, last_active_at, expires_at, metadata) "
            "VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)",
            (_uuid_to_blob(sid), user_id, expires_at, _json_text(metadata, default={})),
        )
        row = await self.lookup_session(tx, session_id=sid)
        assert row is not None
        return row

    async def lookup_session(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> Row | None:
        row = await _fetch_one(
            _sqlite_tx(tx).conn,
            "SELECT session_id, user_id, started_at, last_active_at, expires_at, metadata "
            "FROM sessions WHERE session_id = ? AND datetime(expires_at) > datetime('now')",
            (_uuid_to_blob(session_id),),
        )
        if row is None:
            return None
        out = dict(row)
        out["session_id"] = _blob_to_uuid(out.get("session_id"))
        out["metadata"] = _json_value(out.get("metadata"), {})
        return out

    async def update_session_active(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> bool:
        return (
            await _execute_count(
                _sqlite_tx(tx).conn,
                "UPDATE sessions SET last_active_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (_uuid_to_blob(session_id),),
            )
            > 0
        )

    async def expire_session(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> bool:
        return (
            await _execute_count(
                _sqlite_tx(tx).conn,
                "UPDATE sessions SET expires_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (_uuid_to_blob(session_id),),
            )
            > 0
        )

    async def log_session_event(
        self,
        tx: Transaction,
        *,
        session_id: str | bytes | uuid.UUID,
        event_kind: str,
        payload: dict[str, Any] | None = None,
    ) -> Row:
        row = await _fetch_one(
            _sqlite_tx(tx).conn,
            "INSERT INTO session_logs (session_id, event_kind, payload, ts) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) RETURNING id, session_id, event_kind, payload, ts",
            (_uuid_to_blob(session_id), event_kind, _json_text(payload, default={})),
        )
        assert row is not None
        out = dict(row)
        out["session_id"] = _blob_to_uuid(out.get("session_id"))
        out["payload"] = _json_value(out.get("payload"), {})
        return out

    async def create_consultation(
        self,
        tx: Transaction,
        *,
        consultation_id: str | bytes | uuid.UUID | None = None,
        user_id: str,
        prompt: str,
        task_type: str | None,
        mode: str | None,
        status: str = "pending",
    ) -> Row:
        cid = consultation_id or uuid.uuid4()
        conn = _sqlite_tx(tx).conn
        await _execute(
            conn,
            "INSERT INTO consultations (id, user_id, prompt, task_type, mode, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)",
            (_uuid_to_blob(cid), user_id, prompt, task_type, mode, status),
        )
        row = await self.fetch_consultation(tx, consultation_id=cid)
        assert row is not None
        return row

    async def append_consultation_response(
        self,
        tx: Transaction,
        *,
        consultation_id: str | bytes | uuid.UUID,
        provider: str,
        model_id: str,
        response: str,
        final_score: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
    ) -> Row:
        row = await _fetch_one(
            _sqlite_tx(tx).conn,
            "INSERT INTO consultation_responses "
            "(consultation_id, provider, model_id, response, final_score, tokens_in, tokens_out, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) RETURNING *",
            (
                _uuid_to_blob(consultation_id),
                provider,
                model_id,
                response,
                final_score,
                tokens_in,
                tokens_out,
                latency_ms,
            ),
        )
        assert row is not None
        out = dict(row)
        out["consultation_id"] = _blob_to_uuid(out.get("consultation_id"))
        return out

    async def fetch_consultation(self, tx: Transaction, *, consultation_id: str | bytes | uuid.UUID) -> Row | None:
        conn = _sqlite_tx(tx).conn
        row = await _fetch_one(
            conn,
            "SELECT id, user_id, prompt, task_type, mode, status, created_at, completed_at "
            "FROM consultations WHERE id = ?",
            (_uuid_to_blob(consultation_id),),
        )
        if row is None:
            return None
        out = dict(row)
        out["id"] = _blob_to_uuid(out.get("id"))
        responses = await _fetch_all(
            conn,
            "SELECT id, consultation_id, provider, model_id, response, final_score, tokens_in, tokens_out, latency_ms, created_at "
            "FROM consultation_responses WHERE consultation_id = ? ORDER BY id",
            (_uuid_to_blob(consultation_id),),
        )
        out["responses"] = [dict(r, consultation_id=_blob_to_uuid(r["consultation_id"])) for r in responses]
        return out

    async def list_consultations(
        self,
        tx: Transaction,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        where: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT id, user_id, prompt, task_type, mode, status, created_at, completed_at FROM consultations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        rows = await _fetch_all(_sqlite_tx(tx).conn, sql, [*params, limit, offset])
        out: list[Row] = []
        for row in rows:
            item = dict(row)
            item["id"] = _blob_to_uuid(item.get("id"))
            out.append(item)
        return out

    async def record_usage_ledger(
        self,
        tx: Transaction,
        record: UsageLedgerRecord,
    ) -> UsageLedgerResult:
        conn = _sqlite_tx(tx).conn
        auth_method = "api"
        try:
            row = await _fetch_one(
                conn,
                "SELECT auth_method FROM subscription_plans WHERE provider = ? AND plan_name = ?",
                (record.provider, record.tier),
            )
            if row:
                auth_method = str(row["auth_method"] if isinstance(row, dict) else row[0]).lower()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise

        registry_row: Row | None = None
        if auth_method != "subscription":
            try:
                registry_row = await _fetch_one(
                    conn,
                    """
                    SELECT input_cost_per_mtok, output_cost_per_mtok, raw
                    FROM model_registry
                    WHERE provider = ? AND model_id = ?
                    """,
                    (record.provider, record.model),
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                logger.warning(
                    "usage_ledger model_registry table missing for provider=%s model=%s; recording est_cost_usd=0",
                    record.provider,
                    record.model,
                )
        if auth_method == "subscription":
            est_cost = Decimal("0")
        elif record.est_cost_usd is not None:
            est_cost = Decimal(str(record.est_cost_usd))
        elif registry_row is None:
            logger.warning(
                "usage_ledger model_registry price missing for provider=%s model=%s; recording est_cost_usd=0",
                record.provider,
                record.model,
            )
            est_cost = Decimal("0")
        else:
            input_cost = Decimal(str(registry_row["input_cost_per_mtok"] or 0))
            output_cost = Decimal(str(registry_row["output_cost_per_mtok"] or 0))
            reasoning_cost = output_cost
            raw = registry_row["raw"] if isinstance(registry_row, dict) else None
            if raw:
                try:
                    parsed = json.loads(str(raw))
                    reasoning_cost = Decimal(str(parsed.get("reasoning_cost_per_mtok") or output_cost))
                except (TypeError, ValueError):
                    reasoning_cost = output_cost
            est_cost = (
                Decimal(record.tokens_in) * input_cost
                + Decimal(record.tokens_out) * output_cost
                + Decimal(record.tokens_reasoning) * reasoning_cost
            ) / Decimal(1000000)

        cursor = await _execute(
            conn,
            """
            INSERT INTO usage_ledger (
                provider, model, task_kind, tokens_in, tokens_out,
                tokens_reasoning, est_cost_usd, latency_ms, outcome,
                caller_subsystem, tier, session_id, request_count,
                plan_window_id, path_kind, subscription_amortized
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id, est_cost_usd
            """,
            (
                record.provider,
                record.model,
                record.task_kind,
                record.tokens_in,
                record.tokens_out,
                record.tokens_reasoning,
                str(est_cost),
                record.latency_ms,
                record.outcome,
                record.caller_subsystem,
                record.tier,
                record.session_id,
                record.request_count,
                record.plan_window_id,
                record.path_kind or "api",
                1 if auth_method == "subscription" else 0,
            ),
        )
        try:
            row = await _maybe_await(cursor.fetchone())
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                await _maybe_await(close())
        if row is None:
            raise RuntimeError("usage_ledger insert returned no row")
        return UsageLedgerResult(
            id=int(row["id"] if isinstance(row, dict) else row[0]),
            est_cost_usd=Decimal(str(row["est_cost_usd"] if isinstance(row, dict) else row[1])),
        )

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self._db_path != Path(":memory:"):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if aiosqlite is not None:
            conn = await aiosqlite.connect(str(self._db_path))
        else:
            conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = _dict_factory
        await self._check_sqlite_version(conn)
        self._conn = conn
        await _execute(conn, "PRAGMA journal_mode=WAL")
        await _execute(conn, "PRAGMA foreign_keys=ON")
        await self._register_functions(conn)
        await self._load_sqlite_vec(conn)
        await self._apply_migrations(conn)
        await self._ensure_repository_columns(conn)
        await self._create_vec_virtual_table(conn)
        # Wire the configured dim into the memory repository so the runtime
        # search + upsert paths can enforce the same invariant the startup
        # checks established. Without this, a misconfigured embedding
        # endpoint after open() could poison the table or silently degrade
        # search until the next restart.
        self._memories._expected_embedding_dim = self._resolve_embedding_dim()
        await _commit(conn)

    async def _check_sqlite_version(self, conn: Any) -> None:
        raw_version = await _fetch_val(conn, "SELECT sqlite_version()")
        version = tuple(int(part) for part in str(raw_version).split(".")[:3])
        if version < MIN_SQLITE_VERSION:
            await _call(conn.close)
            required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
            raise RuntimeError(f"SQLite {required}+ is required for UPDATE ... RETURNING support; found {raw_version}")

    async def _register_functions(self, conn: Any) -> None:
        await _call(conn.create_function, "mnemos_cosine_similarity", 2, _cosine_similarity)
        await _call(conn.create_function, "mnemos_content_sha256", 1, _content_hash_for_sqlite)

    async def _load_sqlite_vec(self, conn: Any) -> None:
        try:
            await _call(conn.enable_load_extension, True)
            try:
                await _call(conn.load_extension, "vec0")
            finally:
                await _call(conn.enable_load_extension, False)
            self._vec_loaded = True
            return
        except Exception as exc:
            logger.debug("sqlite-vec load_extension('vec0') unavailable: %s", exc)

        try:  # pragma: no cover - depends on optional sqlite-vec wheel.
            import sqlite_vec

            raw_conn = getattr(conn, "_conn", conn)
            sqlite_vec.load(raw_conn)
            self._vec_loaded = True
        except Exception as exc:  # pragma: no cover - optional path.
            logger.debug("sqlite-vec Python loader unavailable; using cosine UDF fallback: %s", exc)

    async def _apply_migrations(self, conn: Any) -> None:
        migrations_dir = Path(__file__).resolve().parents[2] / "mnemos" / "db_migrations" / "migrations_sqlite"
        for migration_name in SQLITE_MIGRATION_FILES:
            migration_path = migrations_dir / migration_name
            if not migration_path.exists():
                continue
            try:
                await _executescript(conn, migration_path.read_text())
            except sqlite3.OperationalError as exc:
                # SQLite ``ALTER TABLE ADD COLUMN`` cannot be wrapped
                # in IF NOT EXISTS and re-running it on a column that
                # already exists raises ``duplicate column name``.
                # Treat that specific error as a no-op so upgrade
                # migrations stay idempotent — every other operational
                # error still propagates.
                if "duplicate column name" in str(exc).lower():
                    logger.debug(
                        "sqlite migration %s: column already present, skipping (%s)",
                        migration_name,
                        exc,
                    )
                    continue
                raise

    async def _ensure_repository_columns(self, conn: Any) -> None:
        await self._ensure_columns(
            conn,
            "sessions",
            {
                "model": "model TEXT NOT NULL DEFAULT 'gpt-4o'",
                "last_activity": "last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "message_count": "message_count INTEGER NOT NULL DEFAULT 0",
                "total_tokens": "total_tokens INTEGER NOT NULL DEFAULT 0",
                "deleted_at": "deleted_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "session_messages",
            {
                "model": "model TEXT",
                "tokens_used": "tokens_used INTEGER",
                "memories_injected": "memories_injected INTEGER",
                "deleted_at": "deleted_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "session_memory_injections",
            {
                "message_id": "message_id TEXT",
                "injection_timestamp": "injection_timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "deleted_at": "deleted_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "oauth_providers",
            {
                "name": "name TEXT",
                "display_name": "display_name TEXT",
                "kind": "kind TEXT NOT NULL DEFAULT 'oidc'",
                "enabled": "enabled INTEGER NOT NULL DEFAULT 1",
                "issuer_url": "issuer_url TEXT",
                "scope": "scope TEXT NOT NULL DEFAULT 'openid email profile'",
                "authorize_url": "authorize_url TEXT",
                "token_url": "token_url TEXT",
                "userinfo_url": "userinfo_url TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "oauth_identities",
            {
                "provider": "provider TEXT",
                "external_id": "external_id TEXT",
                "display_name": "display_name TEXT",
                "raw_claims": "raw_claims TEXT NOT NULL DEFAULT '{}'",
                "last_login_at": "last_login_at TEXT",
                "created": "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            },
        )
        await self._ensure_columns(
            conn,
            "oauth_sessions",
            {
                "session_id": "session_id TEXT",
                "identity_id": "identity_id TEXT",
                "user_agent": "user_agent TEXT",
                "ip_address": "ip_address TEXT",
                "last_used_at": "last_used_at TEXT",
                "revoked_at": "revoked_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "state",
            {
                "updated_by": "updated_by TEXT",
                "version": "version INTEGER NOT NULL DEFAULT 1",
                "deleted_at": "deleted_at TEXT",
            },
        )
        await self._ensure_columns(
            conn,
            "memories",
            {
                "federation_remote_updated": "federation_remote_updated TEXT",
                "archived_at": "archived_at TEXT",
                "consolidated_into": "consolidated_into TEXT",
                "consolidated_at": "consolidated_at TEXT",
                "deleted_at": "deleted_at TEXT",
                "content_hash": "content_hash TEXT",
            },
        )
        await _execute(
            conn,
            "UPDATE memories SET content_hash = mnemos_content_sha256(content) WHERE content_hash IS NULL",
        )
        await self._ensure_columns(
            conn,
            "federation_peers",
            {
                "auth_token": "auth_token TEXT",
                "namespace_filter": "namespace_filter TEXT",
                "category_filter": "category_filter TEXT",
                "sync_interval_secs": "sync_interval_secs INTEGER NOT NULL DEFAULT 300",
                "last_sync_cursor": "last_sync_cursor TEXT",
                "last_error": "last_error TEXT",
                "last_error_at": "last_error_at TEXT",
                "total_pulled": "total_pulled INTEGER NOT NULL DEFAULT 0",
                "compat_mode": "compat_mode TEXT NOT NULL DEFAULT 'strict'",
                "peer_mnemos_version": "peer_mnemos_version TEXT",
                "last_schema_check_at": "last_schema_check_at TEXT",
                "created": "created TEXT",
                "updated": "updated TEXT",
                "copy_embeddings": "copy_embeddings INTEGER NOT NULL DEFAULT 0",
            },
        )
        await self._ensure_columns(
            conn,
            "federation_sync_log",
            {
                "started_at": "started_at TEXT",
                "finished_at": "finished_at TEXT",
                "memories_pulled": "memories_pulled INTEGER NOT NULL DEFAULT 0",
                "memories_new": "memories_new INTEGER NOT NULL DEFAULT 0",
                "memories_updated": "memories_updated INTEGER NOT NULL DEFAULT 0",
                "cursor_before": "cursor_before TEXT",
                "cursor_after": "cursor_after TEXT",
            },
        )

    async def _ensure_columns(self, conn: Any, table: str, definitions: dict[str, str]) -> None:
        rows = await _fetch_all(conn, f"PRAGMA table_info({table})")
        existing = {row["name"] for row in rows}
        for column, definition in definitions.items():
            if column not in existing:
                await _execute(conn, f"ALTER TABLE {table} ADD COLUMN {definition}")

    async def _create_vec_virtual_table(self, conn: Any) -> None:
        dim = self._resolve_embedding_dim()
        # Guard 1: if the vec0 virtual table already exists at a different dim,
        # the CREATE ... IF NOT EXISTS DDL would be a silent no-op and the
        # service would run searches against the wrong dim. Fatal — operator
        # must explicitly migrate.
        existing_vec_dim = await self._existing_vec_table_dim(conn)
        if existing_vec_dim is not None and existing_vec_dim != dim:
            raise RuntimeError(
                f"SQLite vec0 dimension mismatch: memory_embedding_vec exists "
                f"at dim={existing_vec_dim} but MNEMOS_EMBEDDING_DIM resolves "
                f"to {dim}. The vec0 virtual table cannot be re-sized in place. "
                f"To migrate: stop this service, run "
                f"`sqlite3 {self._db_path} 'DROP TABLE memory_embedding_vec; "
                f"DELETE FROM memory_embeddings;'`, then restart the service to "
                f"recreate the table at the new dim and re-embed all memories. "
                f"Refusing to start to prevent silent search degradation."
            )
        # Guard 2: scan ALL fallback memory_embeddings rows. Stale-dim rows
        # would silently score 0.0 in cosine similarity against new-dim
        # queries — search degrades to "rank by recency" with no warning.
        # We can't trust a single-row sample because a DB poisoned before
        # the runtime dim guard landed (c9007dd) can have mixed-dim rows.
        fb_histogram = await self._scan_fallback_embedding_dims(conn)
        bad_dims = {d: c for d, c in fb_histogram.items() if d != dim}
        if bad_dims:
            shape = ", ".join(f"dim={d} x{c}" for d, c in sorted(bad_dims.items()))
            raise RuntimeError(
                f"SQLite fallback embedding dimension mismatch: "
                f"memory_embeddings has {sum(bad_dims.values())} rows at "
                f"non-configured dims ({shape}); MNEMOS_EMBEDDING_DIM "
                f"resolves to {dim}. Searching new-dim queries against "
                f"stale-dim rows produces meaningless cosine scores. To "
                f"migrate: stop this service, run "
                f"`sqlite3 {self._db_path} 'DELETE FROM memory_embeddings;'`, "
                f"then restart and re-embed all memories at the new dim. "
                f"Refusing to start to prevent silent search degradation."
            )
        if not self._vec_loaded:
            return
        try:
            await _execute(
                conn,
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_embedding_vec USING vec0(embedding float[{dim}])",
            )
        except Exception as exc:
            self._vec_loaded = False
            logger.debug("sqlite-vec virtual table creation failed; using fallback memory_embeddings table: %s", exc)

    async def _existing_vec_table_dim(self, conn: Any) -> Optional[int]:
        """Return the embedded float[N] dim of memory_embedding_vec if it exists.

        Returns None if the table doesn't exist (fresh install) or if the DDL
        can't be parsed. Parses the DDL string from sqlite_master.sql; format is
        ``CREATE VIRTUAL TABLE ... USING vec0(embedding float[<N>])`` where N
        is a positive integer.
        """
        row = await _fetch_one(
            conn,
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_embedding_vec'",
        )
        if not row:
            return None
        ddl = row.get("sql") if isinstance(row, dict) else row[0]
        if not ddl:
            return None
        match = re.search(r"float\[(\d+)\]", ddl, re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def _scan_fallback_embedding_dims(self, conn: Any) -> dict[int, int]:
        """Return a histogram of {dim: row_count} across all memory_embeddings.

        A DB that was running BEFORE the runtime dim guard landed could have
        accumulated mixed-dim rows from a misconfigured embedding endpoint
        (e.g. the model was switched mid-flight). Sampling a single row could
        miss this — if the sample happened to match the configured dim,
        startup would succeed while stale-dim rows lurked in the table and
        silently scored 0.0 in cosine similarity.

        This scans every row. The query uses sqlite's json_array_length
        which is O(1) per row given the stored format. For the PYTHIA fleet
        (~9k memories) this is millisecond-scale at boot. Returns empty
        dict if the table is absent or has no rows.
        """
        row_meta = await _fetch_one(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_embeddings'",
        )
        if not row_meta:
            return {}
        rows = await _fetch_all(
            conn,
            "SELECT json_array_length(embedding) AS dim, COUNT(*) AS cnt "
            "FROM memory_embeddings WHERE embedding IS NOT NULL "
            "GROUP BY json_array_length(embedding)",
        )
        histogram: dict[int, int] = {}
        for r in rows:
            dim = r.get("dim") if isinstance(r, dict) else r[0]
            cnt = r.get("cnt") if isinstance(r, dict) else r[1]
            if dim is None:
                continue
            try:
                histogram[int(dim)] = int(cnt)
            except (TypeError, ValueError):
                continue
        return histogram

    def _resolve_embedding_dim(self) -> int:
        # Settings can be None in tests + lite-CLI paths. Fall back to 768
        # (nomic-embed-text default) when no override is available.
        try:
            dim = self._settings.database.embedding_dim
        except AttributeError:
            return 768
        # sqlite-vec's SQLITE_VEC_VEC0_MAX_DIMENSIONS upstream caps at 8192;
        # values above silently fail the CREATE VIRTUAL TABLE and drop us to
        # the slower JSON/UDF path. Reject those before they bite.
        if not isinstance(dim, int) or dim < 1 or dim > 8192:
            logger.warning(
                "MNEMOS_EMBEDDING_DIM=%r out of supported range [1, 8192] "
                "(sqlite-vec SQLITE_VEC_VEC0_MAX_DIMENSIONS); falling back to "
                "768. Set the env var to your model's actual dim.",
                dim,
            )
            return 768
        return dim

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        if self._closed:
            raise RuntimeError("SQLite backend is closed")
        async with self._lock:
            await self.open()
            assert self._conn is not None
            await _execute(self._conn, "BEGIN IMMEDIATE")
            tx = SqliteTransaction(self._conn)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    async def insert_pantheon_routing_audit(
        self,
        tx: Transaction,
        record: Mapping[str, Any],
    ) -> None:
        cost_usd = record.get("cost_usd")
        if isinstance(cost_usd, Decimal):
            cost_usd = float(cost_usd)
        await _execute(
            _sqlite_tx(tx).conn,
            """
            INSERT INTO pantheon_routing_audit
                   (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
                    latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("request_id"),
                record.get("tenant_user_id"),
                record.get("alias_or_model"),
                record.get("resolved_to"),
                record.get("outcome"),
                record.get("latency_ms"),
                record.get("tokens_in"),
                record.get("tokens_out"),
                cost_usd,
                record.get("error_class"),
                record.get("payload_json"),
            ),
        )

    @property
    def memories(self) -> MemoryRepository:
        return self._memories

    @property
    def kg_triples(self) -> KGRepository:
        return self._kg_triples

    @property
    def memory_versions(self) -> VersionRepository:
        return self._memory_versions

    @property
    def memory_branches(self) -> BranchRepository:
        return self._memory_branches

    @property
    def compression(self) -> CompressionRepository:
        return self._compression

    @property
    def compression_queue(self) -> CompressionQueueRepository:
        return self._compression_queue

    @property
    def webhooks(self) -> WebhookRepository:
        return self._webhooks

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit

    @property
    def oauth(self) -> OAuthRepository:
        return self._oauth

    @property
    def sessions(self) -> SessionsRepository:
        return self._sessions

    @property
    def consultations(self) -> ConsultationsRepository:
        return self._consultations

    @property
    def federation(self) -> FederationRepository:
        return self._federation

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv

    @property
    def audit_chain(self) -> AuditChainRepository:
        return self._audit_chain

    async def ping(self) -> bool:
        try:
            await self.open()
            assert self._conn is not None
            await _fetch_val(self._conn, "SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._closed:
            return
        if self._conn is not None:
            await _call(self._conn.close)
        self._conn = None
        self._closed = True
