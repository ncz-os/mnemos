"""MySQL 9.0+ persistence backend for MNEMOS.

Uses ``aiomysql`` (asyncio wrapper around PyMySQL) with a native async
connection pool.  MySQL 9.0+ is required for the native ``VECTOR``
column type and ``VECTOR_DISTANCE`` / ``TO_VECTOR`` functions
used by semantic search.

Key SQL-level differences from Postgres/Oracle:

- Positional ``%s`` placeholders (aiomysql / PyMySQL convention).
- ``TO_VECTOR(%s)`` to bind an embedding string; ``VECTOR_DISTANCE``
  for ANN distance (MySQL 9.0 nomenclature).
- ``DATETIME(6)`` with ``SET time_zone = '+00:00'`` for UTC timestamps.
- ``INSERT … ON DUPLICATE KEY UPDATE id = id`` to preserve
  ``ON CONFLICT DO NOTHING`` semantics for duplicate-key writes.
- ``COALESCE`` (no NVL / NVL2), ``LIMIT n`` (no FETCH FIRST).
- ``MATCH (col) AGAINST (%s IN BOOLEAN MODE)`` for full-text search.
- No advisory locks — ``supports_advisory_locks = False``.
- No LISTEN/NOTIFY — ``supports_listen_notify = False``.
- No pgvector — ``supports_pgvector = False``.

This backend implements the core memory / FTS / vector-search and state
key-value surfaces.
KG triples, compression, versioning, and federation surfaces raise
``NotImplementedError`` (same posture as the initial Oracle port) and will be
filled in across subsequent slices following M4 review. Webhooks are explicitly
declared unsupported and gated before callers can reach the outbox methods.

Configuration example::

    [database]
    backend = "mysql"
    dsn     = "mysql://mnemos:secret@db-primary:3306/mnemos"
    # pool_min_size / pool_max_size from [server] or env vars as usual.

References:
- aiomysql: https://aiomysql.readthedocs.io/
- MySQL 9.0 VECTOR: https://dev.mysql.com/doc/refman/9.0/en/vector-functions.html
- VECTOR_DISTANCE: https://dev.mysql.com/doc/refman/9.0/en/vector-functions.html
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.core.config import embedding_dim_env, runtime_env_value_stripped
from mnemos.persistence.base import (
    BranchRepository,
    CompressionQueueRepository,
    CompressionRepository,
    CORE_CAPABILITY,
    MYSQL_CAPABILITY_DETAILS,
    ConsultationAuditRepository,
    FederationRepository,
    KG_CAPABILITY,
    KGRepository,
    MemoryRepository,
    STATE_CAPABILITY,
    STATE_DETAIL_CAPABILITY,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

_LOG = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_DIM = embedding_dim_env()
_DEFAULT_MYSQL_POOL_MIN = 2
_DEFAULT_MYSQL_POOL_MAX = 10
_DEFAULT_MYSQL_ACQUIRE_TIMEOUT = 60.0

# MySQL 9.0 native vector column declaration
_VECTOR_COLUMN = f"VECTOR({_DEFAULT_EMBEDDING_DIM})"


# ── helpers ───────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %.1f", name, raw, default)
        return default


def _content_hash(content: Any) -> str:
    normalized = ("" if content is None else str(content)).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_and_format_vector(embedding: Sequence[float]) -> str:
    """Validate and format an embedding into a MySQL TO_VECTOR-compatible string.

    MySQL 9.0 ``TO_VECTOR`` accepts JSON arrays: ``'[0.1,0.2,...]'``.
    """
    if not embedding:
        raise ValueError("embedding must not be empty")
    values = list(embedding)
    formatted: list[str] = []
    for idx, value in enumerate(values):
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"embedding[{idx}] is not float-convertible: {value!r}") from exc
        if not math.isfinite(num):
            raise ValueError(f"embedding[{idx}] is non-finite ({num!r}); NaN and Inf are rejected.")
        formatted.append(f"{num:.7f}")
    return "[" + ",".join(formatted) + "]"


def _cosine_distance_python(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine distance (1 - cosine_similarity) for two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def _rank_score_sort_key(row: Row) -> float:
    rank = row.get("rank_score") if isinstance(row, dict) else None
    try:
        score = float(rank)
    except (TypeError, ValueError):
        return math.inf
    return score if math.isfinite(score) else math.inf


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

    if not isinstance(row, dict):
        return date.min
    return _coerce_date(row.get("updated")) or _coerce_date(row.get("created")) or date.min


def _is_vec_distance_unsupported(exc: BaseException) -> bool:
    """True when exc indicates MySQL lacks built-in vector distance functions."""
    msg = str(exc)
    return "1305" in msg and ("VEC_DISTANCE" in msg or "VEC_COSINE" in msg or "VEC_L2" in msg)


def _is_unique_violation(exc: BaseException) -> bool:
    """True when exc is a MySQL unique-constraint violation (error 1062)."""
    # aiomysql wraps as pymysql.err.IntegrityError; also check string form
    msg = str(exc)
    if "1062" in msg or "Duplicate entry" in msg:
        return True
    errno = getattr(getattr(exc, "args", (None,))[0], "errno", None)
    return errno == 1062


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """Parse ``mysql://user:pass@host:port/db`` into aiomysql kwargs."""
    if "://" not in dsn:
        raise ValueError(f"Invalid MySQL DSN (must start with mysql://): {dsn!r}")
    parsed = urlparse(dsn)
    kwargs: dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "db": (parsed.path or "/mnemos").lstrip("/") or "mnemos",
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if parsed.username:
        kwargs["user"] = unquote(parsed.username)
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    return kwargs


def _render_visibility(
    visibility: VisibilityFilter,
    *,
    table_alias: str = "",
) -> tuple[str, list[Any]]:
    """Render a VisibilityFilter into a MySQL WHERE fragment and positional params."""
    p = f"{table_alias}." if table_alias else ""

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return "", []
        return f"{p}namespace = %s", [visibility.namespace]

    if visibility.namespace is None:
        return "1=0", []

    if visibility.scope == VisibilityScope.OWN_ONLY:
        return (
            f"{p}owner_id = %s AND {p}namespace = %s",
            [visibility.user_id, visibility.namespace],
        )

    group_ids = list(visibility.group_ids)
    params: list[Any] = [visibility.user_id]
    if group_ids:
        placeholders = ", ".join(["%s"] * len(group_ids))
        group_clause = f"{p}group_id IN ({placeholders})"
        params += group_ids
    else:
        group_clause = "0=1"

    return (
        "("
        f"{p}owner_id = %s"
        f" OR {p}federation_source IS NOT NULL"
        f" OR (MOD({p}permission_mode, 10) >= 4)"
        f" OR (MOD(FLOOR(COALESCE({p}permission_mode, 0) / 10), 10) >= 4"
        f" AND {p}group_id IS NOT NULL AND {group_clause})"
        f") AND {p}namespace = %s",
        params + [visibility.namespace],
    )


