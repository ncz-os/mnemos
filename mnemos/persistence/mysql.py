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
KG triples, versioning, branches, compression, federation, state, and audit
surfaces are implemented in MySQL dialect. Webhooks are explicitly declared
unsupported and gated before callers can reach the outbox methods.

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
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.core.auth_context import UserContext
from mnemos.core.config import embedding_dim_env, runtime_env_value_stripped
from mnemos.persistence.base import (
    AUDIT_CAPABILITY,
    AUDIT_DETAIL_CAPABILITY,
    AuditChainRepository,
    BranchRepository,
    CompressionStatsRow,
    COMPRESSION_QUEUE_CAPABILITY,
    CompressionQueueRepository,
    CompressionRepository,
    CORE_CAPABILITY,
    FEDERATION_CAPABILITY,
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


def _json_array_text(value: Sequence[Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value))


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


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


async def _ensure_mysql_columns(conn: Any, table: str, definitions: dict[str, str]) -> None:
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT COLUMN_NAME
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = %s
            """,
            (table,),
        )
        existing = {str(row[0]).lower() for row in await cursor.fetchall()}
        for column, definition in definitions.items():
            if column.lower() not in existing:
                await cursor.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


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
    federation_remote_updated DATETIME(6),
    consolidated_into VARCHAR(64),
    consolidated_at   DATETIME(6),
    federation_last_pushed_at DATETIME(6),
    federation_push_peer VARCHAR(512),
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
    INDEX idx_memories_federation_remote (federation_source, federation_remote_updated),
    INDEX idx_memories_push (federation_source, federation_last_pushed_at),
    FULLTEXT INDEX idx_memories_ft (content)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_FEDERATION_PEERS = """\
CREATE TABLE IF NOT EXISTS federation_peers (
    id                   VARCHAR(64)  NOT NULL,
    name                 VARCHAR(256),
    base_url             TEXT,
    auth_token           TEXT,
    api_key              TEXT,
    namespace_filter     JSON,
    category_filter      JSON,
    enabled              BOOLEAN      NOT NULL DEFAULT TRUE,
    sync_interval_secs   INT          NOT NULL DEFAULT 300,
    last_sync_at         TIMESTAMP(6) NULL,
    last_sync_cursor     TEXT,
    cursor_updated       TEXT,
    last_error           TEXT,
    last_error_at        TIMESTAMP(6) NULL,
    total_pulled         INT          NOT NULL DEFAULT 0,
    compat_mode          VARCHAR(32)  NOT NULL DEFAULT 'strict',
    peer_mnemos_version  VARCHAR(128),
    last_schema_check_at TIMESTAMP(6) NULL,
    copy_embeddings      BOOLEAN      NOT NULL DEFAULT FALSE,
    created              TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated              TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_federation_peers_name (name),
    INDEX idx_federation_peers_enabled (enabled, last_sync_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_FEDERATION_SYNC_LOG = """\
CREATE TABLE IF NOT EXISTS federation_sync_log (
    id                VARCHAR(64)  NOT NULL,
    peer_id           VARCHAR(64)  NOT NULL,
    direction         VARCHAR(16)  NOT NULL DEFAULT 'pull',
    status            VARCHAR(32)  NOT NULL DEFAULT 'started',
    started_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at       TIMESTAMP(6) NULL,
    memories_pulled   INT          NOT NULL DEFAULT 0,
    memories_new      INT          NOT NULL DEFAULT 0,
    memories_updated  INT          NOT NULL DEFAULT 0,
    records_seen      INT          NOT NULL DEFAULT 0,
    records_written   INT          NOT NULL DEFAULT 0,
    error             TEXT,
    cursor_before     TEXT,
    cursor_after      TEXT,
    PRIMARY KEY (id),
    INDEX idx_federation_sync_log_peer_started (peer_id, started_at),
    CONSTRAINT fk_federation_sync_log_peer
        FOREIGN KEY (peer_id) REFERENCES federation_peers(id) ON DELETE CASCADE
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

_DDL_COMPRESSION_CANDIDATES = """\
CREATE TABLE IF NOT EXISTS memory_compression_candidates (
    id                  VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    memory_id           VARCHAR(64)  NOT NULL,
    owner_id            VARCHAR(256) NOT NULL DEFAULT 'default',
    contest_id          VARCHAR(64),
    engine_id           VARCHAR(100) NOT NULL,
    engine_version      VARCHAR(50),
    compressed_content  LONGTEXT,
    original_tokens     INT,
    compressed_tokens   INT,
    candidate_content   LONGTEXT,
    candidate_tokens    INT,
    compression_ratio   DOUBLE,
    quality_score       DOUBLE,
    speed_factor        DOUBLE,
    composite_score     DOUBLE,
    scoring_profile     VARCHAR(50)  NOT NULL DEFAULT 'balanced',
    elapsed_ms          INT,
    judge_model         VARCHAR(200),
    gpu_used            BOOLEAN      NOT NULL DEFAULT FALSE,
    is_winner           BOOLEAN      NOT NULL DEFAULT FALSE,
    reject_reason       TEXT,
    manifest            JSON,
    created             TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_mcc_memory (memory_id),
    INDEX idx_mcc_contest (contest_id),
    INDEX idx_mcc_memory_winner (memory_id, is_winner),
    INDEX idx_mcc_owner (owner_id),
    INDEX idx_mcc_engine (engine_id),
    CONSTRAINT fk_mcc_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_COMPRESSED_VARIANTS = """\
CREATE TABLE IF NOT EXISTS memory_compressed_variants (
    memory_id            VARCHAR(64)  NOT NULL,
    owner_id             VARCHAR(256) NOT NULL DEFAULT 'default',
    winner_candidate_id  VARCHAR(64),
    engine_id            VARCHAR(100) NOT NULL,
    engine_version       VARCHAR(50),
    compressed_content   LONGTEXT,
    compressed_tokens    INT,
    compression_ratio    DOUBLE,
    quality_score        DOUBLE,
    composite_score      DOUBLE,
    scoring_profile      VARCHAR(50)  NOT NULL DEFAULT 'balanced',
    judge_model          VARCHAR(200),
    selected_at          TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (memory_id),
    INDEX idx_mcv_owner (owner_id),
    INDEX idx_mcv_engine (engine_id),
    CONSTRAINT fk_mcv_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    CONSTRAINT fk_mcv_candidate
        FOREIGN KEY (winner_candidate_id) REFERENCES memory_compression_candidates(id) ON DELETE SET NULL
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

_DDL_MODEL_REGISTRY = """\
CREATE TABLE IF NOT EXISTS model_registry (
    id                    VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    provider              VARCHAR(50)  NOT NULL,
    model_id              VARCHAR(512) NOT NULL,
    display_name          TEXT,
    family                TEXT,
    context_window        INT,
    max_output_tokens     INT,
    capabilities          JSON         NOT NULL DEFAULT (JSON_ARRAY()),
    input_cost_per_mtok   DECIMAL(12,6) DEFAULT 0,
    output_cost_per_mtok  DECIMAL(12,6) DEFAULT 0,
    cache_read_per_mtok   DECIMAL(12,6) DEFAULT 0,
    cache_write_per_mtok  DECIMAL(12,6) DEFAULT 0,
    available             BOOLEAN      NOT NULL DEFAULT TRUE,
    deprecated            BOOLEAN      NOT NULL DEFAULT FALSE,
    arena_score           DECIMAL(8,2),
    arena_rank            INT,
    graeae_weight         DECIMAL(5,4),
    first_seen            TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen             TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_synced           TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    raw                   JSON         NOT NULL DEFAULT (JSON_OBJECT()),
    PRIMARY KEY (id),
    UNIQUE KEY uq_model_registry_provider_model (provider, model_id),
    INDEX idx_model_registry_provider (provider),
    INDEX idx_model_registry_available (available),
    INDEX idx_model_registry_arena_score (arena_score),
    INDEX idx_model_registry_graeae_weight (graeae_weight),
    INDEX idx_model_registry_family (family(191)),
    INDEX idx_model_registry_last_synced (last_synced)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MODEL_REGISTRY_SYNC_LOG = """\
CREATE TABLE IF NOT EXISTS model_registry_sync_log (
    id                VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    provider          VARCHAR(50)  NOT NULL,
    synced_at         TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    models_found      INT          NOT NULL DEFAULT 0,
    models_added      INT          NOT NULL DEFAULT 0,
    models_updated    INT          NOT NULL DEFAULT 0,
    models_deprecated INT          NOT NULL DEFAULT 0,
    error             TEXT,
    duration_ms       INT,
    PRIMARY KEY (id),
    INDEX idx_model_registry_sync_log_provider (provider),
    INDEX idx_model_registry_sync_log_synced_at (synced_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_GRAEAE_CONSULTATIONS = """\
CREATE TABLE IF NOT EXISTS graeae_consultations (
    id                 VARCHAR(64)  NOT NULL,
    prompt             LONGTEXT     NOT NULL,
    task_type          VARCHAR(100) NOT NULL,
    consensus_response LONGTEXT,
    consensus_score    DOUBLE,
    winning_muse       VARCHAR(100),
    cost               DOUBLE       DEFAULT 0,
    latency_ms         INT          DEFAULT 0,
    mode               VARCHAR(50)  DEFAULT 'single',
    owner_id           VARCHAR(256) NOT NULL DEFAULT 'default',
    namespace          VARCHAR(256) NOT NULL DEFAULT 'default',
    created            TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at         TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_graeae_consult_task_type (task_type),
    INDEX idx_graeae_consult_created (created),
    INDEX idx_graeae_consult_mode (mode),
    INDEX idx_graeae_consult_winning_muse (winning_muse),
    INDEX idx_graeae_consultations_owner (owner_id),
    INDEX idx_graeae_consultations_owner_namespace (owner_id, namespace),
    INDEX idx_graeae_consultations_deleted (deleted_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_GRAEAE_AUDIT_LOG = """\
CREATE TABLE IF NOT EXISTS graeae_audit_log (
    id              VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    sequence_num    BIGINT       NOT NULL AUTO_INCREMENT,
    consultation_id VARCHAR(64),
    prompt          LONGTEXT,
    prompt_hash     VARCHAR(64),
    provider        VARCHAR(50),
    model           VARCHAR(100),
    response_text   LONGTEXT,
    response_hash   VARCHAR(64),
    chain_hash      VARCHAR(64),
    prev_id         VARCHAR(64),
    prev_chain_hash VARCHAR(64),
    task_type       VARCHAR(100),
    quality_score   DOUBLE,
    latency_ms      INT,
    cost_usd        DOUBLE,
    created_at      TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at      TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_graeae_audit_sequence (sequence_num),
    INDEX idx_audit_sequence (sequence_num),
    INDEX idx_audit_created (created_at),
    INDEX idx_graeae_audit_log_consultation (consultation_id),
    INDEX idx_graeae_audit_log_created_at (created_at),
    INDEX idx_graeae_audit_log_chain_hash (chain_hash),
    INDEX idx_graeae_audit_log_deleted (deleted_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_CONSULTATION_MEMORY_REFS = """\
CREATE TABLE IF NOT EXISTS consultation_memory_refs (
    consultation_id VARCHAR(64) NOT NULL,
    memory_id       VARCHAR(64) NOT NULL,
    relevance_score DOUBLE,
    injected_at     TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (consultation_id, memory_id),
    INDEX idx_consultation_memory_refs_consultation (consultation_id),
    INDEX idx_consultation_memory_refs_memory (memory_id),
    INDEX idx_consultation_memory_refs_injected_at (injected_at)
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

_DDL_MEMORY_AUDIT_CHAIN = """\
CREATE TABLE IF NOT EXISTS memory_audit_chain (
    entry_id        VARBINARY(32)  NOT NULL,
    memory_id       VARBINARY(32)  NOT NULL,
    prev_entry_id   VARBINARY(32),
    prev_entry_hash VARBINARY(32),
    op              VARCHAR(32)    NOT NULL,
    payload_hash    VARBINARY(32)  NOT NULL,
    writer_id       VARCHAR(256)   NOT NULL,
    writer_pubkey   VARBINARY(512) NOT NULL,
    signature       VARBINARY(512) NOT NULL,
    signed_at       TIMESTAMP(6)   NOT NULL,
    global_root     VARBINARY(32),
    global_seq      BIGINT,
    PRIMARY KEY (entry_id),
    INDEX ix_memory_audit_by_memory (memory_id, signed_at DESC),
    INDEX ix_memory_audit_by_root (global_root),
    INDEX ix_memory_audit_unsigned (signed_at),
    INDEX ix_memory_audit_by_writer (writer_id, signed_at DESC)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MEMORY_AUDIT_ROOTS = """\
CREATE TABLE IF NOT EXISTS memory_audit_roots (
    global_root    VARBINARY(32)  NOT NULL,
    window_start   TIMESTAMP(6)   NOT NULL,
    window_end     TIMESTAMP(6)   NOT NULL,
    entry_count    INT            NOT NULL,
    root_signature VARBINARY(512) NOT NULL,
    signer_pubkey  VARBINARY(512) NOT NULL,
    sealed_at      TIMESTAMP(6)   NOT NULL,
    PRIMARY KEY (global_root),
    INDEX ix_memory_audit_roots_window (window_end DESC)
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
    _DDL_FEDERATION_PEERS,
    _DDL_FEDERATION_SYNC_LOG,
    _DDL_MEMORY_VERSIONS,
    _DDL_MEMORY_BRANCHES,
    _DDL_MEMORY_AUDIT_CHAIN,
    _DDL_MEMORY_AUDIT_ROOTS,
    _DDL_KG_TRIPLES,
    _DDL_COMPRESSION_CANDIDATES,
    _DDL_COMPRESSED_VARIANTS,
    _DDL_COMPRESSION_QUEUE,
    _DDL_STATE,
    _DDL_MODEL_REGISTRY,
    _DDL_MODEL_REGISTRY_SYNC_LOG,
    _DDL_GRAEAE_CONSULTATIONS,
    _DDL_GRAEAE_AUDIT_LOG,
    _DDL_CONSULTATION_MEMORY_REFS,
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
        self._named_locks: set[str] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def conn(self) -> Any:
        return self._conn

    def named_lock_held(self, name: str) -> bool:
        return name in self._named_locks

    def hold_named_lock(self, name: str) -> None:
        self._named_locks.add(name)

    async def _release_named_locks(self) -> None:
        if not self._named_locks:
            return
        async with self._conn.cursor() as cursor:
            for name in tuple(self._named_locks):
                await cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
        self._named_locks.clear()

    async def commit(self) -> None:
        if self._closed:
            return
        try:
            await self._conn.commit()
        finally:
            await self._release_named_locks()
            self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        try:
            await self._conn.rollback()
        finally:
            await self._release_named_locks()
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
        embedding: Sequence[float] | None = None,
        created: Any,
        updated: Any,
    ) -> str:
        conn = tx.conn
        # Format embedding as MySQL TO_VECTOR literal; NULL when absent.
        # Inlining it in the INSERT keeps the vector co-transactional
        # with the row — semantic_search sees it immediately.
        vec_literal: str | None = None
        if embedding:
            self._require_dim(embedding, "insert_memory")
            vec_literal = _validate_and_format_vector(embedding)
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memories (
                        id, content, content_hash, category, subcategory, metadata,
                        quality_rating, verbatim_content, owner_id, namespace,
                        permission_mode, source_model, source_provider,
                        source_session, source_agent,
                        embedding, created, updated
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        TO_VECTOR(%s),
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
                        vec_literal,
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
                f"""
                SELECT DISTINCT memory_id
                  FROM memory_versions
                 WHERE memory_id IN ({placeholders})
                   AND deleted_at IS NULL
                """,
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
                f"""
                SELECT m.id, m.content AS memory_content,
                       mv.content AS head_content
                  FROM memories m
                  LEFT JOIN memory_branches b
                    ON b.memory_id = m.id AND b.name = 'main'
                  LEFT JOIN memory_versions mv
                    ON mv.id = b.head_version_id
                   AND mv.deleted_at IS NULL
                 WHERE m.id IN ({placeholders})
                   AND m.deleted_at IS NULL
                """,
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
            row = await _fetchone_dict(cursor) or {}
            await cursor.execute(
                """
                SELECT federation_source, COUNT(*) AS cnt
                  FROM memories
                 WHERE federation_source IS NOT NULL AND deleted_at IS NULL
                 GROUP BY federation_source
                 ORDER BY cnt DESC
                """
            )
            peer_rows = await _fetch_all_dicts(cursor)
            await cursor.execute(
                """
                SELECT category, COUNT(*) AS cnt
                  FROM memories
                 WHERE deleted_at IS NULL
                 GROUP BY category
                """
            )
            cat_rows = await _fetch_all_dicts(cursor)
            await cursor.execute(
                """
                SELECT category, subcategory, COUNT(*) AS cnt
                  FROM memories
                 WHERE subcategory IS NOT NULL AND deleted_at IS NULL
                 GROUP BY category, subcategory
                 ORDER BY cnt DESC
                """
            )
            sub_rows = await _fetch_all_dicts(cursor)

        from mnemos.persistence.base import MemoryStatsRow

        memories_by_subcategory: dict[str, dict[str, int]] = {}
        for sub_row in sub_rows:
            memories_by_subcategory.setdefault(str(sub_row["category"]), {})[str(sub_row["subcategory"])] = int(
                sub_row["cnt"] or 0
            )
        return MemoryStatsRow(
            total_memories=int(row.get("total_memories") or 0),
            native_memories=int(row.get("native_memories") or 0),
            federated_memories=int(row.get("federated_memories") or 0),
            memories_by_peer={str(r["federation_source"]): int(r["cnt"] or 0) for r in peer_rows},
            memories_by_category={str(r["category"]): int(r["cnt"] or 0) for r in cat_rows},
            memories_by_subcategory=memories_by_subcategory,
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

    # --- DAG/export/dedup parity methods ---

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            if user.role == "root":
                await cursor.execute(
                    "SELECT 1 FROM memory_versions WHERE memory_id = %s AND deleted_at IS NULL LIMIT 1",
                    (memory_id,),
                )
            else:
                vis_clause, vis_params = _render_visibility(
                    VisibilityFilter.for_read(user, namespace=user.namespace),
                    table_alias="m",
                )
                await cursor.execute(
                    f"""
                    SELECT 1
                      FROM memories m
                     WHERE m.id = %s
                       AND m.deleted_at IS NULL
                       AND {vis_clause}
                     LIMIT 1
                    """,
                    [memory_id, *vis_params],
                )
            if await cursor.fetchone() is None:
                raise PermissionError("Memory not found")

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        conn = tx.conn
        scope_sql = ""
        scope_params: list[Any] = []
        if user.role != "root":
            scope_sql = "AND (mv.owner_id = %s OR MOD(mv.permission_mode, 10) >= 4) AND mv.namespace = %s"
            scope_params = [user.user_id, user.namespace]
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                WITH RECURSIVE commit_walk AS (
                    SELECT mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                           mv.version_num, mv.branch, mv.content, mv.category,
                           mv.change_type, mv.snapshot_at, mv.snapshot_by,
                           mv.owner_id, mv.namespace, mv.permission_mode, 1 AS depth
                      FROM memory_versions mv
                      INNER JOIN memory_branches mb
                              ON mb.memory_id = mv.memory_id
                             AND mb.name = %s
                             AND mb.head_version_id = mv.id
                     WHERE mv.memory_id = %s
                       AND mv.deleted_at IS NULL
                       {scope_sql}
                    UNION ALL
                    SELECT mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                           mv.version_num, mv.branch, mv.content, mv.category,
                           mv.change_type, mv.snapshot_at, mv.snapshot_by,
                           mv.owner_id, mv.namespace, mv.permission_mode, cw.depth + 1
                      FROM memory_versions mv
                      INNER JOIN commit_walk cw
                              ON mv.id = cw.parent_version_id
                             AND mv.memory_id = cw.memory_id
                     WHERE cw.depth < %s
                       AND mv.deleted_at IS NULL
                       {scope_sql}
                )
                SELECT commit_hash, version_num, branch, category, change_type,
                       snapshot_at, snapshot_by, owner_id, namespace, permission_mode
                  FROM commit_walk
                 ORDER BY depth ASC
                 LIMIT %s
                """,
                [branch, memory_id, *scope_params, limit, *scope_params, limit],
            )
            return await _fetch_all_dicts(cursor)

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        conn = tx.conn
        sql = """
            SELECT content, version_num
              FROM memory_versions
             WHERE memory_id = %s AND commit_hash = %s AND deleted_at IS NULL
        """
        async with conn.cursor() as cursor:
            if user.role == "root":
                await cursor.execute(sql, (memory_id, commit_a))
                row_a = await _fetchone_dict(cursor)
                await cursor.execute(sql, (memory_id, commit_b))
                row_b = await _fetchone_dict(cursor)
            else:
                gated = sql + " AND (owner_id = %s OR MOD(permission_mode, 10) >= 4) AND namespace = %s"
                await cursor.execute(gated, (memory_id, commit_a, user.user_id, user.namespace))
                row_a = await _fetchone_dict(cursor)
                await cursor.execute(gated, (memory_id, commit_b, user.user_id, user.namespace))
                row_b = await _fetchone_dict(cursor)
        return row_a, row_b

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        conn = tx.conn
        sql = """
            SELECT commit_hash, version_num, branch, category, subcategory,
                   content, change_type, snapshot_at, snapshot_by
              FROM memory_versions
             WHERE memory_id = %s AND commit_hash = %s AND deleted_at IS NULL
        """
        params: list[Any] = [memory_id, commit_hash]
        if user.role != "root":
            sql += " AND (owner_id = %s OR MOD(permission_mode, 10) >= 4) AND namespace = %s"
            params.extend([user.user_id, user.namespace])
        async with conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetchone_dict(cursor)

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
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if effective_owner:
            conditions.append("owner_id = %s")
            params.append(effective_owner)
        if effective_ns:
            conditions.append("namespace = %s")
            params.append(effective_ns)
        if category:
            conditions.append("category = %s")
            params.append(category)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, content, category, subcategory, created, updated,
                       owner_id, namespace, permission_mode, quality_rating,
                       source_model, source_provider, source_session, source_agent,
                       metadata, NULL AS prov_kind, NULL AS morpheus_run_id,
                       NULL AS source_memories, federation_source
                  FROM memories
                 WHERE {" AND ".join(conditions)}
                 ORDER BY created ASC
                 LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            return await _fetch_all_dicts(cursor)

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
        placeholders = ", ".join(["%s"] * len(referenced_ids))
        sql = f"SELECT id, owner_id, namespace FROM memories WHERE id IN ({placeholders}) AND deleted_at IS NULL"
        params: list[Any] = list(referenced_ids)
        if scope_owner is not None:
            sql += " AND owner_id = %s"
            params.append(scope_owner)
        if scope_namespace is not None:
            sql += " AND namespace = %s"
            params.append(scope_namespace)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetch_all_dicts(cursor)

    async def fetch_duplicate_content_groups(self, *args: Any, **kwargs: Any) -> list[Row]:
        return await self.find_duplicate_content_groups(*args, **kwargs)

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        params: list[Any] = []
        namespace_sql = ""
        if namespace is not None:
            namespace_sql = "AND namespace = %s"
            params.append(namespace)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT owner_id, namespace, content_hash,
                       COUNT(*) AS duplicate_count,
                       JSON_ARRAYAGG(id) AS memory_ids,
                       SUBSTRING_INDEX(GROUP_CONCAT(id ORDER BY created ASC, id ASC), ',', 1) AS canonical_id
                  FROM memories
                 WHERE deleted_at IS NULL
                   AND archived_at IS NULL
                   AND consolidated_into IS NULL
                   AND content_hash IS NOT NULL
                   {namespace_sql}
                 GROUP BY owner_id, namespace, content_hash
                HAVING COUNT(*) > 1
                 ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
                """,
                params,
            )
            rows = await _fetch_all_dicts(cursor)
        for row in rows:
            row["memory_ids"] = _json_list(row.get("memory_ids"))
        return rows

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        placeholders = ", ".join(["%s"] * len(duplicate_ids))
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                UPDATE memories
                   SET consolidated_into = %s,
                       consolidated_at = NOW(6),
                       deleted_at = COALESCE(deleted_at, NOW(6)),
                       updated = NOW(6)
                 WHERE id IN ({placeholders})
                   AND id <> %s
                   AND deleted_at IS NULL
                   AND archived_at IS NULL
                   AND consolidated_into IS NULL
                   AND EXISTS (
                       SELECT 1 FROM (SELECT id FROM memories
                                       WHERE id = %s AND deleted_at IS NULL
                                         AND archived_at IS NULL
                                         AND consolidated_into IS NULL) c
                   )
                """,
                [canonical_id, *duplicate_ids, canonical_id, canonical_id],
            )
            return int(cursor.rowcount or 0)


# ── KG, Version, Branch, Compression, Webhook, ConsultationAudit,
#    Federation, State, AuditChain repositories ───────────────────────────────


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
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conn = tx.conn
        placeholders = ", ".join(["%s"] * len(memory_ids))
        where = [f"memory_id IN ({placeholders})"]
        params: list[Any] = list(memory_ids)
        if effective_owner:
            where.append("owner_id = %s")
            params.append(effective_owner)
        params.append(hard_limit + 1)
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT memory_id, owner_id, winner_candidate_id, engine_id, "
                "engine_version, compressed_content, compressed_tokens, "
                "compression_ratio, quality_score, composite_score, "
                "scoring_profile, judge_model, selected_at "
                "FROM memory_compressed_variants "
                f"WHERE {' AND '.join(where)} "
                "LIMIT %s",
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT 1
                  FROM memory_compression_candidates
                 WHERE id = %s
                   AND memory_id = %s
                   AND owner_id = %s
                """,
                (candidate_id, memory_id, owner_id),
            )
            return await cursor.fetchone() is not None

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
        conn = tx.conn
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memory_compressed_variants (
                        memory_id, owner_id, winner_candidate_id,
                        engine_id, engine_version, compressed_content,
                        compressed_tokens, compression_ratio,
                        quality_score, composite_score,
                        scoring_profile, judge_model, selected_at
                    )
                    VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        COALESCE(%s, 'balanced'), %s,
                        COALESCE(%s, CURRENT_TIMESTAMP(6))
                    )
                    ON DUPLICATE KEY UPDATE memory_id = memory_id
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
                return "INSERT 0 1" if cursor.rowcount else "INSERT 0 0"
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT owner_id, winner_candidate_id, engine_id, engine_version,
                       compressed_content, compressed_tokens, compression_ratio,
                       quality_score, composite_score, scoring_profile, judge_model,
                       selected_at
                  FROM memory_compressed_variants
                 WHERE memory_id = %s
                """,
                (memory_id,),
            )
            return await _fetchone_dict(cursor)

    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT COUNT(*), AVG(compression_ratio),
                       SUM(CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END)
                  FROM memory_compressed_variants
                """,
            )
            row = await cursor.fetchone() or (0, None, 0)
        total, avg_ratio, unreviewed = row
        return CompressionStatsRow(
            total_compressions=int(total or 0),
            average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
            unreviewed_compressions=int(unreviewed or 0),
        )


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
    _AUDIT_CHAIN_LOCK_NAME = "mnemos_audit_global"
    _AUDIT_CHAIN_LOCK_TIMEOUT_SECS = 10

    async def _insert_audit_link_locked(
        self,
        tx: Transaction,
        cursor: Any,
        *,
        audit_id: str,
        consultation_id: str | None,
        prompt: str,
        prompt_hash: str,
        provider: str | None,
        model: str | None = None,
        response_text: str,
        response_hash: str,
        task_type: str | None,
        quality_score: Any | None,
        latency_ms: Any | None = None,
        cost_usd: Any | None = None,
        genesis_hash: str = "",
    ) -> str:
        lock_name = self._AUDIT_CHAIN_LOCK_NAME
        named_lock_held = getattr(tx, "named_lock_held", None)
        hold_named_lock = getattr(tx, "hold_named_lock", None)
        release_immediately = hold_named_lock is None

        if not (callable(named_lock_held) and named_lock_held(lock_name)):
            await cursor.execute(
                "SELECT GET_LOCK(%s, %s)",
                (lock_name, self._AUDIT_CHAIN_LOCK_TIMEOUT_SECS),
            )
            lock_row = await cursor.fetchone()
            if isinstance(lock_row, dict):
                lock_value = next(iter(lock_row.values()), 0)
            else:
                lock_value = lock_row[0] if lock_row else 0
            if int(lock_value or 0) != 1:
                raise TimeoutError(f"Timed out acquiring MySQL audit chain lock {lock_name!r}")
            if callable(hold_named_lock):
                hold_named_lock(lock_name)

        try:
            await cursor.execute(
                """
                SELECT id, chain_hash
                  FROM graeae_audit_log
                 WHERE deleted_at IS NULL
                 ORDER BY sequence_num DESC
                 LIMIT 1
                """
            )
            prev = await _fetchone_dict(cursor)
            prev_id = prev["id"] if prev else None
            prev_chain_hash = prev["chain_hash"] if prev else genesis_hash
            chain_hash = hashlib.sha256((prev_chain_hash + prompt_hash + response_hash).encode()).hexdigest()
            await cursor.execute(
                """
                INSERT INTO graeae_audit_log (
                    id, consultation_id, prompt, prompt_hash, provider, model, response_text,
                    response_hash, chain_hash, prev_id, prev_chain_hash, task_type, quality_score,
                    latency_ms, cost_usd
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    audit_id,
                    consultation_id,
                    prompt,
                    prompt_hash,
                    provider,
                    model,
                    response_text,
                    response_hash,
                    chain_hash,
                    prev_id,
                    prev_chain_hash,
                    task_type or "reasoning",
                    quality_score,
                    latency_ms,
                    cost_usd,
                ),
            )
        finally:
            if release_immediately:
                await cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        return audit_id

    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        from mnemos.core.recommendation import choose_recommended_model

        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT provider, model_id, display_name, input_cost_per_mtok, output_cost_per_mtok,
                       capabilities, COALESCE(graeae_weight, 0) AS graeae_weight, context_window
                  FROM model_registry
                 WHERE available = TRUE
                   AND deprecated = FALSE
                """
            )
            rows = await _fetch_all_dicts(cursor)
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT provider
                  FROM model_registry
                 WHERE model_id = %s
                   AND available = TRUE
                   AND deprecated = FALSE
                """,
                (model,),
            )
            row = await _fetchone_dict(cursor)
            if row is not None:
                return row["provider"]

            if "/" not in model:
                return None

            head, tail = model.split("/", 1)
            await cursor.execute(
                """
                SELECT provider
                  FROM model_registry
                 WHERE provider = %s
                   AND model_id = %s
                   AND available = TRUE
                   AND deprecated = FALSE
                """,
                (head, tail),
            )
            row = await _fetchone_dict(cursor)
            return row["provider"] if row is not None else None

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT provider, model_id, display_name
                  FROM model_registry
                 WHERE available = TRUE
                   AND deprecated = FALSE
                 ORDER BY graeae_weight IS NULL, graeae_weight DESC, model_id ASC
                """
            )
            return await _fetch_all_dicts(cursor)

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT provider
                  FROM model_registry
                 WHERE model_id = %s
                   AND available = TRUE
                   AND deprecated = FALSE
                 LIMIT 1
                """,
                (model_id,),
            )
            row = await _fetchone_dict(cursor)
            return row["provider"] if row is not None else None

    async def insert_consultation_audit(self, tx: Transaction, **kwargs: Any) -> str:
        audit_id = str(kwargs.get("id") or uuid.uuid4().hex)
        prompt = kwargs.get("prompt") or ""
        response_text = kwargs.get("response_text") or kwargs.get("response") or ""
        prompt_hash = kwargs.get("prompt_hash") or hashlib.sha256(prompt.encode()).hexdigest()
        response_hash = kwargs.get("response_hash") or hashlib.sha256(response_text.encode()).hexdigest()

        async with tx.conn.cursor() as cursor:
            await self._insert_audit_link_locked(
                tx,
                cursor,
                audit_id=audit_id,
                consultation_id=kwargs.get("consultation_id"),
                prompt=prompt,
                prompt_hash=prompt_hash,
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                response_text=response_text,
                response_hash=response_hash,
                task_type=kwargs.get("task_type"),
                quality_score=kwargs.get("quality_score"),
                latency_ms=kwargs.get("latency_ms"),
                cost_usd=kwargs.get("cost_usd"),
                genesis_hash=kwargs.get("genesis_hash") or "",
            )
        return audit_id

    async def fetch_consultation_audits(
        self,
        tx: Transaction,
        *,
        consultation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Row]:
        sql = """
            SELECT id, sequence_num, consultation_id, prompt, prompt_hash, provider, model,
                   response_text, response_hash, chain_hash, prev_id, prev_chain_hash,
                   task_type, quality_score, latency_ms, cost_usd, created_at
              FROM graeae_audit_log
             WHERE deleted_at IS NULL
        """
        params: list[Any] = []
        if consultation_id is not None:
            sql += " AND consultation_id = %s"
            params.append(consultation_id)
        sql += " ORDER BY sequence_num DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        async with tx.conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetch_all_dicts(cursor)

    async def fetch_consultation_audit(self, tx: Transaction, audit_id: str) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, sequence_num, consultation_id, prompt, prompt_hash, provider, model,
                       response_text, response_hash, chain_hash, prev_id, prev_chain_hash,
                       task_type, quality_score, latency_ms, cost_usd, created_at
                  FROM graeae_audit_log
                 WHERE id = %s
                   AND deleted_at IS NULL
                """,
                (audit_id,),
            )
            return await _fetchone_dict(cursor)

    async def fetch_consultation_by_id(self, tx: Transaction, consultation_id: str) -> Row | None:
        return await self.get_consultation(
            tx,
            consultation_id=consultation_id,
            root=True,
            user_id="",
            namespace=None,
        )

    async def fetch_consultations(
        self,
        tx: Transaction,
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Row]:
        sql = """
            SELECT id, prompt, task_type, consensus_response, consensus_score,
                   winning_muse, cost, latency_ms, mode, owner_id, namespace, created
              FROM graeae_consultations
             WHERE deleted_at IS NULL
        """
        params: list[Any] = []
        if owner_id is not None:
            sql += " AND owner_id = %s"
            params.append(owner_id)
        if namespace is not None:
            sql += " AND namespace = %s"
            params.append(namespace)
        sql += " ORDER BY created DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        async with tx.conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetch_all_dicts(cursor)

    async def create_consultation_with_audit(self, tx: Transaction, **kwargs: Any) -> Any:
        consultation_id = uuid.uuid4().hex
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO graeae_consultations (
                    id, prompt, task_type, consensus_response, consensus_score, winning_muse,
                    cost, latency_ms, mode, owner_id, namespace
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    consultation_id,
                    kwargs["prompt"],
                    kwargs["task_type"],
                    kwargs["consensus_response"][:500],
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
            await self._insert_audit_link_locked(
                tx,
                cursor,
                audit_id=uuid.uuid4().hex,
                consultation_id=consultation_id,
                prompt=kwargs["prompt"],
                prompt_hash=prompt_hash,
                provider=kwargs["winning_muse"],
                response_text=kwargs["consensus_response"],
                response_hash=response_hash,
                task_type=kwargs["task_type"],
                quality_score=kwargs["consensus_score"],
                genesis_hash=kwargs["genesis_hash"],
            )
            for memory_id in kwargs["memory_ids"]:
                await cursor.execute(
                    """
                    INSERT INTO consultation_memory_refs (consultation_id, memory_id, injected_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE consultation_id = consultation_id
                    """,
                    (consultation_id, memory_id),
                )
        return consultation_id

    async def list_audit_log(
        self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None, limit: int, offset: int
    ) -> list[Row]:
        if root and namespace is None:
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT id, sequence_num, consultation_id, prompt_hash, response_hash,
                           chain_hash, prev_id, task_type, provider, quality_score, created_at
                      FROM graeae_audit_log
                     WHERE deleted_at IS NULL
                     ORDER BY sequence_num DESC
                     LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                return await _fetch_all_dicts(cursor)

        if root:
            sql = """
                SELECT al.id, al.sequence_num, al.consultation_id, al.prompt_hash,
                       al.response_hash, al.chain_hash, al.prev_id, al.task_type,
                       al.provider, al.quality_score, al.created_at
                  FROM graeae_audit_log al
                  JOIN graeae_consultations c ON c.id = al.consultation_id
                 WHERE c.namespace = %s
                   AND c.deleted_at IS NULL
                   AND al.deleted_at IS NULL
                 ORDER BY al.sequence_num DESC
                 LIMIT %s OFFSET %s
            """
            params = (namespace, limit, offset)
        else:
            sql = """
                WITH visible AS (
                    SELECT al.id, al.sequence_num AS global_sequence_num, al.consultation_id,
                           al.prompt_hash, al.response_hash, al.task_type, al.provider,
                           al.quality_score, al.created_at,
                           ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num,
                           LAG(al.id) OVER (ORDER BY al.sequence_num ASC) AS scoped_prev_id
                      FROM graeae_audit_log al
                      JOIN graeae_consultations c ON c.id = al.consultation_id
                     WHERE c.owner_id = %s
                       AND c.namespace = %s
                       AND c.deleted_at IS NULL
                       AND al.deleted_at IS NULL
                )
                SELECT id, scoped_sequence_num AS sequence_num, consultation_id, prompt_hash,
                       response_hash, NULL AS chain_hash, scoped_prev_id AS prev_id,
                       task_type, provider, quality_score, created_at
                  FROM visible
                 ORDER BY global_sequence_num DESC
                 LIMIT %s OFFSET %s
            """
            params = (user_id, namespace, limit, offset)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetch_all_dicts(cursor)

    async def fetch_audit_chain(self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None) -> list[Row]:
        if root and namespace is None:
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id
                      FROM graeae_audit_log
                     WHERE deleted_at IS NULL
                     ORDER BY sequence_num ASC
                    """
                )
                return await _fetch_all_dicts(cursor)
        owner_sql = "" if root else "c.owner_id = %s AND "
        params = (namespace,) if root else (user_id, namespace)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                "SELECT al.sequence_num, ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
                "al.prompt_hash, al.response_hash, al.chain_hash, al.prev_id, al.prev_chain_hash, "
                "(SELECT prev.chain_hash FROM graeae_audit_log prev WHERE prev.sequence_num < al.sequence_num "
                "ORDER BY prev.sequence_num DESC LIMIT 1) AS expected_prev_hash "
                "FROM graeae_audit_log al JOIN graeae_consultations c ON c.id = al.consultation_id "
                f"WHERE {owner_sql}c.namespace = %s AND c.deleted_at IS NULL AND al.deleted_at IS NULL "
                "ORDER BY al.sequence_num ASC",
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def get_consultation(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> Row | None:
        if root and namespace is None:
            sql = """
                SELECT id, prompt, task_type, consensus_response, consensus_score,
                       winning_muse, cost, latency_ms, mode, created
                  FROM graeae_consultations
                 WHERE id = %s
                   AND deleted_at IS NULL
            """
            params = (consultation_id,)
        elif root:
            sql = """
                SELECT id, prompt, task_type, consensus_response, consensus_score,
                       winning_muse, cost, latency_ms, mode, created
                  FROM graeae_consultations
                 WHERE id = %s
                   AND namespace = %s
                   AND deleted_at IS NULL
            """
            params = (consultation_id, namespace)
        else:
            sql = """
                SELECT id, prompt, task_type, consensus_response, consensus_score,
                       winning_muse, cost, latency_ms, mode, created
                  FROM graeae_consultations
                 WHERE id = %s
                   AND owner_id = %s
                   AND namespace = %s
                   AND deleted_at IS NULL
            """
            params = (consultation_id, user_id, namespace)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(sql, params)
            return await _fetchone_dict(cursor)

    async def get_consultation_artifacts(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> tuple[Row | None, list[Row]]:
        consultation = await self.get_consultation(
            tx,
            consultation_id=consultation_id,
            root=root,
            user_id=user_id,
            namespace=namespace,
        )
        if not consultation:
            return None, []
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT memory_id, injected_at
                  FROM consultation_memory_refs
                 WHERE consultation_id = %s
                 ORDER BY injected_at
                """,
                (consultation_id,),
            )
            refs = await _fetch_all_dicts(cursor)
        return consultation, refs


class MysqlFederationRepository(FederationRepository):
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
        out["copy_embeddings"] = bool(out.get("copy_embeddings", False))
        out["namespace_filter"] = _json_list(out.get("namespace_filter")) or None
        out["category_filter"] = _json_list(out.get("category_filter")) or None
        out["created"] = out.get("created") or out.get("created_at")
        out["updated"] = out.get("updated") or out.get("updated_at")
        out["last_sync_cursor"] = out.get("last_sync_cursor") or out.get("cursor_updated")
        return out

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: Any | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        where = ["deleted_at IS NULL"]
        params: list[Any] = []
        if updated_after is not None and id_after is not None:
            where.append("(updated > %s OR (updated = %s AND id > %s))")
            params.extend([updated_after, updated_after, id_after])
        params.append(limit)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, content, category, subcategory, metadata,
                       owner_id, namespace, updated
                  FROM memories
                 WHERE {" AND ".join(where)}
                 ORDER BY updated ASC, id ASC
                 LIMIT %s
                """,
                params,
            )
            return await _fetch_all_dicts(cursor)

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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO federation_peers
                  (id, name, base_url, auth_token, api_key, namespace_filter,
                   category_filter, enabled, sync_interval_secs, compat_mode,
                   created, updated)
                VALUES
                  (%s, %s, %s, %s, %s, CAST(%s AS JSON),
                   CAST(%s AS JSON), %s, %s, %s,
                   CURRENT_TIMESTAMP(6), CURRENT_TIMESTAMP(6))
                """,
                (
                    peer_id,
                    name,
                    base_url,
                    auth_token,
                    auth_token,
                    _json_array_text(namespace_filter),
                    _json_array_text(category_filter),
                    bool(enabled),
                    sync_interval_secs,
                    compat_mode,
                ),
            )
        row = await self.get_peer(tx, peer_id)
        assert row is not None
        return row

    async def list_peers(self, tx: Transaction) -> list[Row]:
        async with tx.conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM federation_peers ORDER BY name")
            rows = await _fetch_all_dicts(cursor)
        return [self._peer_row(row) for row in rows]  # type: ignore[list-item]

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM federation_peers WHERE id = %s", (peer_id,))
            return self._peer_row(await _fetchone_dict(cursor))

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        assignments: list[str] = []
        params: list[Any] = []
        for col, value in updates.items():
            if col in {"namespace_filter", "category_filter"}:
                assignments.append(f"{col} = CAST(%s AS JSON)")
                params.append(_json_array_text(value))
            elif col == "enabled":
                assignments.append("enabled = %s")
                params.append(bool(value))
            else:
                assignments.append(f"{col} = %s")
                params.append(value)
        assignments.append("updated = CURRENT_TIMESTAMP(6)")
        params.append(peer_id)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE federation_peers SET {', '.join(assignments)} WHERE id = %s",
                params,
            )
            if not cursor.rowcount:
                return None
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO federation_peers (id, base_url, name, auth_token, enabled)
                VALUES (%s, %s, %s, '', %s)
                ON DUPLICATE KEY UPDATE
                    base_url = VALUES(base_url),
                    name = VALUES(name),
                    enabled = VALUES(enabled),
                    updated = CURRENT_TIMESTAMP(6)
                """,
                (peer_id, base_url, name, bool(enabled)),
            )

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        async with tx.conn.cursor() as cursor:
            await cursor.execute("DELETE FROM federation_peers WHERE id = %s", (peer_id,))
            return int(cursor.rowcount or 0) > 0

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, started_at, finished_at, memories_pulled,
                       memories_new, memories_updated, error,
                       cursor_before, cursor_after
                  FROM federation_sync_log
                 WHERE peer_id = %s
                 ORDER BY started_at DESC
                 LIMIT %s
                """,
                (peer_id, limit),
            )
            return await _fetch_all_dicts(cursor)

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
        memory_where = [
            "m.federation_source IS NULL",
            "(MOD(m.permission_mode, 10)) >= 4",
            "m.archived_at IS NULL",
            "m.consolidated_into IS NULL",
            "m.deleted_at IS NULL",
        ]
        tombstone_where = [
            "m.federation_source IS NULL",
            "m.consolidated_into IS NOT NULL",
            "m.consolidated_at IS NOT NULL",
            "m.deleted_at IS NULL",
        ]
        memory_params: list[Any] = []
        tombstone_params: list[Any] = []
        if since_updated is not None:
            memory_where.append("(m.updated > %s OR (m.updated = %s AND m.id > %s))")
            memory_params.extend([since_updated, since_updated, since_id])
            tombstone_where.append("(m.consolidated_at > %s OR (m.consolidated_at = %s AND m.id > %s))")
            tombstone_params.extend([since_updated, since_updated, since_id])
        if namespaces:
            placeholders = ", ".join(["%s"] * len(namespaces))
            memory_where.append(f"m.namespace IN ({placeholders})")
            tombstone_where.append(f"m.namespace IN ({placeholders})")
            memory_params.extend(namespaces)
            tombstone_params.extend(namespaces)
        if categories:
            placeholders = ", ".join(["%s"] * len(categories))
            memory_where.append(f"m.category IN ({placeholders})")
            tombstone_where.append(f"m.category IN ({placeholders})")
            memory_params.extend(categories)
            tombstone_params.extend(categories)

        if prefer_compressed:
            use_variant = (
                "m.archived_at IS NULL "
                "AND v.compressed_content IS NOT NULL "
                "AND (2 * CHAR_LENGTH(JSON_QUOTE(v.compressed_content))) "
                "  < (CHAR_LENGTH(JSON_QUOTE(m.content)) "
                "     + COALESCE(CHAR_LENGTH(JSON_QUOTE(m.verbatim_content)), 0))"
            )
            content_select = f"CASE WHEN {use_variant} THEN v.compressed_content ELSE m.content END AS content,"
            compressed_select = (
                f"CASE WHEN {use_variant} THEN v.compressed_content ELSE NULL END AS compressed_content,"
            )
            verbatim_select = f"CASE WHEN {use_variant} THEN NULL ELSE m.verbatim_content END AS verbatim_content,"
            join_compressed = "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id"
        else:
            content_select = "m.content,"
            compressed_select = "NULL AS compressed_content,"
            verbatim_select = "m.verbatim_content,"
            join_compressed = ""

        if include_embedding:
            from mnemos.core.config import get_settings as _gs
            from mnemos.core.config import embed_http_model_override

            try:
                http_model = embed_http_model_override()
                embed_model = http_model or (_gs().providers.inference_embed_model or "").strip() or "unknown"
            except Exception:
                embed_model = "unknown"
            embed_select_memory = "FROM_VECTOR(m.embedding) AS embedding, %s AS embedding_model,"
            embed_select_tombstone = "NULL AS embedding, NULL AS embedding_model,"
            select_params = [embed_model]
        else:
            embed_select_memory = ""
            embed_select_tombstone = ""
            select_params = []

        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT *
                FROM (
                    SELECT NULL AS type,
                           m.id,
                           {content_select}
                           m.category,
                           m.subcategory,
                           m.metadata,
                           m.quality_rating,
                           {verbatim_select}
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
                           {compressed_select}
                           {embed_select_memory}
                           NULL AS _trailer
                    FROM memories m
                    {join_compressed}
                    WHERE {" AND ".join(memory_where)}

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
                           NULL AS compressed_content,
                           {embed_select_tombstone}
                           NULL AS _trailer
                    FROM memories m
                    WHERE {" AND ".join(tombstone_where)}
                ) feed
                ORDER BY updated ASC, id ASC
                LIMIT %s
                """,
                [*select_params, *memory_params, *tombstone_params, limit],
            )
            return await _fetch_all_dicts(cursor)

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        where = [
            "m.federation_source IS NULL",
            "(MOD(m.permission_mode, 10)) >= 4",
            "m.archived_at IS NULL",
            "m.consolidated_into IS NULL",
            "m.deleted_at IS NULL",
            "m.id = %s",
        ]
        params: list[Any] = [memory_id]
        if namespaces:
            where.append(f"m.namespace IN ({', '.join(['%s'] * len(namespaces))})")
            params.extend(namespaces)
        if categories:
            where.append(f"m.category IN ({', '.join(['%s'] * len(categories))})")
            params.extend(categories)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, content, category, subcategory, metadata, quality_rating,
                       verbatim_content, owner_id, namespace, permission_mode,
                       source_model, source_provider, source_session, source_agent,
                       created, updated, archived_at
                  FROM memories m
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            return await _fetchone_dict(cursor)

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, name, base_url, auth_token, namespace_filter,
                       category_filter, enabled, last_sync_cursor, compat_mode,
                       COALESCE(copy_embeddings, 0) AS copy_embeddings
                  FROM federation_peers
                 WHERE id = %s
                """,
                (peer_id,),
            )
            return self._peer_row(await _fetchone_dict(cursor))

    async def update_peer_schema_check(self, tx: Transaction, peer_id: str, peer_version: str | None) -> None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE federation_peers
                   SET peer_mnemos_version = %s,
                       last_schema_check_at = CURRENT_TIMESTAMP(6)
                 WHERE id = %s
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
        async with tx.conn.cursor() as cursor:
            if is_transient:
                await cursor.execute(
                    """
                    UPDATE federation_peers
                       SET last_sync_at = DATE_ADD(
                               DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL sync_interval_secs SECOND),
                               INTERVAL 60 SECOND
                           ),
                           last_error = %s,
                           last_error_at = CURRENT_TIMESTAMP(6)
                     WHERE id = %s
                    """,
                    (error, peer_id),
                )
            else:
                await cursor.execute(
                    """
                    UPDATE federation_peers
                       SET last_sync_at = CURRENT_TIMESTAMP(6),
                           last_error = %s,
                           last_error_at = CURRENT_TIMESTAMP(6)
                     WHERE id = %s
                    """,
                    (error, peer_id),
                )

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        log_id = str(uuid.uuid4())
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO federation_sync_log
                  (id, peer_id, direction, status, started_at, cursor_before)
                VALUES (%s, %s, 'pull', 'started', CURRENT_TIMESTAMP(6), %s)
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE federation_sync_log
                   SET finished_at = CURRENT_TIMESTAMP(6),
                       memories_pulled = %s,
                       memories_new = %s,
                       memories_updated = %s,
                       records_seen = %s,
                       records_written = %s,
                       status = %s,
                       error = %s,
                       cursor_after = %s
                 WHERE id = %s
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE federation_peers
                   SET last_sync_at = CURRENT_TIMESTAMP(6),
                       last_error = %s,
                       last_error_at = CURRENT_TIMESTAMP(6)
                 WHERE id = %s
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
        async with tx.conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE federation_peers
                   SET last_sync_at = CURRENT_TIMESTAMP(6),
                       last_sync_cursor = %s,
                       cursor_updated = %s,
                       last_error = NULL,
                       last_error_at = NULL,
                       total_pulled = total_pulled + %s
                 WHERE id = %s
                """,
                (cursor, cursor, total_pulled, peer_id),
            )

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, name, sync_interval_secs, last_sync_at
                  FROM federation_peers
                 WHERE enabled = TRUE
                   AND (
                        last_sync_at IS NULL
                        OR DATE_ADD(last_sync_at, INTERVAL sync_interval_secs SECOND) <= CURRENT_TIMESTAMP(6)
                   )
                 ORDER BY COALESCE(
                     DATE_ADD(last_sync_at, INTERVAL sync_interval_secs SECOND),
                     TIMESTAMP('1970-01-01 00:00:00')
                 )
                 LIMIT %s
                """,
                (limit,),
            )
            return await _fetch_all_dicts(cursor)

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                "SELECT federation_remote_updated FROM memories WHERE id = %s AND deleted_at IS NULL",
                (local_id,),
            )
            return await _fetchone_dict(cursor)

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
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memories
                      (id, content, content_hash, category, subcategory, metadata,
                       verbatim_content, quality_rating, owner_id, namespace,
                       permission_mode, source_model, source_provider,
                       source_session, source_agent, federation_source,
                       federation_remote_updated, created, updated)
                    VALUES
                      (%s, %s, %s, %s, %s, %s,
                       %s, %s, 'federation', %s,
                       644, %s, %s,
                       %s, %s, %s,
                       %s, CURRENT_TIMESTAMP(6), %s)
                    """,
                    (
                        local_id,
                        content,
                        _content_hash(content),
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
        except Exception as exc:
            if _is_unique_violation(exc):
                return False
            raise

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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE memories
                   SET content = %s,
                       content_hash = %s,
                       category = %s,
                       subcategory = %s,
                       metadata = %s,
                       verbatim_content = %s,
                       quality_rating = %s,
                       namespace = %s,
                       federation_remote_updated = %s,
                       updated = %s
                 WHERE id = %s
                   AND deleted_at IS NULL
                   AND (
                        federation_remote_updated IS NULL
                        OR federation_remote_updated < %s
                   )
                """,
                (
                    content,
                    _content_hash(content),
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
            return int(cursor.rowcount or 0) > 0

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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE memories
                   SET consolidated_into = %s,
                       consolidated_at = COALESCE(%s, CURRENT_TIMESTAMP(6)),
                       permission_mode = 400,
                       metadata = JSON_SET(
                           COALESCE(CAST(NULLIF(metadata, '') AS JSON), JSON_OBJECT()),
                           '$.federation_consolidation',
                           JSON_OBJECT(
                               'remote_id', %s,
                               'remote_consolidated_into', %s,
                               'peer', %s
                           )
                       )
                 WHERE id = %s
                   AND deleted_at IS NULL
                   AND (consolidated_into IS NULL OR consolidated_into <> %s)
                   AND EXISTS (
                       SELECT 1 FROM memories
                        WHERE id = %s AND deleted_at IS NULL
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
            return int(cursor.rowcount or 0) > 0

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        local_id = f"fed:{peer_name}:{memory_id}"
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE memories
                   SET deleted_at = CURRENT_TIMESTAMP(6)
                 WHERE id IN (%s, %s)
                   AND federation_source = %s
                   AND deleted_at IS NULL
                """,
                (memory_id, local_id, peer_name),
            )
            return int(cursor.rowcount or 0)

    async def upsert_federated_memory(
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
        peer_name: str,
        remote_updated: Any,
        source_model: str | None = None,
        source_provider: str | None = None,
        source_session: str | None = None,
        source_agent: str | None = None,
    ) -> bool:
        inserted = await self.insert_federated_memory(
            tx,
            local_id=local_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=metadata_json,
            verbatim_content=verbatim_content,
            quality_rating=quality_rating,
            namespace=namespace,
            source_model=source_model,
            source_provider=source_provider,
            source_session=source_session,
            source_agent=source_agent,
            peer_name=peer_name,
            remote_updated=remote_updated,
        )
        if inserted:
            return True
        return await self.update_federated_memory_if_newer(
            tx,
            local_id=local_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=metadata_json,
            verbatim_content=verbatim_content,
            quality_rating=quality_rating,
            namespace=namespace,
            remote_updated=remote_updated,
        )

    async def fetch_federation_peers(self, tx: Transaction) -> list[Row]:
        return await self.list_peers(tx)

    async def upsert_federation_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        await self.upsert_peer(tx, peer_id=peer_id, base_url=base_url, name=name, enabled=enabled)

    async def delete_federation_peer(self, tx: Transaction, peer_id: str) -> bool:
        return await self.delete_peer(tx, peer_id)

    async def fetch_local_memories_for_push(
        self,
        tx: Transaction,
        *,
        peer_name: str | None = None,
        since_updated: Any | None = None,
        limit: int = 100,
    ) -> list[Row]:
        where = [
            "federation_source IS NULL",
            "deleted_at IS NULL",
            "archived_at IS NULL",
            "consolidated_into IS NULL",
            "(MOD(permission_mode, 10)) >= 4",
        ]
        params: list[Any] = []
        if peer_name is not None:
            where.append("(federation_push_peer IS NULL OR federation_push_peer = %s)")
            params.append(peer_name)
        if since_updated is not None:
            where.append("updated > %s")
            params.append(since_updated)
        params.append(limit)
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT id, content, category, subcategory, metadata, owner_id,
                       namespace, permission_mode, created, updated
                  FROM memories
                 WHERE {" AND ".join(where)}
                 ORDER BY updated ASC, id ASC
                 LIMIT %s
                """,
                params,
            )
            return await _fetch_all_dicts(cursor)

    async def mark_memories_pushed(
        self,
        tx: Transaction,
        *,
        peer_name: str,
        memory_ids: Sequence[str],
    ) -> int:
        if not memory_ids:
            return 0
        placeholders = ", ".join(["%s"] * len(memory_ids))
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                UPDATE memories
                   SET federation_last_pushed_at = CURRENT_TIMESTAMP(6),
                       federation_push_peer = %s
                 WHERE id IN ({placeholders})
                   AND federation_source IS NULL
                   AND deleted_at IS NULL
                """,
                [peer_name, *memory_ids],
            )
            return int(cursor.rowcount or 0)


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
                    value = VALUES(value),
                    updated = CURRENT_TIMESTAMP(6),
                    version = version + 1,
                    deleted_at = NULL
                """,
                (owner_id, namespace, key, value),
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


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class MysqlAuditChainRepository(AuditChainRepository):
    """MySQL implementation of the v6.2 per-memory audit chain."""

    async def get_latest_audit_entry(self, tx: Transaction, memory_id: bytes) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                  FROM memory_audit_chain
                 WHERE memory_id = %s
                 ORDER BY signed_at DESC
                 LIMIT 1
                """,
                (memory_id,),
            )
            return await _fetchone_dict(cursor)

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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO memory_audit_chain (
                    entry_id, memory_id, prev_entry_id, prev_entry_hash,
                    op, payload_hash, writer_id, writer_pubkey,
                    signature, signed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT entry_id, signature, signed_at
                  FROM memory_audit_chain
                 WHERE global_root IS NULL
                   AND signed_at <= DATE_SUB(NOW(6), INTERVAL %s SECOND)
                 ORDER BY signed_at ASC, entry_id ASC
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
                """,
                (max_window_seconds, limit),
            )
            return await _fetch_all_dicts(cursor)

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
        async with tx.conn.cursor() as cursor:
            for offset, entry_id in enumerate(entry_ids):
                await cursor.execute(
                    """
                    UPDATE memory_audit_chain
                       SET global_root = %s,
                           global_seq = %s
                     WHERE entry_id = %s
                    """,
                    (global_root, starting_seq + offset, entry_id),
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
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO memory_audit_roots (
                    global_root, window_start, window_end, entry_count,
                    root_signature, signer_pubkey, sealed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (global_root, window_start, window_end, entry_count, root_signature, signer_pubkey, sealed_at),
            )

    async def list_window_entries(self, tx: Transaction, global_root: bytes) -> list[Row]:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT entry_id, memory_id, signature, signed_at,
                       global_seq, payload_hash, op
                  FROM memory_audit_chain
                 WHERE global_root = %s
                 ORDER BY signed_at ASC, entry_id ASC
                """,
                (global_root,),
            )
            return await _fetch_all_dicts(cursor)

    async def get_audit_entry_by_id(self, tx: Transaction, entry_id: bytes) -> Row | None:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                  FROM memory_audit_chain
                 WHERE entry_id = %s
                """,
                (entry_id,),
            )
            return await _fetchone_dict(cursor)

    async def get_chain_stats(self, tx: Transaction) -> dict:
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT COUNT(*) AS total_entries,
                       SUM(CASE WHEN global_root IS NULL THEN 1 ELSE 0 END) AS unsealed_count,
                       MIN(CASE WHEN global_root IS NULL THEN signed_at ELSE NULL END) AS oldest_unsealed_signed_at
                  FROM memory_audit_chain
                """
            )
            chain_stats = await _fetchone_dict(cursor) or {}
            await cursor.execute(
                """
                SELECT COUNT(*) AS sealed_root_count,
                       MAX(sealed_at) AS last_sealed_at
                  FROM memory_audit_roots
                """
            )
            root_stats = await _fetchone_dict(cursor) or {}
        return {
            "total_entries": int(chain_stats.get("total_entries") or 0),
            "unsealed_count": int(chain_stats.get("unsealed_count") or 0),
            "oldest_unsealed_signed_at": _iso_or_none(chain_stats.get("oldest_unsealed_signed_at")),
            "sealed_root_count": int(root_stats.get("sealed_root_count") or 0),
            "last_sealed_at": _iso_or_none(root_stats.get("last_sealed_at")),
        }

    async def get_latest_audit_entries_batch(self, tx: Transaction, memory_ids: list[bytes]) -> dict[bytes, Row]:
        if not memory_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(memory_ids))
        async with tx.conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                  FROM (
                        SELECT mac.*,
                               ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY signed_at DESC) AS rn
                          FROM memory_audit_chain mac
                         WHERE memory_id IN ({placeholders})
                       ) ranked
                 WHERE rn = 1
                """,
                list(memory_ids),
            )
            rows = await _fetch_all_dicts(cursor)
        return {row["memory_id"]: row for row in rows}