async def _fetch_all_dicts(cursor: Any) -> list[Row]:
    """Fetch all rows as a list of column-name-keyed dicts."""
    rows = await cursor.fetchall()
    if not rows:
        return []
    cols = [col[0].lower() for col in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


async def _fetchone_dict(cursor: Any) -> Row | None:
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [col[0].lower() for col in cursor.description]
    return dict(zip(cols, row))


def _stub_method(method_name: str):
    """Build a coroutine stub that raises NotImplementedError on call."""

    async def _stub(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"mysql: {method_name} not yet implemented")

    _stub.__name__ = method_name
    return _stub


_MYSQL_WEBHOOKS_UNSUPPORTED = (
    "mysql: webhooks are not supported by MysqlBackend yet; "
    "use a backend with webhook outbox support before accessing backend.webhooks"
)


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL_MEMORIES = f"""\
CREATE TABLE IF NOT EXISTS memories (
    id                VARCHAR(64)   NOT NULL,
    content           LONGTEXT      NOT NULL,
    content_hash      VARCHAR(64)   NOT NULL,
    category          VARCHAR(128)  NOT NULL,
    subcategory       VARCHAR(128),
    metadata          LONGTEXT,
    quality_rating    INT           NOT NULL DEFAULT 3,
    verbatim_content  LONGTEXT,
    compressed_content LONGTEXT,
    source_model      VARCHAR(256),
    source_provider   VARCHAR(256),
    source_session    VARCHAR(512),
    source_agent      VARCHAR(256),
    owner_id          VARCHAR(256)  NOT NULL,
    namespace         VARCHAR(256)  NOT NULL,
    permission_mode   INT           NOT NULL DEFAULT 0,
    group_id          VARCHAR(256),
    federation_source VARCHAR(512),
    consolidated_into VARCHAR(64),
    recall_count      INT           NOT NULL DEFAULT 0,
    last_recalled_at  DATETIME(6),
    archived_at       DATETIME(6),
    deleted_at        DATETIME(6),
    created           DATETIME(6)   NOT NULL DEFAULT NOW(6),
    updated           DATETIME(6)   NOT NULL DEFAULT NOW(6),
    embedding         {_VECTOR_COLUMN},
    PRIMARY KEY (id),
    INDEX idx_memories_ns_cat  (namespace, category),
    INDEX idx_memories_owner   (owner_id, namespace),
    INDEX idx_memories_hash    (content_hash),
    FULLTEXT INDEX idx_memories_ft (content)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_KG_TRIPLES = """\
CREATE TABLE IF NOT EXISTS kg_triples (
    id           VARCHAR(64)  NOT NULL,
    subject      VARCHAR(512) NOT NULL,
    predicate    VARCHAR(256) NOT NULL,
    object       VARCHAR(512) NOT NULL,
    subject_type VARCHAR(128),
    object_type  VARCHAR(128),
    valid_from   DATETIME(6),
    valid_until  DATETIME(6),
    memory_id    VARCHAR(64),
    confidence   FLOAT,
    created      DATETIME(6)  NOT NULL DEFAULT NOW(6),
    owner_id     VARCHAR(256) NOT NULL,
    namespace    VARCHAR(256),
    deleted_at   DATETIME(6),
    PRIMARY KEY (id),
    INDEX idx_kg_memory  (memory_id),
    INDEX idx_kg_owner   (owner_id, namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_COMPRESSION_QUEUE = """\
CREATE TABLE IF NOT EXISTS memory_compression_queue (
    id              VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    memory_id       VARCHAR(64)  NOT NULL,
    owner_id        VARCHAR(256) NOT NULL,
    reason          VARCHAR(256) NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    priority        INT          NOT NULL DEFAULT 0,
    scoring_profile VARCHAR(256) NOT NULL,
    attempts        INT          NOT NULL DEFAULT 0,
    enqueued_at     TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    started_at      TIMESTAMP(6),
    finished_at     TIMESTAMP(6),
    error           TEXT,
    PRIMARY KEY (id),
    INDEX idx_compression_queue_status   (status),
    INDEX idx_compression_queue_priority (priority)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_STATE = """\
CREATE TABLE IF NOT EXISTS state (
    owner_id   VARCHAR(100) NOT NULL DEFAULT 'default',
    namespace  VARCHAR(100) NOT NULL DEFAULT 'default',
    `key`      VARCHAR(500) NOT NULL,
    value      LONGTEXT,
    updated    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    version    BIGINT       NOT NULL DEFAULT 1,
    deleted_at TIMESTAMP(6) NULL,
    UNIQUE KEY uq_state_owner_namespace_key (owner_id, namespace, `key`),
    INDEX idx_state_owner (owner_id),
    INDEX idx_state_namespace (namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MEMORY_VERSIONS = """\
CREATE TABLE IF NOT EXISTS memory_versions (
    id                VARCHAR(64)   NOT NULL,
    memory_id         VARCHAR(64)   NOT NULL,
    version_num       INT           NOT NULL,
    content           LONGTEXT      NOT NULL,
    category          VARCHAR(128),
    subcategory       VARCHAR(128),
    metadata          JSON,
    verbatim_content  LONGTEXT,
    owner_id          VARCHAR(256)  NOT NULL DEFAULT 'default',
    namespace         VARCHAR(256)  NOT NULL DEFAULT 'default',
    permission_mode   INT           NOT NULL DEFAULT 600,
    source_model      VARCHAR(256),
    source_provider   VARCHAR(256),
    source_session    VARCHAR(512),
    source_agent      VARCHAR(256),
    snapshot_at       TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    snapshot_by       VARCHAR(256),
    change_type       VARCHAR(40)   NOT NULL DEFAULT 'create',
    commit_hash       VARCHAR(128),
    parent_version_id VARCHAR(64),
    branch            VARCHAR(128)  NOT NULL DEFAULT 'main',
    merge_parents     JSON,
    deleted_at        TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_memory_versions_memory_version (memory_id, branch, version_num),
    INDEX idx_mv_memory_id (memory_id),
    INDEX idx_mv_memory_id_vnum (memory_id, version_num DESC),
    INDEX idx_mv_snapshot_at (snapshot_at),
    INDEX idx_mv_commit_hash (commit_hash),
    INDEX idx_mv_branch_head (memory_id, branch, version_num DESC),
    INDEX idx_mv_owner_namespace (owner_id, namespace),
    INDEX idx_mv_parent_version (parent_version_id),
    INDEX idx_mv_deleted (deleted_at),
    CONSTRAINT fk_mv_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MEMORY_BRANCHES = """\
CREATE TABLE IF NOT EXISTS memory_branches (
    memory_id       VARCHAR(64)  NOT NULL,
    name            VARCHAR(128) NOT NULL,
    head_version_id VARCHAR(64),
    created_by      VARCHAR(256),
    created_at      TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (memory_id, name),
    INDEX idx_memory_branches_memory (memory_id),
    INDEX idx_memory_branches_head (head_version_id),
    CONSTRAINT fk_memory_branches_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_branches_head
        FOREIGN KEY (head_version_id) REFERENCES memory_versions(id) ON DELETE SET NULL
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_INIT_DDLS = [
    _DDL_MEMORIES,
    _DDL_MEMORY_VERSIONS,
    _DDL_MEMORY_BRANCHES,
    _DDL_KG_TRIPLES,
    _DDL_COMPRESSION_QUEUE,
    _DDL_STATE,
]


# ── Pool factory ──────────────────────────────────────────────────────────────


async def create_mysql_pool(
    dsn: str,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    settings: Any = None,
) -> Any:
    """Create an aiomysql async connection pool for MNEMOS.

    ``dsn`` must be a ``mysql://user:pass@host:port/db`` URL.
    Pool sizing is driven by ``MNEMOS_MYSQL_POOL_MIN`` /
    ``MNEMOS_MYSQL_POOL_MAX`` env vars (or the ``min_size`` / ``max_size``
    keyword args, which take precedence).
    """
    try:
        import aiomysql
    except ImportError as exc:
        raise ImportError(
            "The MySQL persistence backend requires the 'aiomysql' package. "
            "Install it with: pip install 'mnemos-os[mysql]'"
        ) from exc

    kwargs = _parse_mysql_dsn(dsn)
    pool_min = min_size if min_size is not None else _env_int("MNEMOS_MYSQL_POOL_MIN", _DEFAULT_MYSQL_POOL_MIN)
    pool_max = max_size if max_size is not None else _env_int("MNEMOS_MYSQL_POOL_MAX", _DEFAULT_MYSQL_POOL_MAX)

    pool = await aiomysql.create_pool(
        minsize=pool_min,
        maxsize=pool_max,
        connect_timeout=_env_float("MNEMOS_MYSQL_CONNECT_TIMEOUT", 10.0),
        **kwargs,
    )
    return pool


# ── Transaction ───────────────────────────────────────────────────────────────


class _MysqlTransaction:
    """Backend-neutral transaction wrapping an aiomysql connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def conn(self) -> Any:
        return self._conn

    async def commit(self) -> None:
        if self._closed:
            return
        await self._conn.commit()
        self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._conn.rollback()
        self._closed = True


# ── Memory repository ─────────────────────────────────────────────────────────


class MysqlMemoryRepository(MemoryRepository):
    """MySQL 9.0+ implementation of the MNEMOS memory repository.

    Vector search uses ``VEC_DISTANCE_COSINE`` (MySQL 9.0) and requires
    a ``VECTOR(n)`` column on the ``memories`` table.  FTS uses MySQL's
    built-in ``FULLTEXT INDEX`` with ``MATCH … AGAINST … IN BOOLEAN MODE``.
    """

    _expected_embedding_dim: int | None = _DEFAULT_EMBEDDING_DIM

    def _require_dim(self, embedding: Sequence[float], op: str) -> None:
        expected = self._expected_embedding_dim
        if expected is None:
            return
        actual = len(embedding)
        if actual != expected:
            raise ValueError(
                f"MySQL embedding dim mismatch on {op}: got {actual}-D vector "
                f"but the configured MNEMOS_EMBEDDING_DIM is {expected}. The "
                f"embedding endpoint may have been switched to a different "
                f"model. Verify INFERENCE_EMBED_HOST / model selection and "
                f"either restart with the matching MNEMOS_EMBEDDING_DIM or "
                f"swap the embedding endpoint back to the model the DB was "
                f"sized for."
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
        created: Any,
        updated: Any,
    ) -> str:
        conn = tx.conn
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memories (
                        id, content, content_hash, category, subcategory, metadata,
                        quality_rating, verbatim_content, owner_id, namespace,
                        permission_mode, source_model, source_provider,
                        source_session, source_agent, created, updated
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        COALESCE(%s, NOW(6)), COALESCE(%s, NOW(6))
                    )
                    ON DUPLICATE KEY UPDATE
                        id = id
                    """,
                    (
                        memory_id,
                        content,
                        _content_hash(content),
                        category,
                        subcategory,
                        metadata_json,
                        quality_rating,
                        verbatim_content,
                        owner_id,
                        namespace,
                        permission_mode,
                        source_model,
                        source_provider,
                        source_session,
                        source_agent,
                        created,
                        updated,
                    ),
                )
                return "INSERT 0 1" if cursor.rowcount else "INSERT 0 0"
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, content, category, subcategory, metadata, quality_rating,
                       compressed_content, verbatim_content, owner_id, namespace,
                       permission_mode, source_model, source_provider, source_session,
                       source_agent, group_id, created, updated, archived_at, deleted_at
                  FROM memories
                 WHERE id = %s AND deleted_at IS NULL
                """,
                (memory_id,),
            )
            return await _fetchone_dict(cursor)

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        # MySQL schema has no version-snapshot trigger; suppression is implicit.
        return None

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = tx.conn
        async with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(memory_ids))
            await cursor.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(memory_ids),
            )
            return await _fetch_all_dicts(cursor)

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = tx.conn
        async with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(memory_ids))
            await cursor.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(memory_ids),
            )
            return await _fetch_all_dicts(cursor)

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        if not embedding:
            return
        self._require_dim(embedding, "upsert_memory_embedding")
        vec_literal = _validate_and_format_vector(embedding)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "UPDATE memories SET embedding = TO_VECTOR(%s) WHERE id = %s",
                (vec_literal, memory_id),
            )

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
    ) -> tuple[list[Row], int]:
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.deleted_at IS NULL"]
        params: list[Any] = []
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        if category is not None:
            where.append("m.category = %s")
            params.append(category)
        if subcategory is not None:
            where.append("m.subcategory = %s")
            params.append(subcategory)
        where_sql = " AND ".join(where)

        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT COUNT(*) AS cnt FROM memories m WHERE {where_sql}",
                params,
            )
            row = await cursor.fetchone()
            total = int(row[0]) if row else 0

        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                       m.quality_rating, m.compressed_content, m.verbatim_content,
                       m.owner_id, m.namespace, m.permission_mode, m.source_model,
                       m.source_provider, m.source_session, m.source_agent,
                       m.group_id, m.created, m.updated, m.archived_at,
                       m.recall_count, m.last_recalled_at
                  FROM memories m
                 WHERE {where_sql}
                 ORDER BY m.created DESC
                 LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = await _fetch_all_dicts(cursor)
        return rows, total

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.id = %s", "m.deleted_at IS NULL"]
        params: list[Any] = [memory_id]
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                       m.quality_rating, m.compressed_content, m.verbatim_content,
                       m.owner_id, m.namespace, m.permission_mode, m.source_model,
                       m.source_provider, m.source_session, m.source_agent,
                       m.group_id, m.created, m.updated, m.archived_at,
                       m.recall_count, m.last_recalled_at
                  FROM memories m
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            return await _fetchone_dict(cursor)

    _UPDATABLE_FIELDS = frozenset(
        {
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
        }
    )

    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        if not fields:
            return await self.get_memory(tx, memory_id, visibility=visibility)
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.id = %s", "m.deleted_at IS NULL"]
        params: list[Any] = [memory_id]
        if vis_clause:
            where.append(vis_clause)
            params += vis_params

        safe_fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_FIELDS}
        if not safe_fields:
            return await self.get_memory(tx, memory_id, visibility=visibility)
        set_cols = ", ".join(f"{col} = %s" for col in safe_fields)
        set_vals = list(safe_fields.values())

        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE memories m SET {set_cols}, updated = NOW(6) WHERE {' AND '.join(where)}",
                set_vals + params,
            )
            if not cursor.rowcount:
                return None
        return await self.get_memory(tx, memory_id, visibility=visibility)

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conn = tx.conn
        where = ["content_hash = %s", "deleted_at IS NULL", "archived_at IS NULL"]
        params: list[Any] = [content_hash]
        if cross_namespace:
            where.append("owner_id = %s")
            params.append(owner_id)
        else:
            where += ["owner_id = %s", "namespace = %s"]
            params += [owner_id, namespace]
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT id FROM memories WHERE {' AND '.join(where)} LIMIT 1",
                params,
            )
            return await _fetchone_dict(cursor)

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.id = %s", "m.deleted_at IS NULL", "m.archived_at IS NULL"]
        params: list[Any] = [memory_id]
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                UPDATE memories m
                   SET recall_count = recall_count + 1,
                       last_recalled_at = NOW(6)
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            if not cursor.rowcount:
                return None
        return await self.get_memory(tx, memory_id, visibility=visibility)

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
        row = await self.get_memory(tx, memory_id, visibility=visibility)
        if row is None:
            return None
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.id = %s", "m.deleted_at IS NULL"]
        params: list[Any] = [memory_id]
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE memories m SET deleted_at = NOW(6) WHERE {' AND '.join(where)}",
                params,
            )
        return row if cursor.rowcount else None

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
    ) -> list[Row]:
        if not embedding:
            return []
        self._require_dim(embedding, "semantic_search")
        vec_literal = _validate_and_format_vector(embedding)
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.deleted_at IS NULL", "m.embedding IS NOT NULL"]
        params: list[Any] = []
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                where.append(f"m.{col} = %s")
                params.append(val)

        # MySQL 9.0 VECTOR_DISTANCE returns 0 for identical vectors and grows
        # with dissimilarity. Keep the SQL rank/order expression as the bare
        # distance so the native vector index can serve top-K; recency boost is
        # applied in Python after over-fetching candidates.
        rank_expr = "VECTOR_DISTANCE(m.embedding, TO_VECTOR(%s), 'COSINE')"
        candidate_limit = max(limit, min(limit * 4, 200)) if boost_recency else limit

        # Bind the TO_VECTOR placeholder before the rest of the params.
        # ORDER BY uses the selected alias so the vector is bound once.
        vec_params = [vec_literal] + params + [candidate_limit]

        conn = tx.conn
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                           m.quality_rating, m.compressed_content, m.verbatim_content,
                           m.owner_id, m.namespace, m.permission_mode, m.source_model,
                           m.source_provider, m.source_session, m.source_agent,
                           m.group_id, m.created, m.updated, m.archived_at,
                           m.recall_count, m.last_recalled_at,
                           {rank_expr} AS rank_score
                      FROM memories m
                     WHERE {" AND ".join(where)}
                     ORDER BY rank_score ASC
                     LIMIT %s
                    """,
                    vec_params,
                )
                rows = await _fetch_all_dicts(cursor)
        except Exception as exc:
            if _is_vec_distance_unsupported(exc):
                # MySQL Community Edition lacks VEC_DISTANCE_COSINE; fall back to
                # Python-side cosine computation.
                return await self._python_cosine_search(
                    tx,
                    vec_literal=vec_literal,
                    where=where,
                    params=params,
                    limit=limit,
                    boost_recency=boost_recency,
                    recency_weight=recency_weight,
                )
            raise

        if boost_recency and rows:
            w = float(recency_weight)
            today = datetime.now(timezone.utc).date()
            for row in rows:
                rank = row.get("rank_score")
                if rank is None:
                    continue
                try:
                    rank_f = float(rank)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(rank_f):
                    continue
                upd_date = _recency_date(row)
                age_days = max(0, (today - upd_date).days)
                row["rank_score"] = rank_f - w * (1.0 / (1.0 + age_days))
            rows.sort(key=_rank_score_sort_key)
            rows = rows[:limit]

        return rows

    async def _python_cosine_search(
        self,
        tx: Transaction,
        *,
        vec_literal: str,
        where: list[str],
        params: list[Any],
        limit: int,
        boost_recency: bool,
        recency_weight: float,
    ) -> list[Row]:
        """Fallback semantic search using Python-side cosine when MySQL lacks
        built-in VEC_DISTANCE functions (Community Edition)."""
        import time as _time

        query_vec = json.loads(vec_literal)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                       m.quality_rating, m.compressed_content, m.verbatim_content,
                       m.owner_id, m.namespace, m.permission_mode, m.source_model,
                       m.source_provider, m.source_session, m.source_agent,
                       m.group_id, m.created, m.updated, m.archived_at,
                       m.recall_count, m.last_recalled_at,
                       FROM_VECTOR(m.embedding) AS embedding_json
                  FROM memories m
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            raw_rows = await _fetch_all_dicts(cursor)

        now_ts = _time.time()
        for row in raw_rows:
            emb_json = row.pop("embedding_json", None)
            try:
                emb = json.loads(emb_json) if emb_json else None
                dist = _cosine_distance_python(query_vec, emb) if emb else 1.0
            except (json.JSONDecodeError, ValueError, TypeError):
                dist = 1.0
            if boost_recency and row.get("updated") is not None:
                try:
                    updated = row["updated"]
                    age_days = (now_ts - updated.timestamp()) / 86400.0
                    dist -= recency_weight * (1.0 / (1.0 + age_days))
                except (AttributeError, OSError):
                    pass
            row["rank_score"] = dist

        raw_rows.sort(key=lambda r: r.get("rank_score", 1.0))
        return raw_rows[:limit]

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
    ) -> list[Row]:
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = [
            "m.deleted_at IS NULL",
            "MATCH (m.content) AGAINST (%s IN BOOLEAN MODE)",
        ]
        params: list[Any] = [query]
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if vis_clause:
            where.append(vis_clause)
            params += vis_params
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                where.append(f"m.{col} = %s")
                params.append(val)

        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                       m.quality_rating, m.compressed_content, m.verbatim_content,
                       m.owner_id, m.namespace, m.permission_mode, m.source_model,
                       m.source_provider, m.source_session, m.source_agent,
                       m.group_id, m.created, m.updated, m.archived_at,
                       m.recall_count, m.last_recalled_at,
                       MATCH (m.content) AGAINST (%s IN BOOLEAN MODE) AS rank_score
                  FROM memories m
                 WHERE {" AND ".join(where)}
                 ORDER BY rank_score DESC
                 LIMIT %s
                """,
                [query] + params + [limit],
            )
            return await _fetch_all_dicts(cursor)

    async def gather_stats(self, tx: Transaction) -> Any:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_memories,
                    SUM(CASE WHEN federation_source IS NULL THEN 1 ELSE 0 END) AS native_memories,
                    SUM(CASE WHEN federation_source IS NOT NULL THEN 1 ELSE 0 END) AS federated_memories,
                    AVG(quality_rating) AS avg_quality_rating
                FROM memories
                WHERE deleted_at IS NULL
                """
            )
            row = await _fetchone_dict(cursor)

        from mnemos.persistence.base import MemoryStatsRow

        return MemoryStatsRow(
            total_memories=int(row["total_memories"] or 0),
            native_memories=int(row["native_memories"] or 0),
            federated_memories=int(row["federated_memories"] or 0),
            avg_quality_rating=float(row["avg_quality_rating"]) if row.get("avg_quality_rating") is not None else None,
        )

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        from mnemos.core.lifecycle import _get_embedding

        embedding = await _get_embedding(query)
        if not embedding:
            return []

        from mnemos.core.security import is_root

        namespace = None if is_root(user) else user.namespace
        vis = VisibilityFilter.for_read(user, namespace=namespace)
        return await self.semantic_search(tx, embedding=embedding, limit=limit, visibility=vis)

    # --- unimplemented stubs (port forthcoming) ---

    fetch_memory_log = _stub_method("fetch_memory_log")
    fetch_diff_commit_pair = _stub_method("fetch_diff_commit_pair")
    fetch_checkout_commit = _stub_method("fetch_checkout_commit")
    fetch_memory_export = _stub_method("fetch_memory_export")
    fetch_referenced_memory_allowlist = _stub_method("fetch_referenced_memory_allowlist")
    assert_memory_readable = _stub_method("assert_memory_readable")
    fetch_duplicate_content_groups = _stub_method("fetch_duplicate_content_groups")
    consolidate_duplicate_memories = _stub_method("consolidate_duplicate_memories")
    find_duplicate_content_groups = _stub_method("find_duplicate_content_groups")


# ── KG, Version, Branch, Compression, Webhook, ConsultationAudit,
#    Federation, State — all stubbed; implementation follows M4 cadence.


class MysqlKGRepository(KGRepository):
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
        conditions: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if memory_ids:
            placeholders = ", ".join(["%s"] * len(memory_ids))
            if include_unattached:
                conditions.append(f"(memory_id IS NULL OR memory_id IN ({placeholders}))")
            else:
                conditions.append(f"memory_id IN ({placeholders})")
            params.extend(memory_ids)
        elif include_unattached:
            conditions.append("memory_id IS NULL")
        else:
            return []
        if effective_owner:
            conditions.append("owner_id = %s")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = %s")
            params.append(effective_ns)
        params.append(hard_limit + 1)

        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, subject, predicate, object, subject_type, object_type, "
                "valid_from, valid_until, memory_id, confidence, created, owner_id, namespace "
                f"FROM kg_triples WHERE {' AND '.join(conditions)} LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)

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
        conn = tx.conn
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO kg_triples (
                        id, subject, predicate, object,
                        subject_type, object_type,
                        valid_from, valid_until,
                        memory_id, confidence, created,
                        owner_id, namespace
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s,
                        COALESCE(%s, CURRENT_TIMESTAMP(6)), %s,
                        %s, COALESCE(%s, 1.0),
                        COALESCE(%s, CURRENT_TIMESTAMP(6)),
                        %s, COALESCE(%s, 'default')
                    )
                    ON DUPLICATE KEY UPDATE
                        id = id
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
                return "INSERT 0 1" if cursor.rowcount else "INSERT 0 0"
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT subject, predicate, object, subject_type, object_type, memory_id,
                       confidence, owner_id, namespace, valid_from, valid_until, created
                  FROM kg_triples
                 WHERE id = %s AND deleted_at IS NULL
                """,
                (triple_id,),
            )
            return await _fetchone_dict(cursor)

    async def fetch_kg_triple(
        self,
        tx: Transaction,
        *,
        subject: str,
        predicate: str,
        obj: str,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> Row | None:
        conditions = ["subject = %s", "predicate = %s", "object = %s", "deleted_at IS NULL"]
        params: list[Any] = [subject, predicate, obj]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT * FROM kg_triples WHERE {' AND '.join(conditions)} "
                "ORDER BY valid_from ASC, created ASC LIMIT 1",
                params,
            )
            return await _fetchone_dict(cursor)

    async def fetch_kg_triples_for_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        conditions = ["memory_id = %s", "deleted_at IS NULL"]
        params: list[Any] = [memory_id]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        params.append(limit)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT * FROM kg_triples WHERE {' AND '.join(conditions)} "
                "ORDER BY valid_from ASC, created ASC LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def delete_kg_triple(
        self,
        tx: Transaction,
        triple_id: str,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> bool:
        conditions = ["id = %s", "deleted_at IS NULL"]
        params: list[Any] = [triple_id]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE kg_triples SET deleted_at = CURRENT_TIMESTAMP(6) WHERE {' AND '.join(conditions)}",
                params,
            )
            return int(cursor.rowcount or 0) > 0

    async def update_kg_triple(
        self,
        tx: Transaction,
        triple_id: str,
        *,
        fields: dict[str, Any],
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> Row | None:
        allowed = {
            "subject",
            "predicate",
            "object",
            "subject_type",
            "object_type",
            "valid_from",
            "valid_until",
            "memory_id",
            "confidence",
        }
        safe_fields = {key: value for key, value in fields.items() if key in allowed}
        if not safe_fields:
            return await self.fetch_kg_triple_by_id(tx, triple_id)

        set_sql = ", ".join(f"{column} = %s" for column in safe_fields)
        conditions = ["id = %s", "deleted_at IS NULL"]
        params: list[Any] = list(safe_fields.values()) + [triple_id]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)

        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE kg_triples SET {set_sql} WHERE {' AND '.join(conditions)}",
                params,
            )
            if not cursor.rowcount:
                return None
        return await self.fetch_kg_triple_by_id(tx, triple_id)

    async def list_kg_triples(
        self,
        tx: Transaction,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Row]:
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        if memory_id is not None:
            conditions.append("memory_id = %s")
            params.append(memory_id)
        params.extend([limit, offset])
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT *
                  FROM kg_triples
                 WHERE {" AND ".join(conditions)}
                 ORDER BY valid_from ASC, created ASC
                 LIMIT %s OFFSET %s
                """,
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def assert_memory_ownership_for_kg(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        owner_id: str,
        namespace: str | None = None,
    ) -> Row | None:
        conditions = ["id = %s", "owner_id = %s", "deleted_at IS NULL"]
        params: list[Any] = [memory_id, owner_id]
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT id, owner_id, namespace FROM memories WHERE {' AND '.join(conditions)}",
                params,
            )
            return await _fetchone_dict(cursor)

    async def fetch_kg_triple_timeline(
        self,
        tx: Transaction,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        obj: str | None = None,
        owner_id: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if subject is not None:
            conditions.append("subject = %s")
            params.append(subject)
        if predicate is not None:
            conditions.append("predicate = %s")
            params.append(predicate)
        if obj is not None:
            conditions.append("object = %s")
            params.append(obj)
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        params.append(limit)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT * FROM kg_triples WHERE {' AND '.join(conditions)} "
                "ORDER BY valid_from ASC, created ASC LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)

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
        conditions = [
            "(LOWER(subject) LIKE LOWER(%s) OR LOWER(predicate) LIKE LOWER(%s) OR LOWER(object) LIKE LOWER(%s))",
            "deleted_at IS NULL",
        ]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        params.append(limit)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT * FROM kg_triples WHERE {' AND '.join(conditions)} "
                "ORDER BY valid_from ASC, created ASC LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)


class MysqlVersionRepository(VersionRepository):
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        placeholders = ", ".join(["%s"] * len(memory_ids))
        conditions.append(f"memory_id IN ({placeholders})")
        params.extend(memory_ids)
        if effective_owner:
            conditions.append("owner_id = %s")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = %s")
            params.append(effective_ns)
        params.append(hard_limit + 1)

        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, memory_id, version_num, content, category, "
                "subcategory, metadata, verbatim_content, owner_id, "
                "namespace, permission_mode, source_model, source_provider, "
                "source_session, source_agent, snapshot_at, snapshot_by, "
                "change_type, commit_hash, parent_version_id, branch, merge_parents "
                f"FROM memory_versions WHERE {' AND '.join(conditions)} "
                "ORDER BY memory_id ASC, branch ASC, version_num ASC "
                "LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        if not version_ids:
            return []
        placeholders = ", ".join(["%s"] * len(version_ids))
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, memory_id, owner_id, namespace
                  FROM memory_versions
                 WHERE id IN ({placeholders})
                   AND deleted_at IS NULL
                """,
                list(version_ids),
            )
            return await _fetch_all_dicts(cursor)

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
        conn = tx.conn
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memory_versions (
                        id, memory_id, version_num, content,
                        category, subcategory, metadata, verbatim_content,
                        owner_id, namespace, permission_mode,
                        source_model, source_provider, source_session, source_agent,
                        snapshot_at, snapshot_by, change_type,
                        commit_hash, parent_version_id, branch, merge_parents
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, COALESCE(%s, 'default'), COALESCE(%s, 600),
                        %s, %s, %s, %s,
                        COALESCE(%s, CURRENT_TIMESTAMP(6)), %s, COALESCE(%s, 'create'),
                        %s, %s, COALESCE(%s, 'main'), %s
                    )
                    ON DUPLICATE KEY UPDATE
                        id = id
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
                        json.dumps(merge_parents if merge_parents is not None else []),
                    ),
                )
                return "INSERT 0 1" if cursor.rowcount else "INSERT 0 0"
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT memory_id, owner_id, namespace, version_num, content, commit_hash,
                       parent_version_id, branch, merge_parents, category, subcategory,
                       metadata, verbatim_content, permission_mode, source_model,
                       source_provider, source_session, source_agent, snapshot_at,
                       snapshot_by, change_type
                  FROM memory_versions
                 WHERE id = %s
                   AND deleted_at IS NULL
                """,
                (version_id,),
            )
            return await _fetchone_dict(cursor)


class MysqlBranchRepository(BranchRepository):
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: Any,
    ) -> dict[str, Any]:
        conn = tx.conn
        async with conn.cursor() as cursor:
            if self._is_root(user):
                await cursor.execute(
                    "SELECT 1 FROM memories WHERE id = %s",
                    (memory_id,),
                )
            else:
                await cursor.execute(
                    "SELECT 1 FROM memories WHERE id = %s AND owner_id = %s AND namespace = %s",
                    (memory_id, user.user_id, user.namespace),
                )
            live = await cursor.fetchone()
            if not live:
                return {"success": False, "error": f"Memory {memory_id} not found"}

            if from_commit:
                start = await self._fetch_branch_start_by_commit(cursor, memory_id, from_commit, user)
                if not start:
                    return {"success": False, "error": "Commit not found"}
            else:
                start = await self._fetch_main_branch_start(cursor, memory_id, user)
                if not start:
                    return {"success": False, "error": "main branch not found"}

            await cursor.execute(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE memory_id = memory_id
                """,
                (memory_id, name, start["id"], user.user_id),
            )
            existing = await self._fetch_existing_branch(cursor, memory_id, name, user)
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

    @staticmethod
    def _is_root(user: Any) -> bool:
        return getattr(user, "role", None) == "root"

    async def _fetch_branch_start_by_commit(
        self,
        cursor: Any,
        memory_id: str,
        from_commit: str,
        user: Any,
    ) -> Row | None:
        if self._is_root(user):
            await cursor.execute(
                "SELECT id, commit_hash FROM memory_versions WHERE memory_id = %s AND commit_hash = %s",
                (memory_id, from_commit),
            )
        else:
            await cursor.execute(
                """
                SELECT id, commit_hash
                  FROM memory_versions
                 WHERE memory_id = %s
                   AND commit_hash = %s
                   AND (owner_id = %s OR MOD(permission_mode, 10) >= 4)
                   AND namespace = %s
                """,
                (memory_id, from_commit, user.user_id, user.namespace),
            )
        return await _fetchone_dict(cursor)

    async def _fetch_main_branch_start(self, cursor: Any, memory_id: str, user: Any) -> Row | None:
        if self._is_root(user):
            await cursor.execute(
                """
                SELECT mv.id, mv.commit_hash
                  FROM memory_versions mv
                  INNER JOIN memory_branches mb
                          ON mb.memory_id = mv.memory_id
                         AND mb.head_version_id = mv.id
                 WHERE mv.memory_id = %s
                   AND mb.name = 'main'
                """,
                (memory_id,),
            )
        else:
            await cursor.execute(
                """
                SELECT mv.id, mv.commit_hash
                  FROM memory_versions mv
                  INNER JOIN memory_branches mb
                          ON mb.memory_id = mv.memory_id
                         AND mb.head_version_id = mv.id
                 WHERE mv.memory_id = %s
                   AND mb.name = 'main'
                   AND (mv.owner_id = %s OR MOD(mv.permission_mode, 10) >= 4)
                   AND mv.namespace = %s
                """,
                (memory_id, user.user_id, user.namespace),
            )
        return await _fetchone_dict(cursor)

    async def _fetch_existing_branch(
        self,
        cursor: Any,
        memory_id: str,
        name: str,
        user: Any,
    ) -> Row | None:
        if self._is_root(user):
            await cursor.execute(
                """
                SELECT mb.head_version_id, mv.commit_hash
                  FROM memory_branches mb
                  INNER JOIN memory_versions mv
                          ON mv.id = mb.head_version_id
                         AND mv.memory_id = mb.memory_id
                 WHERE mb.memory_id = %s
                   AND mb.name = %s
                """,
                (memory_id, name),
            )
        else:
            await cursor.execute(
                """
                SELECT mb.head_version_id, mv.commit_hash
                  FROM memory_branches mb
                  INNER JOIN memory_versions mv
                          ON mv.id = mb.head_version_id
                         AND mv.memory_id = mb.memory_id
                         AND (mv.owner_id = %s OR MOD(mv.permission_mode, 10) >= 4)
                         AND mv.namespace = %s
                 WHERE mb.memory_id = %s
                   AND mb.name = %s
                """,
                (user.user_id, user.namespace, memory_id, name),
            )
        return await _fetchone_dict(cursor)

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return
        placeholders = ", ".join(["%s"] * len(memory_ids))
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"DELETE FROM memory_branches WHERE memory_id IN ({placeholders})",
                list(memory_ids),
            )

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        if not memory_ids:
            return []
        params: list[Any] = list(memory_ids)
        conditions = [f"memory_id IN ({', '.join(['%s'] * len(memory_ids))})"]
        if authorized_version_uuids is not None:
            if not authorized_version_uuids:
                return []
            conditions.append(f"id IN ({', '.join(['%s'] * len(authorized_version_uuids))})")
            params.extend(authorized_version_uuids)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT memory_id, branch, id AS head_version_id "
                "FROM ("
                "  SELECT memory_id, branch, id, version_num, "
                "         ROW_NUMBER() OVER (PARTITION BY memory_id, branch ORDER BY version_num DESC) AS rn "
                "  FROM memory_versions "
                f"  WHERE {' AND '.join(conditions)}"
                ") ranked WHERE rn = 1",
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES (%s, %s, %s, NULL)
                ON DUPLICATE KEY UPDATE head_version_id = VALUES(head_version_id)
                """,
                (memory_id, branch, head_version_id),
            )


class MysqlCompressionRepository(CompressionRepository):
    fetch_compressed_variants_for_export = _stub_method("fetch_compressed_variants_for_export")
    compression_candidate_exists = _stub_method("compression_candidate_exists")
    insert_compressed_variant = _stub_method("insert_compressed_variant")
    fetch_compressed_variant_by_memory_id = _stub_method("fetch_compressed_variant_by_memory_id")
    gather_stats = _stub_method("gather_stats")
    insert_compression = _stub_method("insert_compression")
    fetch_compressions = _stub_method("fetch_compressions")
    fetch_compression_by_id = _stub_method("fetch_compression_by_id")
    delete_compression = _stub_method("delete_compression")
    update_compression_review = _stub_method("update_compression_review")
    fetch_memories_for_compression = _stub_method("fetch_memories_for_compression")
    fetch_manifests = _stub_method("fetch_manifests")
    fetch_manifest_by_id = _stub_method("fetch_manifest_by_id")
    insert_manifest = _stub_method("insert_manifest")


class MysqlCompressionQueueRepository(CompressionQueueRepository):
    """MySQL implementation of the v3.1 compression work queue."""

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
        conn = tx.conn
        placeholders = ", ".join(["%s"] * len(memory_ids))
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, owner_id
                  FROM memories
                 WHERE id IN ({placeholders}) AND deleted_at IS NULL
                """,
                list(memory_ids),
            )
            known = await _fetch_all_dicts(cursor)

        owner_by_id = {row["id"]: row["owner_id"] for row in known}
        enqueued: list[str] = []
        async with conn.cursor() as cursor:
            for mid in memory_ids:
                if mid not in owner_by_id:
                    continue
                await cursor.execute(
                    """
                    INSERT INTO memory_compression_queue
                        (memory_id, owner_id, reason, priority, scoring_profile)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
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
        if limit <= 0:
            return 0
        where_parts: list[str] = ["m.deleted_at IS NULL"]
        params: list[Any] = [reason, priority, scoring_profile]
        if only_uncompressed:
            where_parts.append("NOT EXISTS (SELECT 1 FROM memory_compressed_variants v WHERE v.memory_id = m.id)")
        if category is not None:
            where_parts.append("m.category = %s")
            params.append(category)
        params.append(limit)

        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                INSERT INTO memory_compression_queue
                    (memory_id, owner_id, reason, priority, scoring_profile)
                SELECT m.id, m.owner_id, %s, %s, %s
                  FROM memories m
                 WHERE {" AND ".join(where_parts)}
                 ORDER BY LENGTH(m.content) DESC
                 LIMIT %s
                """,
                params,
            )
            return int(cursor.rowcount or 0)

    async def dequeue_compression(
        self,
        tx: Transaction,
        *,
        limit: int,
    ) -> list[Row]:
        if limit <= 0:
            return []
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id
                  FROM memory_compression_queue
                 WHERE status = 'pending'
                 ORDER BY priority DESC, enqueued_at
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (int(limit),),
            )
            locked = await cursor.fetchall()
            queue_ids = [row[0] for row in locked]
            if not queue_ids:
                return []

            placeholders = ", ".join(["%s"] * len(queue_ids))
            await cursor.execute(
                f"""
                UPDATE memory_compression_queue
                   SET status = 'running',
                       started_at = NOW(6),
                       attempts = attempts + 1
                 WHERE id IN ({placeholders})
                """,
                queue_ids,
            )
            await cursor.execute(
                f"""
                SELECT id, memory_id, owner_id, reason, scoring_profile, attempts
                  FROM memory_compression_queue
                 WHERE id IN ({placeholders})
                """,
                queue_ids,
            )
            rows = await _fetch_all_dicts(cursor)

        by_id = {str(row["id"]): row for row in rows}
        out: list[Row] = []
        for queue_id in queue_ids:
            row = by_id.get(str(queue_id))
            if row is not None:
                row["id"] = str(row["id"])
                out.append(row)
        return out

    async def mark_compression_done(
        self,
        tx: Transaction,
        *,
        queue_id: str,
    ) -> None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE memory_compression_queue
                   SET status = 'done',
                       finished_at = NOW(6),
                       error = NULL
                 WHERE id = %s
                """,
                (queue_id,),
            )

    async def mark_compression_failed(
        self,
        tx: Transaction,
        *,
        queue_id: str,
        error: str,
    ) -> None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE memory_compression_queue
                   SET status = 'failed',
                       finished_at = NOW(6),
                       error = %s
                 WHERE id = %s
                """,
                (error, queue_id),
            )

    async def sweep_stale_compression(
        self,
        tx: Transaction,
        *,
        stale_threshold_secs: int,
        max_attempts: int,
    ) -> int:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, attempts, error
                  FROM memory_compression_queue
                 WHERE status = 'running'
                   AND (started_at IS NULL
                        OR started_at < DATE_SUB(NOW(6), INTERVAL %s SECOND))
                 FOR UPDATE SKIP LOCKED
                """,
                (int(stale_threshold_secs),),
            )
            stale_rows = await _fetch_all_dicts(cursor)
            for row in stale_rows:
                attempts = int(row["attempts"] or 0)
                error = row.get("error")
                terminalize = (
                    attempts >= max_attempts and error is not None and not str(error).startswith("infra_retry:")
                )
                if terminalize:
                    await cursor.execute(
                        """
                        UPDATE memory_compression_queue
                           SET status = 'failed',
                               finished_at = NOW(6),
                               error = %s
                         WHERE id = %s
                        """,
                        (
                            f"stranded_running: exceeded stale threshold after {attempts} attempts",
                            row["id"],
                        ),
                    )
                elif attempts >= max_attempts:
                    await cursor.execute(
                        """
                        UPDATE memory_compression_queue
                           SET status = 'pending',
                               started_at = NULL,
                               finished_at = NULL,
                               attempts = %s,
                               error = 'infra_retry: stale-recovered without content-failure breadcrumb'
                         WHERE id = %s
                        """,
                        (max(attempts - 1, 0), row["id"]),
                    )
                else:
                    await cursor.execute(
                        """
                        UPDATE memory_compression_queue
                           SET status = 'pending',
                               started_at = NULL,
                               finished_at = NULL,
                               error = NULL
                         WHERE id = %s
                        """,
                        (row["id"],),
                    )
        return len(stale_rows)