# ── Backend facade ────────────────────────────────────────────────────────────


class MysqlBackend:  # P14: PersistenceBackend is now a Union type alias; align with SqliteBackend/OracleBackend/Db2Backend/PostgresBackend bare-class pattern
    """MySQL 9.0+ persistence facade backed by an aiomysql connection pool.

    Core memory, FTS, VECTOR search, KG, versioning, branches, compression,
    federation, state key-value, and audit-chain surfaces are implemented.
    OAuth, Sessions, and Consultations repositories remain intentionally
    unwired pending schema-unification work. Webhooks are currently unsupported;
    callers should use ``supports_webhooks`` before dispatching.

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
        self._audit_chain_repo = MysqlAuditChainRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def capabilities(self) -> set[str]:
        return {CORE_CAPABILITY, STATE_CAPABILITY, FEDERATION_CAPABILITY, AUDIT_CAPABILITY}

    @property
    def capability_details(self) -> set[str]:
        return {
            *MYSQL_CAPABILITY_DETAILS,
            KG_CAPABILITY,
            COMPRESSION_QUEUE_CAPABILITY,
            STATE_DETAIL_CAPABILITY,
            AUDIT_DETAIL_CAPABILITY,
        }

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

    @property
    def audit_chain(self) -> AuditChainRepository:
        return self._audit_chain_repo

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
                await _ensure_mysql_columns(
                    conn,
                    "memories",
                    {
                        "federation_remote_updated": "federation_remote_updated DATETIME(6)",
                        "consolidated_at": "consolidated_at DATETIME(6)",
                        "federation_last_pushed_at": "federation_last_pushed_at DATETIME(6)",
                        "federation_push_peer": "federation_push_peer VARCHAR(512)",
                    },
                )
                await _ensure_mysql_columns(
                    conn,
                    "federation_peers",
                    {
                        "auth_token": "auth_token TEXT",
                        "api_key": "api_key TEXT",
                        "namespace_filter": "namespace_filter JSON",
                        "category_filter": "category_filter JSON",
                        "sync_interval_secs": "sync_interval_secs INT NOT NULL DEFAULT 300",
                        "last_sync_cursor": "last_sync_cursor TEXT",
                        "cursor_updated": "cursor_updated TEXT",
                        "last_error": "last_error TEXT",
                        "last_error_at": "last_error_at TIMESTAMP(6) NULL",
                        "total_pulled": "total_pulled INT NOT NULL DEFAULT 0",
                        "compat_mode": "compat_mode VARCHAR(32) NOT NULL DEFAULT 'strict'",
                        "peer_mnemos_version": "peer_mnemos_version VARCHAR(128)",
                        "last_schema_check_at": "last_schema_check_at TIMESTAMP(6) NULL",
                        "copy_embeddings": "copy_embeddings BOOLEAN NOT NULL DEFAULT FALSE",
                        "created": "created TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)",
                        "updated": "updated TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)",
                    },
                )
                await _ensure_mysql_columns(
                    conn,
                    "federation_sync_log",
                    {
                        "direction": "direction VARCHAR(16) NOT NULL DEFAULT 'pull'",
                        "status": "status VARCHAR(32) NOT NULL DEFAULT 'started'",
                        "started_at": "started_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)",
                        "finished_at": "finished_at TIMESTAMP(6) NULL",
                        "memories_pulled": "memories_pulled INT NOT NULL DEFAULT 0",
                        "memories_new": "memories_new INT NOT NULL DEFAULT 0",
                        "memories_updated": "memories_updated INT NOT NULL DEFAULT 0",
                        "records_seen": "records_seen INT NOT NULL DEFAULT 0",
                        "records_written": "records_written INT NOT NULL DEFAULT 0",
                        "cursor_before": "cursor_before TEXT",
                        "cursor_after": "cursor_after TEXT",
                    },
                )
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
    "MysqlAuditChainRepository",
    "MysqlVersionRepository",
    "MysqlWebhookRepository",
    "create_mysql_pool",
]