class MysqlWebhookRepository(WebhookRepository):
    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        raise NotImplementedError(_MYSQL_WEBHOOKS_UNSUPPORTED)


class MysqlConsultationAuditRepository(ConsultationAuditRepository):
    fetch_recommended_model = _stub_method("fetch_recommended_model")
    fetch_model_recommendation = _stub_method("fetch_model_recommendation")
    lookup_provider_for_model = _stub_method("lookup_provider_for_model")
    fetch_available_models = _stub_method("fetch_available_models")
    fetch_model_provider = _stub_method("fetch_model_provider")
    insert_consultation_audit = _stub_method("insert_consultation_audit")
    fetch_consultation_audits = _stub_method("fetch_consultation_audits")
    fetch_consultation_audit = _stub_method("fetch_consultation_audit")
    fetch_consultation_by_id = _stub_method("fetch_consultation_by_id")
    fetch_consultations = _stub_method("fetch_consultations")


class MysqlFederationRepository(FederationRepository):
    fetch_memory_page = _stub_method("fetch_memory_page")
    create_peer = _stub_method("create_peer")
    list_peers = _stub_method("list_peers")
    get_peer = _stub_method("get_peer")
    update_peer = _stub_method("update_peer")
    upsert_peer = _stub_method("upsert_peer")
    delete_peer = _stub_method("delete_peer")
    fetch_sync_log = _stub_method("fetch_sync_log")
    feed_query = _stub_method("feed_query")
    get_feed_memory = _stub_method("get_feed_memory")
    get_sync_peer = _stub_method("get_sync_peer")
    update_peer_schema_check = _stub_method("update_peer_schema_check")
    record_schema_abort = _stub_method("record_schema_abort")
    create_sync_log = _stub_method("create_sync_log")
    finish_sync_log = _stub_method("finish_sync_log")
    record_sync_error = _stub_method("record_sync_error")
    record_sync_success = _stub_method("record_sync_success")
    list_due_peers = _stub_method("list_due_peers")
    fetch_federated_memory_marker = _stub_method("fetch_federated_memory_marker")
    insert_federated_memory = _stub_method("insert_federated_memory")
    update_federated_memory_if_newer = _stub_method("update_federated_memory_if_newer")
    apply_consolidation_tombstone = _stub_method("apply_consolidation_tombstone")
    delete_federated_memory = _stub_method("delete_federated_memory")
    upsert_federated_memory = _stub_method("upsert_federated_memory")
    fetch_federation_peers = _stub_method("fetch_federation_peers")
    upsert_federation_peer = _stub_method("upsert_federation_peer")
    delete_federation_peer = _stub_method("delete_federation_peer")
    fetch_local_memories_for_push = _stub_method("fetch_local_memories_for_push")
    mark_memories_pushed = _stub_method("mark_memories_pushed")


class MysqlStateRepository(StateRepository):
    async def get(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> Row | None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT `key`, value, updated, version, owner_id, namespace
                  FROM state
                 WHERE owner_id = %s
                   AND namespace = %s
                   AND `key` = %s
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace, key),
            )
            return await _fetchone_dict(cursor)

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
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO state (owner_id, namespace, `key`, value, updated)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    value = IF(deleted_at IS NULL, %s, value),
                    updated = IF(deleted_at IS NULL, CURRENT_TIMESTAMP(6), updated),
                    version = IF(deleted_at IS NULL, version + 1, version)
                """,
                (owner_id, namespace, key, value, value),
            )
        return await self.get(tx, key, owner_id=owner_id, namespace=namespace)

    async def delete(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> bool:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE state
                   SET deleted_at = CURRENT_TIMESTAMP(6)
                 WHERE owner_id = %s
                   AND namespace = %s
                   AND `key` = %s
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace, key),
            )
            return int(cursor.rowcount or 0) > 0

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        conn = tx.conn
        params: list[Any] = [owner_id, namespace]
        sql = """
            SELECT `key`, updated, version, owner_id, namespace
              FROM state
             WHERE owner_id = %s
               AND namespace = %s
               AND deleted_at IS NULL
             ORDER BY `key`
        """
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        async with conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetch_all_dicts(cursor)

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE state
                   SET deleted_at = CURRENT_TIMESTAMP(6)
                 WHERE owner_id = %s
                   AND namespace = %s
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace),
            )
            return int(cursor.rowcount or 0)

    get_state = get
    set_state = set
    delete_state = delete
    list_state_keys = list_namespace
    get_state_value = get
    set_state_value = set
    delete_state_value = delete
    list_state_namespace = list_namespace
    delete_state_namespace = delete_namespace


# ── Backend facade ────────────────────────────────────────────────────────────


class MysqlBackend:  # P14: PersistenceBackend is now a Union type alias; align with SqliteBackend/OracleBackend/Db2Backend/PostgresBackend bare-class pattern
    """MySQL 9.0+ persistence facade backed by an aiomysql connection pool.

    Core memory, FTS, VECTOR search, and state key-value surfaces are
    implemented.
    All other repository surfaces (KG triples, versioning, compression,
    federation) are stubbed - ``NotImplementedError`` is raised at call time.
    Webhooks are currently unsupported; callers should use ``supports_webhooks``
    before dispatching.

    The pool is managed externally (via ``create_mysql_pool``); callers
    must call ``await backend.close()`` at shutdown to drain the pool.
    """

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    supports_mysql_vector = True  # MySQL 9.0 native VECTOR
    supports_webhooks = False
    _supports_core_persistence = True

    def __init__(self, pool: Any, settings: Any) -> None:
        self._pool = pool
        self._settings = settings
        self._closed = False
        self._memories_repo = MysqlMemoryRepository()
        try:
            self._memories_repo._expected_embedding_dim = int(
                getattr(settings.database, "embedding_dim", _DEFAULT_EMBEDDING_DIM)
            )
        except (AttributeError, TypeError, ValueError):
            self._memories_repo._expected_embedding_dim = _DEFAULT_EMBEDDING_DIM
        self._kg_triples_repo = MysqlKGRepository()
        self._memory_versions_repo = MysqlVersionRepository()
        self._memory_branches_repo = MysqlBranchRepository()
        self._compression_repo = MysqlCompressionRepository()
        self._compression_queue_repo = MysqlCompressionQueueRepository()
        self._consultations_audit_repo = MysqlConsultationAuditRepository()
        self._federation_repo = MysqlFederationRepository()
        self._state_kv_repo = MysqlStateRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def capabilities(self) -> set[str]:
        return {CORE_CAPABILITY, STATE_CAPABILITY}

    @property
    def capability_details(self) -> set[str]:
        return {*MYSQL_CAPABILITY_DETAILS, KG_CAPABILITY, STATE_DETAIL_CAPABILITY}

    async def record_usage_ledger(self, tx: Transaction, record: Any) -> Any:
        raise NotImplementedError("mysql: usage_ledger is not yet implemented")

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        raise NotImplementedError("mysql: category decay is not yet implemented")

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        raise NotImplementedError("mysql: category decay is not yet implemented")

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
        raise NotImplementedError("mysql: journal persistence is not yet implemented")

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
        raise NotImplementedError("mysql: journal persistence is not yet implemented")

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        raise NotImplementedError("mysql: journal persistence is not yet implemented")

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        async with self._pool.acquire() as conn:
            await conn.begin()
            tx = _MysqlTransaction(conn)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    @property
    def memories(self) -> MemoryRepository:
        return self._memories_repo

    @property
    def kg_triples(self) -> KGRepository:
        return self._kg_triples_repo

    @property
    def memory_versions(self) -> VersionRepository:
        return self._memory_versions_repo

    @property
    def memory_branches(self) -> BranchRepository:
        return self._memory_branches_repo

    @property
    def compression(self) -> CompressionRepository:
        return self._compression_repo

    @property
    def compression_queue(self) -> CompressionQueueRepository:
        return self._compression_queue_repo

    @property
    def webhooks(self) -> WebhookRepository:
        return MysqlWebhookRepository()

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit_repo

    @property
    def federation(self) -> FederationRepository:
        return self._federation_repo

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv_repo

    async def open(self) -> None:
        """Validate pool connectivity and apply UTC + init DDL.

        Runs ``SET time_zone = '+00:00'`` and creates the inline schema tables
        if they do not exist (idempotent).
        """
        if self._closed or self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SET time_zone = '+00:00'")
                    await cursor.execute("SELECT 1")
                    for ddl in _INIT_DDLS:
                        await cursor.execute(ddl)
                await conn.commit()
        except Exception as exc:
            _LOG.warning(
                "MysqlBackend.open probe failed (%s); backend remains open but first acquire() may also fail.",
                exc,
            )

    async def close(self) -> None:
        if self._closed:
            return
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
        self._closed = True

    async def ping(self) -> bool:
        if self._closed or self._pool is None:
            return False
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")
                    await cursor.fetchone()
            return True
        except Exception:
            return False


__all__ = [
    "MysqlBackend",
    "MysqlBranchRepository",
    "MysqlCompressionQueueRepository",
    "MysqlCompressionRepository",
    "MysqlConsultationAuditRepository",
    "MysqlFederationRepository",
    "MysqlKGRepository",
    "MysqlMemoryRepository",
    "MysqlStateRepository",
    "MysqlVersionRepository",
    "MysqlWebhookRepository",
    "create_mysql_pool",
]
