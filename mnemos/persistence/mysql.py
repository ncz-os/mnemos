"""MySQL 9.0+ persistence backend for MNEMOS.

Uses ``aiomysql`` (asyncio wrapper around PyMySQL) with a native async
connection pool.  MySQL 9.0+ is required for the native ``VECTOR``
column type and ``VECTOR_DISTANCE`` / ``TO_VECTOR`` functions
used by semantic search.

Positioning — this is MNEMOS's **Enterprise / cloud** MySQL-family vector
backend. The ``VECTOR_DISTANCE`` function this backend depends on is available
on **MySQL Enterprise / HeatWave** (and managed services built on them — AWS
RDS/Aurora MySQL, HeatWave) but is NOT present in MySQL Community Edition
(verified absent through 9.3). Self-hosted / open-source operators who want
free native vector search should use the ``mariadb`` backend (MariaDB Community
ships it) — see ``mnemos.persistence.mariadb``.

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
import uuid
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.core.auth_context import UserContext
from mnemos.core import eligibility as _eligibility
from mnemos.core.config import embedding_dim_env, runtime_env_value_stripped
from mnemos.persistence.base import (
    BackendCapabilityMissing,
    BranchRepository,
    CompressionStatsRow,
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
    OAuthRepository,
    STATE_CAPABILITY,
    STATE_DETAIL_CAPABILITY,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookDeliveryClaim,
    WebhookDeliveryOutcome,
    WebhookDeliveryRecord,
    WebhookFinalizationResult,
    WebhookRepository,
    WebhookSubscriptionRecord,
)
from mnemos.core import webhook_constants
from mnemos.persistence.hot_search import HotSearchMixin
from mnemos.persistence.mcp_oauth import MCPOAuthRepositoryMixin, oauth_utc
from mnemos.persistence.mysql_oauth import MysqlBrowserOAuthMixin
from mnemos.persistence.schema import split_postgres_statements
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


def _json_list_text(value: Any) -> str:
    return json.dumps(_json_list(value))


def _json_text(value: Any, default: Any = None) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                return json.dumps(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return value
    return json.dumps(value if value is not None else default)


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
    return (
        _coerce_date(row.get("last_recalled_at"))
        or _coerce_date(row.get("updated"))
        or _coerce_date(row.get("created"))
        or date.min
    )


def _boosted_rank_score_sort_key(row: Row, *, today: date, recency_weight: float) -> float:
    rank = _rank_score_sort_key(row)
    if not math.isfinite(rank):
        return math.inf
    age_days = max(0, (today - _recency_date(row)).days)
    return rank - recency_weight * (1.0 / (1.0 + age_days))


def _boosted_rank_supersession_sort_key(row: Row, *, today: date, recency_weight: float) -> tuple[bool, float]:
    superseded = isinstance(row, dict) and bool(row.get("superseded_by") or row.get("consolidated_into"))
    return superseded, _boosted_rank_score_sort_key(row, today=today, recency_weight=recency_weight)


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

    def _with_exclusions(clause: str, params: list[Any]) -> tuple[str, list[Any]]:
        excl = tuple(visibility.exclude_namespaces or ())
        if not excl:
            return clause, params
        placeholders = ", ".join(["%s"] * len(excl))
        excl_clause = f"({p}namespace IS NULL OR {p}namespace NOT IN ({placeholders}))"
        clause = f"({clause}) AND {excl_clause}" if clause else excl_clause
        return clause, params + list(excl)

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return _with_exclusions("", [])
        return _with_exclusions(f"{p}namespace = %s", [visibility.namespace])

    if visibility.namespace is None:
        return "1=0", []

    if visibility.scope == VisibilityScope.OWN_ONLY:
        return _with_exclusions(
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

    return _with_exclusions(
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


def _split_mysql_statements(sql: str) -> list[str]:
    """Statement splitter that understands MySQL/MariaDB trigger bodies.

    The shared ``split_postgres_statements`` helper in ``mnemos.persistence.schema``
    only handles plpgsql ``$$ ... $$`` dollar-tag blocks; it does not understand
    MySQL ``CREATE TRIGGER ... BEGIN ... END`` bodies, which contain their own
    internal ``;`` separators.  Without BEGIN/END tracking, the splitter chops a
    multi-statement trigger body in half and the engine rejects the partial
    DDL with a syntax error.

    This splitter is the same as ``_split_semicolon_statements`` plus one rule:
    when a top-level statement opens a ``BEGIN ... END`` block, keep consuming
    characters until the matching ``END`` closes the body at depth 0 before
    yielding the buffer as one statement.  BEGIN keywords inside string
    literals, line/block comments, or already-open nested BEGIN blocks do not
    affect the open/close counter.  Composite END tokens (``END IF``, ``END
    LOOP``, ``END CASE``, ``END WHILE``, ``END REPEAT``) never close a body.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_line_comment = False
    in_block_comment = False
    in_single = False
    in_double = False
    begin_depth = 0
    i = 0
    length = len(sql)
    composite_enders = ("IF", "LOOP", "CASE", "WHILE", "REPEAT")

    def _is_word_char(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    def _match_word(idx: int, word: str) -> bool:
        end = idx + len(word)
        if end > length:
            return False
        if sql[idx:end].upper() != word:
            return False
        prev = sql[idx - 1] if idx > 0 else ""
        nxt_char = sql[end] if end < length else ""
        return not _is_word_char(prev) and not _is_word_char(nxt_char)

    while i < length:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if in_line_comment:
            buffer.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_single:
            buffer.append(ch)
            if ch == "'" and nxt == "'":
                buffer.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buffer.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            buffer.extend((ch, nxt))
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            buffer.extend((ch, nxt))
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            buffer.append(ch)
            in_single = True
            i += 1
            continue
        if ch == '"':
            buffer.append(ch)
            in_double = True
            i += 1
            continue

        # Inside an open BEGIN block: track depth, ignore outer semicolons.
        if begin_depth > 0:
            if _match_word(i, "BEGIN"):
                begin_depth += 1
                buffer.append("BEGIN")
                i += 5
                continue
            if _match_word(i, "END"):
                # Composite END trailers (``END IF``, ``END LOOP``, ...) do
                # NOT close the body. Only a bare ``END`` decrements depth.
                tail_idx = i + 3
                while tail_idx < length and sql[tail_idx] in " \t\r\n":
                    tail_idx += 1
                is_composite = False
                for kw in composite_enders:
                    if _match_word(tail_idx, kw):
                        # Copy the full ``END <kw>`` token verbatim and step past.
                        kw_end = tail_idx + len(kw)
                        buffer.append(sql[i:kw_end])
                        i = kw_end
                        is_composite = True
                        break
                if is_composite:
                    continue
                begin_depth -= 1
                buffer.append("END")
                i += 3
                if begin_depth == 0:
                    # Consume trailing whitespace + the statement terminator.
                    while i < length and sql[i] in " \t\r\n":
                        buffer.append(sql[i])
                        i += 1
                    if i < length and sql[i] == ";":
                        buffer.append(";")
                        i += 1
                    statement = "".join(buffer).strip()
                    if statement and _has_executable_mysql_sql(statement):
                        statements.append(statement)
                    buffer = []
                continue
            buffer.append(ch)
            i += 1
            continue

        # Top-level: detect BEGIN (opens body) vs ; (ends statement).
        if _match_word(i, "BEGIN"):
            begin_depth = 1
            buffer.append("BEGIN")
            i += 5
            continue

        if ch == ";":
            statement = "".join(buffer).strip()
            if statement and _has_executable_mysql_sql(statement):
                statements.append(statement)
            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail and _has_executable_mysql_sql(tail):
        statements.append(tail)
    return statements


def _has_executable_mysql_sql(statement: str) -> bool:
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


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

_DDL_DELETION_REQUESTS = """\
CREATE TABLE IF NOT EXISTS deletion_requests (
    id VARCHAR(64) NOT NULL DEFAULT (UUID()),
    target_user_id VARCHAR(256) NOT NULL,
    target_namespace VARCHAR(256),
    requested_by VARCHAR(256) NOT NULL,
    requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    confirmed_at DATETIME(6),
    status VARCHAR(32) NOT NULL DEFAULT 'requested',
    notes TEXT,
    soft_deleted_at DATETIME(6),
    restore_by DATETIME(6),
    hard_deleted_at DATETIME(6),
    restored_at DATETIME(6),
    PRIMARY KEY (id),
    INDEX idx_deletion_requests_claim (status, confirmed_at, requested_at),
    INDEX idx_deletion_requests_target (target_user_id, target_namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MEMORY_ARCHIVE = """\
CREATE TABLE IF NOT EXISTS memory_archive (
    id VARCHAR(64) NOT NULL,
    archived_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    archived_by VARCHAR(256) NOT NULL DEFAULT 'system:persephone',
    compressed_content LONGBLOB NOT NULL,
    compression_algo VARCHAR(32) NOT NULL DEFAULT 'zstd',
    original_size_bytes BIGINT NOT NULL,
    compressed_size_bytes BIGINT NOT NULL,
    schema_version INT NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    INDEX idx_memory_archive_archived_at (archived_at),
    CONSTRAINT fk_memory_archive_memory FOREIGN KEY (id) REFERENCES memories(id) ON DELETE RESTRICT
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_DELETION_LOG = """\
CREATE TABLE IF NOT EXISTS deletion_log (
    id VARCHAR(64) NOT NULL, memory_id VARCHAR(64) NOT NULL, content_hash VARCHAR(64) NOT NULL,
    owner_id VARCHAR(256), namespace VARCHAR(256), requested_by VARCHAR(256) NOT NULL,
    requested_at DATETIME(6) NOT NULL, executed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    request_kind VARCHAR(32) NOT NULL, reason TEXT, source JSON, PRIMARY KEY (id),
    INDEX idx_deletion_log_memory (memory_id), INDEX idx_deletion_log_owner_ns (owner_id, namespace)
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

_DDL_MEMORY_BRANCHES = """\
CREATE TABLE IF NOT EXISTS memory_branches (
    memory_id       VARCHAR(64)  NOT NULL,
    name            VARCHAR(128) NOT NULL,
    head_version_id VARCHAR(64),
    created_by      VARCHAR(256),
    created_at      TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at      TIMESTAMP(6) NULL,
    PRIMARY KEY (memory_id, name),
    INDEX idx_memory_branches_memory (memory_id),
    INDEX idx_memory_branches_head (head_version_id),
    CONSTRAINT fk_memory_branches_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_branches_head
        FOREIGN KEY (head_version_id) REFERENCES memory_versions(id) ON DELETE SET NULL
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_ENTITIES = """\
CREATE TABLE IF NOT EXISTS entities (
    id          VARCHAR(64)  NOT NULL,
    entity_type VARCHAR(50)  NOT NULL,
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    metadata    JSON,
    owner_id    VARCHAR(256) NOT NULL DEFAULT 'default',
    namespace   VARCHAR(256) NOT NULL DEFAULT 'default',
    created     TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated     TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at  TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_entities_owner_namespace (owner_id, namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_SESSIONS = """\
CREATE TABLE IF NOT EXISTS sessions (
    id               VARCHAR(64)  NOT NULL,
    user_id          VARCHAR(256) NOT NULL,
    namespace        VARCHAR(256) NOT NULL DEFAULT 'default',
    created_at       TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_activity    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    model            VARCHAR(200),
    message_count    INT NOT NULL DEFAULT 0,
    total_tokens     INT NOT NULL DEFAULT 0,
    compression_tier INT NOT NULL DEFAULT 1,
    deleted_at       TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_sessions_user_namespace (user_id, namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_SESSION_MESSAGES = """\
CREATE TABLE IF NOT EXISTS session_messages (
    id                VARCHAR(64) NOT NULL,
    session_id        VARCHAR(64) NOT NULL,
    role              VARCHAR(20) NOT NULL,
    content           LONGTEXT NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at        TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_session_messages_session (session_id),
    CONSTRAINT fk_session_messages_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_SESSION_MEMORY_INJECTIONS = """\
CREATE TABLE IF NOT EXISTS session_memory_injections (
    id          VARCHAR(64) NOT NULL,
    session_id  VARCHAR(64) NOT NULL,
    memory_id   VARCHAR(64) NOT NULL,
    injected_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at  TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_session_memory_injections_session (session_id),
    CONSTRAINT fk_session_memory_injections_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_session_memory_injections_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Journal API table — canonical per-owner/per-namespace journal, matching
# PostgreSQL (db/migrations.sql + v3 ownership/namespace + v4_2 soft-delete).
# The MySQL family never carried these peripheral tables; add them here so the
# inherited journal/usage-ledger/category-decay repositories have schema.
_DDL_JOURNAL = """\
CREATE TABLE IF NOT EXISTS journal (
    id         VARCHAR(36)  NOT NULL,
    owner_id   VARCHAR(100) NOT NULL DEFAULT 'default',
    namespace  VARCHAR(100) NOT NULL DEFAULT 'default',
    entry_date DATE         NOT NULL,
    topic      VARCHAR(100),
    content    LONGTEXT,
    metadata   JSON,
    created    TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at TIMESTAMP(6) NULL,
    PRIMARY KEY (id),
    INDEX idx_journal_owner_namespace (owner_id, namespace),
    INDEX idx_journal_entry_date (entry_date),
    INDEX idx_journal_topic (topic),
    INDEX idx_journal_created (created)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# KNEMON token/cost usage ledger (mirrors db/migrations/0032_usage_ledger.sql).
_DDL_USAGE_LEDGER = """\
CREATE TABLE IF NOT EXISTS usage_ledger (
    id               BIGINT        NOT NULL AUTO_INCREMENT,
    provider         VARCHAR(255)  NOT NULL,
    model            VARCHAR(255)  NOT NULL,
    task_kind        VARCHAR(255)  NOT NULL,
    tokens_in        INT           NOT NULL,
    tokens_out       INT           NOT NULL,
    tokens_reasoning INT           NOT NULL DEFAULT 0,
    est_cost_usd     DECIMAL(12,6) NOT NULL,
    latency_ms       INT           NOT NULL,
    outcome          VARCHAR(32)   NOT NULL,
    caller_subsystem VARCHAR(255)  NOT NULL,
    tier             VARCHAR(255)  NOT NULL,
    session_id       VARCHAR(64),
    request_count    INT           NOT NULL DEFAULT 1,
    plan_window_id   VARCHAR(64),
    path_kind        VARCHAR(64)   NOT NULL DEFAULT 'api',
    subscription_amortized TINYINT NOT NULL DEFAULT 0,
    ts               TIMESTAMP(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX usage_ledger_ts_idx (ts),
    INDEX usage_ledger_model_idx (provider, model),
    INDEX usage_ledger_session_idx (session_id),
    INDEX usage_ledger_window_idx (plan_window_id),
    CONSTRAINT ck_usage_ledger_tokens_in_nonneg CHECK (tokens_in >= 0),
    CONSTRAINT ck_usage_ledger_tokens_out_nonneg CHECK (tokens_out >= 0),
    CONSTRAINT ck_usage_ledger_tokens_reasoning_nonneg CHECK (tokens_reasoning >= 0),
    CONSTRAINT ck_usage_ledger_est_cost_nonneg CHECK (est_cost_usd >= 0),
    CONSTRAINT ck_usage_ledger_latency_nonneg CHECK (latency_ms >= 0),
    CONSTRAINT ck_usage_ledger_outcome CHECK (outcome IN ('ok','err','timeout'))
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Per-category temporal-decay config (mirrors db/migrations/0031_memory_category_decay.sql).
_DDL_CATEGORY_DECAY = """\
CREATE TABLE IF NOT EXISTS memory_category_decay (
    category       VARCHAR(64)   NOT NULL,
    half_life_days DECIMAL(10,2) NOT NULL,
    decay_kind     VARCHAR(16)   NOT NULL,
    floor          DECIMAL(5,4)  NOT NULL DEFAULT 0,
    PRIMARY KEY (category),
    CONSTRAINT ck_memory_category_decay_kind CHECK (decay_kind IN ('exponential','sigmoid','none')),
    CONSTRAINT ck_memory_category_decay_floor CHECK (floor >= 0 AND floor <= 1),
    CONSTRAINT ck_memory_category_decay_halflife CHECK (half_life_days > 0)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Seed the canonical decay defaults (idempotent — INSERT IGNORE on the PK).
_DDL_CATEGORY_DECAY_SEED = """\
INSERT IGNORE INTO memory_category_decay (category, half_life_days, decay_kind, floor) VALUES
    ('feedback', 365, 'exponential', 0.5),
    ('rules', 730, 'exponential', 0.7),
    ('user', 365, 'exponential', 0.6),
    ('reference', 180, 'exponential', 0.3),
    ('project', 60, 'exponential', 0.05),
    ('facts', 90, 'exponential', 0.2),
    ('infrastructure', 30, 'exponential', 0.1),
    ('credentials', 14, 'sigmoid', 0.0),
    ('working', 7, 'exponential', 0.0),
    ('(default)', 180, 'exponential', 0.1)
"""

_INIT_DDLS = [
    _DDL_MEMORIES,
    _DDL_DELETION_REQUESTS,
    _DDL_DELETION_LOG,
    _DDL_MEMORY_ARCHIVE,
    _DDL_FEDERATION_PEERS,
    _DDL_FEDERATION_SYNC_LOG,
    _DDL_MEMORY_VERSIONS,
    _DDL_MEMORY_BRANCHES,
    _DDL_ENTITIES,
    _DDL_SESSIONS,
    _DDL_SESSION_MESSAGES,
    _DDL_SESSION_MEMORY_INJECTIONS,
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
    _DDL_JOURNAL,
    _DDL_USAGE_LEDGER,
    _DDL_CATEGORY_DECAY,
    _DDL_CATEGORY_DECAY_SEED,
]


async def _ensure_mysql_oauth_schema(conn: Any) -> None:
    """Provision OAuth tables on the node's existing MySQL-family connection."""
    directory = Path(__file__).resolve().parents[1] / "db_migrations" / "migrations_mysql"
    async with conn.cursor() as cursor:
        for name in ("0052_oauth_repository.sql", "0053_mcp_oauth.sql"):
            for statement in split_postgres_statements((directory / name).read_text(encoding="utf-8")):
                await cursor.execute(statement)


# ── webhook DDL (item 5) ────────────────────────────────────────────────────


_DDL_WEBHOOK_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id              VARCHAR(64)   NOT NULL DEFAULT (UUID()),
    url             TEXT         NOT NULL,
    events          JSON         NOT NULL,
    secret          TEXT         NOT NULL,
    description     TEXT         NULL,
    owner_id        VARCHAR(256) NOT NULL DEFAULT 'default',
    namespace       VARCHAR(256) NOT NULL DEFAULT 'default',
    created         DATETIME(6)  NOT NULL DEFAULT NOW(6),
    revoked         TINYINT(1)   NOT NULL DEFAULT 0,
    revoked_at      DATETIME(6)  NULL,
    PRIMARY KEY (id),
    INDEX idx_webhook_subscriptions_owner (owner_id, namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


_DDL_WEBHOOK_DELIVERIES = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                VARCHAR(64)   NOT NULL DEFAULT (UUID()),
    subscription_id   VARCHAR(64)   NOT NULL,
    event_type        VARCHAR(256)  NOT NULL,
    payload           LONGTEXT      NOT NULL,
    payload_hash      VARCHAR(64)   NOT NULL,
    attempt_num       INT           NOT NULL DEFAULT 1,
    status            VARCHAR(32)   NOT NULL DEFAULT 'pending',
    response_status   INT           NULL,
    response_body     LONGTEXT      NULL,
    error             TEXT          NULL,
    scheduled_at      DATETIME(6)   NOT NULL DEFAULT NOW(6),
    delivered_at      DATETIME(6)   NULL,
    created           DATETIME(6)   NOT NULL DEFAULT NOW(6),
    lease_token       VARCHAR(64)   NULL,
    lease_expires_at  DATETIME(6)   NULL,
    writer_revision   INT           NOT NULL DEFAULT 1,
    status_updated_at DATETIME(6)   NOT NULL DEFAULT NOW(6),
    superseded        TINYINT(1)    NOT NULL DEFAULT 0,
    live_chain_key    VARCHAR(768)  GENERATED ALWAYS AS (
        CASE
            WHEN status IN ('pending', 'retrying') AND superseded = 0
            THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash, '|', attempt_num)
            ELSE NULL
        END
    ) STORED,
    succeeded_chain_key VARCHAR(768) GENERATED ALWAYS AS (
        CASE
            WHEN status = 'succeeded'
            THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash)
            ELSE NULL
        END
    ) STORED,
    PRIMARY KEY (id),
    CONSTRAINT fk_webhook_deliveries_subscription
        FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


_DDL_WEBHOOK_DELIVERIES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription "
    "ON webhook_deliveries(subscription_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending "
    "ON webhook_deliveries(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at "
    "ON webhook_deliveries(lease_expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt "
    "ON webhook_deliveries(live_chain_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain "
    "ON webhook_deliveries(succeeded_chain_key)",
)


_DDL_WEBHOOK_TRIGGERS = """
DROP TRIGGER IF EXISTS webhook_deliveries_set_status_updated_at;
CREATE TRIGGER webhook_deliveries_set_status_updated_at
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
  SET NEW.status_updated_at = IF(OLD.status <> NEW.status, NOW(6), OLD.status_updated_at);

DROP TRIGGER IF EXISTS webhook_deliveries_enforce_succeeded_terminal;
CREATE TRIGGER webhook_deliveries_enforce_succeeded_terminal
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
BEGIN
  IF OLD.status = 'succeeded' AND NEW.status <> 'succeeded' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'webhook_deliveries: cannot transition status away from succeeded';
  END IF;
END;
"""


async def _ensure_mysql_webhook_schema(conn: Any) -> None:
    """Provision webhook_subscriptions + webhook_deliveries and their
    triggers on first open.  Splits the SQL with ``_split_mysql_statements``
    so the trigger ``BEGIN ... END`` body survives intact.
    """
    async with conn.cursor() as cursor:
        for ddl in (
            _DDL_WEBHOOK_SUBSCRIPTIONS,
            _DDL_WEBHOOK_DELIVERIES,
            *_DDL_WEBHOOK_DELIVERIES_INDEXES,
            _DDL_WEBHOOK_TRIGGERS,
        ):
            for statement in _split_mysql_statements(ddl):
                await cursor.execute(statement)


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
            "Install it with: pip install 'mnemos-core[mysql]'"
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


def _mysql_tx(tx: Transaction) -> _MysqlTransaction:
    if not isinstance(tx, _MysqlTransaction):
        raise TypeError("MySQL repositories require a _MysqlTransaction")
    return tx


# ── Memory repository ─────────────────────────────────────────────────────────


class MysqlMemoryRepository(HotSearchMixin, MemoryRepository):
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
        exclude_superseded: bool = False,
    ) -> tuple[list[Row], int]:
        conn = tx.conn
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = ["m.deleted_at IS NULL"]
        params: list[Any] = []
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if exclude_superseded:
            where.append("m.consolidated_into IS NULL")
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
                       m.recall_count, m.last_recalled_at, m.consolidated_into
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
            "consolidated_into",
            "namespace",
            "updated",
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

        safe_fields = {k: v for k, v in fields.items() if k in self._UPDATABLE_FIELDS and k != "updated"}
        if not safe_fields:
            if "updated" not in fields or fields.get("updated") is None:
                return await self.get_memory(tx, memory_id, visibility=visibility)
        set_cols = ", ".join(f"{col} = %s" for col in safe_fields)
        set_vals = list(safe_fields.values())
        if "updated" in fields and fields.get("updated") is not None:
            set_cols = f"{set_cols}, updated = %s" if set_cols else "updated = %s"
            set_vals.append(fields["updated"])
        else:
            set_cols = f"{set_cols}, updated = NOW(6)" if set_cols else "updated = NOW(6)"

        async with conn.cursor() as cursor:
            await cursor.execute(
                f"UPDATE memories m SET {set_cols} WHERE {' AND '.join(where)}",
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
                f"UPDATE memories m SET deleted_at = COALESCE(deleted_at, NOW(6)), updated = NOW(6) WHERE {' AND '.join(where)}",
                params,
            )
            return row if cursor.rowcount else None

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
        exclude_superseded: bool = False,
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
        if exclude_superseded:
            where.append("m.consolidated_into IS NULL")
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
                           m.recall_count, m.last_recalled_at, m.consolidated_into,
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
            rows.sort(key=lambda row: _boosted_rank_supersession_sort_key(row, today=today, recency_weight=w))
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
                       m.recall_count, m.last_recalled_at, m.consolidated_into,
                       FROM_VECTOR(m.embedding) AS embedding_json
                  FROM memories m
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            raw_rows = await _fetch_all_dicts(cursor)

        today = datetime.now(timezone.utc).date()
        w = float(recency_weight)
        distances = self._cosine_rank_rows(
            query_vec,
            raw_rows,
            "embedding_json",
            extract_embedding=lambda value: json.loads(value) if value else None,
        )
        for row, dist in zip(raw_rows, distances):
            row.pop("embedding_json", None)
            row["rank_score"] = dist

        if boost_recency:
            raw_rows.sort(key=lambda row: _boosted_rank_supersession_sort_key(row, today=today, recency_weight=w))
        else:
            raw_rows.sort(key=_rank_score_sort_key)
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
        exclude_superseded: bool = False,
    ) -> list[Row]:
        vis_clause, vis_params = _render_visibility(visibility, table_alias="m")
        where = [
            "m.deleted_at IS NULL",
            "MATCH (m.content) AGAINST (%s IN BOOLEAN MODE)",
        ]
        params: list[Any] = [query]
        if not include_archived:
            where.append("m.archived_at IS NULL")
        if exclude_superseded:
            where.append("m.consolidated_into IS NULL")
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
                       m.recall_count, m.last_recalled_at, m.consolidated_into,
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

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        # Mirror the Oracle backend: re-create the READABLE filter from the
        # user context and delegate to get_memory (which applies _render_visibility).
        from mnemos.core.security import is_root

        namespace = None if is_root(user) else user.namespace
        visibility = VisibilityFilter.for_read(user, namespace=namespace)
        row = await self.get_memory(tx, memory_id, visibility=visibility, include_archived=True)
        if row is None:
            raise PermissionError("Memory not found")

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        # Simplified vs the Postgres recursive-CTE walk, matching the Oracle
        # backend: latest N versions on this branch, version_num DESC. Caller-side
        # assert_memory_readable enforces handler-level visibility.
        _ = user
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, memory_id, version_num, content, commit_hash,
                       parent_version_id, branch, snapshot_at, snapshot_by,
                       change_type, category, subcategory, owner_id, namespace
                  FROM memory_versions
                 WHERE memory_id = %s AND branch = %s AND deleted_at IS NULL
                 ORDER BY version_num DESC
                 LIMIT %s
                """,
                (memory_id, branch, limit),
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
        _ = user
        conn = tx.conn
        sql = (
            "SELECT content, version_num FROM memory_versions "
            "WHERE memory_id = %s AND commit_hash = %s AND deleted_at IS NULL"
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql, (memory_id, commit_a))
            row_a = await _fetchone_dict(cursor)
            await cursor.execute(sql, (memory_id, commit_b))
            row_b = await _fetchone_dict(cursor)
            return row_a, row_b

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        _ = user
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT commit_hash, version_num, branch, category, subcategory,
                       content, change_type, snapshot_at, snapshot_by
                  FROM memory_versions
                 WHERE memory_id = %s AND commit_hash = %s AND deleted_at IS NULL
                """,
                (memory_id, commit_hash),
            )
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
        conn = tx.conn
        where = ["deleted_at IS NULL"]
        params: list[Any] = []
        if effective_owner:
            where.append("owner_id = %s")
            params.append(effective_owner)
        if effective_ns:
            where.append("namespace = %s")
            params.append(effective_ns)
        if category:
            where.append("category = %s")
            params.append(category)
        sql = (
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, group_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            "metadata, verbatim_content, archived_at, consolidated_into, FROM_VECTOR(embedding) AS embedding "
            "FROM memories WHERE " + " AND ".join(where) + " "
            "ORDER BY created ASC, id ASC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return await _fetch_all_dicts(cursor)

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        ids = list(referenced_ids)
        if not ids:
            return []
        conn = tx.conn
        placeholders = ", ".join(["%s"] * len(ids))
        where = [f"id IN ({placeholders})", "deleted_at IS NULL"]
        params: list[Any] = list(ids)
        if scope_owner is not None:
            where.append("owner_id = %s")
            params.append(scope_owner)
        if scope_namespace is not None:
            where.append("namespace = %s")
            params.append(scope_namespace)
        sql = "SELECT id, owner_id, namespace FROM memories WHERE " + " AND ".join(where)
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return await _fetch_all_dicts(cursor)

    async def backfill_missing_content_hashes(
        self,
        tx: Transaction,
        *,
        batch_size: int = 500,
        apply: bool = False,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        conn = tx.conn
        async with conn.cursor() as cursor:
            if not apply:
                await cursor.execute("SELECT COUNT(*) AS cnt FROM memories WHERE content_hash IS NULL")
                row = await _fetchone_dict(cursor)
                return int((row or {}).get("cnt") or 0)
            await cursor.execute(
                """
                UPDATE memories m
                JOIN (
                    SELECT id
                      FROM memories
                     WHERE content_hash IS NULL
                     ORDER BY created ASC, id ASC
                     LIMIT %s
                ) candidates ON candidates.id = m.id
                   SET m.content_hash = SHA2(
                           REPLACE(REPLACE(COALESCE(m.content, ''), '\\r\\n', '\\n'), '\\r', '\\n'),
                           256
                       ),
                       m.updated = NOW(6)
                 WHERE m.content_hash IS NULL
                """,
                (int(batch_size),),
            )
            return int(cursor.rowcount or 0)

    async def fetch_duplicate_content_groups(self, *args: Any, **kwargs: Any) -> list[Row]:
        return await self.find_duplicate_content_groups(*args, **kwargs)

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = tx.conn
        where = [
            "deleted_at IS NULL",
            "archived_at IS NULL",
            "consolidated_into IS NULL",
            "content_hash IS NOT NULL",
        ]
        params: list[Any] = []
        if namespace is not None:
            where.append("namespace = %s")
            params.append(namespace)
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT owner_id, namespace, content_hash,
                       COUNT(*) AS duplicate_count,
                       GROUP_CONCAT(id ORDER BY created DESC, quality_rating DESC, id DESC SEPARATOR CHAR(31)) AS memory_ids,
                       SUBSTRING_INDEX(GROUP_CONCAT(id ORDER BY created DESC, quality_rating DESC, id DESC SEPARATOR CHAR(31)), CHAR(31), 1) AS keep_id,
                       SUBSTRING_INDEX(GROUP_CONCAT(id ORDER BY created DESC, quality_rating DESC, id DESC SEPARATOR CHAR(31)), CHAR(31), 1) AS canonical_id
                  FROM (
                    SELECT id, owner_id, namespace, content_hash, created, quality_rating,
                           REPLACE(REPLACE(COALESCE(content, ''), '\\r\\n', '\\n'), '\\r', '\\n') AS normalized_content
                      FROM memories
                     WHERE {" AND ".join(where)}
                  ) candidates
                 GROUP BY owner_id, namespace, content_hash, normalized_content
                HAVING COUNT(*) > 1
                 ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
                """,
                params,
            )
            rows = await _fetch_all_dicts(cursor)
        for row in rows:
            raw = row.get("memory_ids") or ""
            row["memory_ids"] = [part for part in str(raw).split("\x1f") if part]
            row["duplicate_count"] = int(row.get("duplicate_count") or 0)
        return rows

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        ids = list(duplicate_ids)
        if not ids:
            return 0
        conn = tx.conn
        placeholders = ", ".join(["%s"] * len(ids))
        async with conn.cursor() as cursor:
            # Canonical must exist and be active. Mirrors the Postgres EXISTS
            # guard; MySQL forbids referencing the UPDATE target table in a
            # subquery (error 1093), so the canonical check runs as its own SELECT.
            await cursor.execute(
                "SELECT 1 FROM memories WHERE id = %s AND deleted_at IS NULL "
                "AND archived_at IS NULL AND consolidated_into IS NULL",
                (canonical_id,),
            )
            if await cursor.fetchone() is None:
                return 0
            # Redirect duplicates to the canonical id + soft-delete, matching
            # Postgres/SQLite so federation consolidation tombstones can emit.
            await cursor.execute(
                f"""
                UPDATE memories
                   SET consolidated_into = %s,
                       consolidated_at = CURRENT_TIMESTAMP(6),
                       deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP(6)),
                       updated = CURRENT_TIMESTAMP(6)
                 WHERE id IN ({placeholders})
                   AND id <> %s
                   AND deleted_at IS NULL
                   AND archived_at IS NULL
                   AND consolidated_into IS NULL
                """,
                (canonical_id, *ids, canonical_id),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)


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
        # MySQL stores version JSON fields as JSON text; normalize Python
        # list/dict inputs internally so importers can pass backend-neutral
        # values just like they do for SQLite/Postgres/Oracle/Db2.
        merge_parents_json = _json_list_text(merge_parents)
        metadata_text = _json_text(metadata_json, {})
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
                        metadata_text,
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
                        merge_parents_json,
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
        # Exclude soft-deleted versions so a tombstoned higher version_num cannot
        # become the branch head (matches log/export/fetch paths; MariaDB inherits).
        conditions = ["deleted_at IS NULL", f"memory_id IN ({', '.join(['%s'] * len(memory_ids))})"]
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
    """MySQL/MariaDB WebhookRepository implementation.

    Implements the full 13-method ABC contract (item 5 of 12) by mirroring
    ``PostgresWebhookRepository``'s row-per-attempt semantics.  Notable
    dialect adaptations vs Postgres / SQLite:

    * ``RETURNING`` is not available in MySQL/MariaDB UPDATE statements;
      the repository SELECTs the affected rows separately after the
      ``UPDATE`` rather than relying on ``RETURNING``.
    * Partial unique indexes ``WHERE status IN (...)`` are emulated with
      generated columns whose value is NULL for terminal rows; MySQL and
      MariaDB unique indexes both permit multiple NULLs, so terminal rows
      may repeat while live rows still enforce one-attempt-per-chain.
    * ``SELECT ... FOR UPDATE SKIP LOCKED`` is supported by both MySQL
      8.0+ and MariaDB 10.6+ on InnoDB - the recovery claim query uses
      that primitive directly.
    * The Postgres advisory lock ``pg_advisory_xact_lock`` is replaced
      with a MySQL session-scoped named lock (``GET_LOCK`` /
      ``RELEASE_LOCK``); the transaction's ``_release_named_locks`` hook
      releases any lock acquired during finalize/guard.

    The ``MysqlBackend.webhooks`` accessor is intentionally kept failing
    closed with ``BackendCapabilityMissing``: the storage layer is now
    consistent with the ABC, but no MySQL/MariaDB delivery worker is
    wired up in production, and advertising delivery would strand rows.
    The 13-method implementation is verified by the conformance gate +
    a live MariaDB integration test (``tests/test_mysql_webhook_repository.py``).
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _row_count(self, cursor: Any) -> int:
        """Read the affected-row count from an aiomysql cursor.

        aiomysql exposes ``cursor.rowcount`` as a synchronous attribute set
        after the most recent ``execute()``; ``UPDATE ... ON DUPLICATE KEY
        UPDATE id=id`` returns 0 for matched-but-unchanged rows and 1 for
        a fresh insert, which is exactly what the ABC expects.
        """
        return int(getattr(cursor, "rowcount", 0) or 0)

    @staticmethod
    async def _db_now(conn: Any) -> datetime:
        """Read the database clock as a tz-aware UTC ``datetime``.

        MySQL ``NOW(6)`` is session-time-zone-relative; ``MysqlBackend.open``
        pins ``SET time_zone='+00:00'`` so the value is already UTC, but we
        still call ``astimezone(UTC)`` to defend against sessions that drift.
        Aliased to ``db_now`` so the shared ``_fetchone_dict`` (which
        lowercases column names) does not collide with the function name.
        """
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT NOW(6) AS db_now")
            row = await _fetchone_dict(cursor)
        value = row["db_now"] if row else None
        if not isinstance(value, datetime):
            raise RuntimeError(
                f"mysql: expected NOW(6) datetime from SELECT, got {value!r}"
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # ── subscription surface ─────────────────────────────────────────────────

    async def create_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        url: str,
        events: Sequence[str],
        secret: str,
        description: str | None,
        owner_id: str,
        namespace: str,
    ) -> WebhookSubscriptionRecord:
        conn = _mysql_tx(tx).conn
        events_json = json.dumps(list(events))
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO webhook_subscriptions
                    (id, url, events, secret, description, owner_id, namespace)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    subscription_id,
                    url,
                    events_json,
                    secret,
                    description,
                    owner_id,
                    namespace,
                ),
            )
            await cursor.execute(
                """
                SELECT id, url, events, description, owner_id, namespace,
                       created, revoked, revoked_at
                FROM webhook_subscriptions
                WHERE id = %s
                """,
                (subscription_id,),
            )
            row = await _fetchone_dict(cursor)
        if row is None:
            raise RuntimeError("mysql: webhook subscription insert returned no row")
        return _mysql_webhook_subscription(row)

    async def list_subscriptions(
        self,
        tx: Transaction,
        *,
        owner_id: str | None,
        namespace: str | None,
        include_revoked: bool,
        limit: int,
    ) -> list[WebhookSubscriptionRecord]:
        _validate_webhook_scope(owner_id, namespace, "list_subscriptions")
        conn = _mysql_tx(tx).conn
        conditions: list[str] = []
        params: list[Any] = []
        if not include_revoked:
            conditions.append("revoked = 0")
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
            conditions.append("namespace = %s")
            params.append(namespace)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(int(limit))
        sql = (
            "SELECT id, url, events, description, owner_id, namespace, "
            "created, revoked, revoked_at "
            "FROM webhook_subscriptions "
            f"{where} ORDER BY created DESC LIMIT %s"
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            rows = await _fetch_all_dicts(cursor)
        return [_mysql_webhook_subscription(row) for row in rows]

    async def get_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
    ) -> WebhookSubscriptionRecord | None:
        _validate_webhook_scope(owner_id, namespace, "get_subscription")
        conn = _mysql_tx(tx).conn
        conditions = ["id = %s"]
        params: list[Any] = [subscription_id]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
            conditions.append("namespace = %s")
            params.append(namespace)
        sql = (
            "SELECT id, url, events, description, owner_id, namespace, "
            "created, revoked, revoked_at FROM webhook_subscriptions WHERE "
            + " AND ".join(conditions)
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            row = await _fetchone_dict(cursor)
        return _mysql_webhook_subscription(row) if row is not None else None

    async def revoke_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
    ) -> bool:
        _validate_webhook_scope(owner_id, namespace, "revoke_subscription")
        conn = _mysql_tx(tx).conn
        conditions = ["id = %s", "revoked = 0"]
        params: list[Any] = [subscription_id]
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
            conditions.append("namespace = %s")
            params.append(namespace)
        sql = (
            "UPDATE webhook_subscriptions "
            "SET revoked = 1, revoked_at = NOW(6) "
            "WHERE " + " AND ".join(conditions)
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return (await self._row_count(cursor)) > 0

    async def list_deliveries(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
        limit: int,
    ) -> list[WebhookDeliveryRecord]:
        _validate_webhook_scope(owner_id, namespace, "list_deliveries")
        conn = _mysql_tx(tx).conn
        params: list[Any] = [subscription_id]
        scope = ""
        if owner_id is not None:
            scope = " AND s.owner_id = %s AND s.namespace = %s"
            params.extend((owner_id, namespace))
        params.append(int(limit))
        sql = (
            "SELECT d.id, d.subscription_id, d.event_type, d.payload, d.payload_hash, "
            "d.attempt_num, d.status, d.response_status, d.response_body, d.error, "
            "d.scheduled_at, d.delivered_at, d.created, d.status_updated_at, "
            "d.superseded, d.lease_token, d.lease_expires_at, d.writer_revision "
            "FROM webhook_deliveries d "
            "JOIN webhook_subscriptions s ON s.id = d.subscription_id "
            f"WHERE d.subscription_id = %s{scope} "
            "ORDER BY d.created DESC LIMIT %s"
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            rows = await _fetch_all_dicts(cursor)
        return [_mysql_webhook_delivery(row) for row in rows]

    # ── dispatch (outbox enqueue) ────────────────────────────────────────────

    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        conn = _mysql_tx(tx).conn
        conditions = ["revoked = 0"]
        params: list[Any] = []
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if namespace is not None:
            conditions.append("namespace = %s")
            params.append(namespace)
        sql_sub = (
            "SELECT id, url, owner_id, namespace, events FROM webhook_subscriptions WHERE "
            + " AND ".join(conditions)
        )
        async with conn.cursor() as cursor:
            await cursor.execute(sql_sub, tuple(params))
            subscriptions = await _fetch_all_dicts(cursor)
        body = json.dumps(
            {
                "event": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": payload,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_ids: list[str] = []
        async with conn.cursor() as cursor:
            for sub in subscriptions:
                # The events column is JSON; MySQL returns it as either a JSON
                # string or (when the connection is configured to deserialize)
                # a Python list.  Coerce to list before membership-test.
                raw_events = sub.get("events")
                if isinstance(raw_events, (bytes, bytearray)):
                    raw_events = raw_events.decode("utf-8")
                if isinstance(raw_events, str):
                    try:
                        sub_events = json.loads(raw_events)
                    except (TypeError, ValueError):
                        sub_events = []
                elif isinstance(raw_events, list):
                    sub_events = raw_events
                else:
                    sub_events = []
                if event_type not in sub_events:
                    continue
                delivery_id = str(uuid.uuid4())
                await cursor.execute(
                    """
                    INSERT INTO webhook_deliveries
                      (id, subscription_id, event_type, payload, payload_hash,
                       status, scheduled_at, writer_revision)
                    VALUES (%s, %s, %s, %s, %s, 'pending', NOW(6), %s)
                    """,
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

    # ── claim (one row) ──────────────────────────────────────────────────────

    async def claim_delivery(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
        lease_seconds: int,
        max_attempts: int,
        writer_revision: int,
    ) -> WebhookDeliveryClaim | None:
        """Mirror of ``PostgresWebhookRepository.claim_delivery``.

        MySQL/MariaDB have no ``UPDATE ... RETURNING`` so we perform the
        conditional UPDATE first, then SELECT the row back to assemble the
        ``WebhookDeliveryClaim`` (the live worker needs the subscription
        URL/secret alongside the delivery row to actually POST).

        The UPDATE uses ``NOW(6) + INTERVAL n SECOND`` for the lease
        expiry so the lease clock is the database clock - same as the
        Postgres ``clock_timestamp() + n INTERVAL`` form.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        conn = _mysql_tx(tx).conn
        # We can't reference NOW(6) in two places consistently without a
        # CTE-like construct (MySQL CTEs cannot be SELECTed in UPDATE for
        # multi-row use). Capture the current DB time once and pass it in.
        claim_now = await self._db_now(conn)
        update_sql = (
            """
            UPDATE webhook_deliveries
            SET lease_token = %s,
                lease_expires_at = %s + INTERVAL %s SECOND,
                status = CASE WHEN status = 'pending' THEN 'retrying' ELSE status END
            WHERE id = %s
              AND scheduled_at <= %s
              AND attempt_num <= %s
              AND superseded = 0
              AND status IN ('pending', 'retrying')
              AND (lease_token IS NULL OR lease_expires_at < %s)
              AND writer_revision = %s
              AND (
                status = 'pending'
                OR NOT EXISTS (
                  SELECT 1 FROM webhook_deliveries newer
                  WHERE newer.subscription_id = webhook_deliveries.subscription_id
                    AND newer.event_type = webhook_deliveries.event_type
                    AND newer.payload_hash = webhook_deliveries.payload_hash
                    AND newer.attempt_num > webhook_deliveries.attempt_num
                )
              )
            """
        )
        params = (
            lease_token,
            claim_now,
            int(lease_seconds),
            delivery_id,
            claim_now,
            int(max_attempts),
            claim_now,
            int(writer_revision),
        )
        async with conn.cursor() as cursor:
            await cursor.execute(update_sql, params)
            if (await self._row_count(cursor)) == 0:
                return None
            await cursor.execute(
                _MYSQL_WEBHOOK_CLAIM_SELECT + " WHERE d.id = %s",
                (delivery_id,),
            )
            row = await _fetchone_dict(cursor)
        if row is None:
            return None
        return _mysql_webhook_claim(row, lease_token, claim_now)

    # ── claim due (recovery) ─────────────────────────────────────────────────

    async def claim_due_deliveries(
        self,
        tx: Transaction,
        *,
        lease_token: str,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
        writer_revision: int,
    ) -> list[WebhookDeliveryClaim]:
        """Recovery-side batch claim.  Uses ``SELECT ... FOR UPDATE SKIP
        LOCKED`` (MySQL 8.0+/MariaDB 10.6+ on InnoDB) inside a derived
        table joined into the UPDATE, so competing workers can grab
        different rows without coordination.
        """
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        conn = _mysql_tx(tx).conn
        claim_now = await self._db_now(conn)
        # The ``FOR UPDATE SKIP LOCKED`` goes inside the derived table -
        # MySQL/MariaDB accept that pattern (the subquery is the locking
        # read; the outer UPDATE then takes row locks on the chosen ids).
        # We also force the optimizer to materialize the derived table via
        # ``STRAIGHT_JOIN`` so the SKIP LOCKED scope is exactly the
        # candidate set. The inner SELECT is a self-contained subquery
        # (it filters by the column values on its own rows); the
        # correlated ``peer`` / ``newer`` checks reference the same
        # ``webhook_deliveries`` table inside EXISTS clauses.
        update_sql = (
            """
            UPDATE webhook_deliveries d
            STRAIGHT_JOIN (
                SELECT w_inner.id, w_inner.subscription_id, w_inner.event_type,
                       w_inner.payload_hash, w_inner.attempt_num
                FROM webhook_deliveries w_inner
                WHERE w_inner.scheduled_at <= %s
                  AND w_inner.attempt_num <= %s
                  AND w_inner.status NOT IN ('succeeded', 'abandoned')
                  AND w_inner.superseded = 0
                  AND w_inner.status IN ('pending', 'retrying')
                  AND (w_inner.lease_token IS NULL OR w_inner.lease_expires_at < %s)
                  AND w_inner.writer_revision = %s
                  AND NOT EXISTS (
                    SELECT 1 FROM webhook_deliveries peer
                    WHERE peer.subscription_id = w_inner.subscription_id
                      AND peer.event_type = w_inner.event_type
                      AND peer.payload_hash = w_inner.payload_hash
                      AND peer.status = 'succeeded'
                  )
                  AND (
                    w_inner.status = 'pending'
                    OR NOT EXISTS (
                      SELECT 1 FROM webhook_deliveries newer
                      WHERE newer.subscription_id = w_inner.subscription_id
                        AND newer.event_type = w_inner.event_type
                        AND newer.payload_hash = w_inner.payload_hash
                        AND newer.attempt_num > w_inner.attempt_num
                    )
                  )
                ORDER BY w_inner.scheduled_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            ) AS cand ON cand.id = d.id
            SET d.lease_token = %s,
                d.lease_expires_at = %s + INTERVAL %s SECOND,
                d.status = CASE WHEN d.status = 'pending' THEN 'retrying' ELSE d.status END
            """
        )
        params = (
            claim_now,
            int(max_attempts),
            claim_now,
            int(writer_revision),
            int(limit),
            lease_token,
            claim_now,
            int(lease_seconds),
        )
        async with conn.cursor() as cursor:
            await cursor.execute(update_sql, params)
            # After the UPDATE, SELECT back the rows we now own so we
            # can build WebhookDeliveryClaim objects. The lease_token
            # projection in the SELECT lets us sanity-check that the
            # token we wrote round-tripped.
            await cursor.execute(
                _MYSQL_WEBHOOK_CLAIM_SELECT
                + """
                WHERE d.id IN (
                    SELECT id FROM webhook_deliveries
                    WHERE lease_token = %s AND lease_expires_at >= %s
                )
                ORDER BY d.scheduled_at
                """,
                (lease_token, claim_now),
            )
            rows = await _fetch_all_dicts(cursor)
        return [_mysql_webhook_claim(row, lease_token, claim_now) for row in rows]

    # ── guard (pre-send fence) ───────────────────────────────────────────────

    async def guard_delivery_claim(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
    ) -> bool:
        conn = _mysql_tx(tx).conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, subscription_id, event_type, payload_hash, attempt_num "
                "FROM webhook_deliveries WHERE id = %s",
                (delivery_id,),
            )
            delivery = await _fetchone_dict(cursor)
            if delivery is None:
                return False
        # Capture the database clock once so lease comparisons across
        # SELECT and UPDATE are consistent.
        now = await self._db_now(conn)
        # Acquire a chain-scoped MySQL session lock so finalize/guard serialize
        # per (subscription, event_type, payload_hash).  The transaction's
        # ``_release_named_locks`` hook releases it on commit/rollback.
        lock_name = _mysql_webhook_chain_lock_name(delivery)
        tx_obj = _mysql_tx(tx)
        held = tx_obj.named_lock_held(lock_name)
        if not held:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
                got = await cursor.fetchone()
            got_value = got[0] if got else 0
            if int(got_value or 0) != 1:
                return False
            tx_obj.hold_named_lock(lock_name)

        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM webhook_deliveries
                  WHERE id = %s
                    AND lease_token = %s
                    AND lease_expires_at > %s
                    AND status IN ('pending', 'retrying')
                    AND superseded = 0
                )
                """,
                (delivery_id, lease_token, now),
            )
            row = await cursor.fetchone()
        is_live = bool(row[0]) if row else False
        if not is_live:
            return False

        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM webhook_deliveries peer
                  WHERE peer.subscription_id = %s
                    AND peer.event_type = %s
                    AND peer.payload_hash = %s
                    AND peer.status = 'succeeded'
                    AND peer.id <> %s
                )
                """,
                (
                    delivery["subscription_id"],
                    delivery["event_type"],
                    delivery["payload_hash"],
                    delivery_id,
                ),
            )
            row = await cursor.fetchone()
        peer_succeeded = bool(row[0]) if row else False
        if peer_succeeded:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned',
                        superseded = 1,
                        response_status = NULL,
                        response_body = NULL,
                        error = 'succeeded-chain-peer-before-send',
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND lease_expires_at > %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (delivery_id, lease_token, now),
                )
            return False

        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM webhook_deliveries newer
                  WHERE newer.subscription_id = %s
                    AND newer.event_type = %s
                    AND newer.payload_hash = %s
                    AND newer.attempt_num > %s
                    AND newer.status IN ('pending', 'retrying')
                    AND newer.superseded = 0
                )
                """,
                (
                    delivery["subscription_id"],
                    delivery["event_type"],
                    delivery["payload_hash"],
                    delivery["attempt_num"],
                ),
            )
            row = await cursor.fetchone()
        live_successor = bool(row[0]) if row else False
        if live_successor:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned',
                        superseded = 1,
                        status_updated_at = NOW(6),
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND lease_expires_at > %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (delivery_id, lease_token, now),
                )
            return False
        return True

    # ── release (pre-send cancel) ─────────────────────────────────────────────

    async def release_delivery_claim(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
    ) -> bool:
        conn = _mysql_tx(tx).conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE webhook_deliveries
                SET lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                  AND lease_token = %s
                """,
                (delivery_id, lease_token),
            )
            updated = (await self._row_count(cursor)) > 0
            if not updated:
                return False
            await cursor.execute(
                "SELECT status, superseded FROM webhook_deliveries WHERE id = %s",
                (delivery_id,),
            )
            row = await _fetchone_dict(cursor)
        if row is None:
            return False
        if row["status"] not in ("pending", "retrying"):
            return False
        if bool(row["superseded"]):
            return False
        return True

    # ── finalize (atomic per-attempt terminalization) ─────────────────────────

    async def finalize_delivery(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
        outcome: WebhookDeliveryOutcome,
        max_attempts: int,
        backoff_schedule: Sequence[int],
    ) -> WebhookFinalizationResult:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        backoff_list = list(backoff_schedule)
        if len(backoff_list) < max_attempts - 1:
            raise ValueError(
                f"backoff_schedule must contain at least max_attempts-1 "
                f"({max_attempts - 1}) entries; got {len(backoff_list)}"
            )
        if any(delay <= 0 for delay in backoff_list):
            raise ValueError("backoff_schedule must contain positive delays")

        conn = _mysql_tx(tx).conn
        # Load the row under the chain lock so concurrent finalize calls
        # serialize per (subscription, event_type, payload_hash).
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT d.id, d.subscription_id, d.event_type, d.payload,
                       d.payload_hash, d.attempt_num, d.status,
                       s.url, s.secret, s.revoked, s.owner_id, s.namespace
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                WHERE d.id = %s
                """,
                (delivery_id,),
            )
            delivery = await _fetchone_dict(cursor)
        if delivery is None:
            return WebhookFinalizationResult(applied=False)
        lock_name = _mysql_webhook_chain_lock_name(delivery)
        tx_obj = _mysql_tx(tx)
        held = tx_obj.named_lock_held(lock_name)
        if not held:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
                got = await cursor.fetchone()
            got_value = got[0] if got else 0
            if int(got_value or 0) != 1:
                # Same chain is currently being finalized by another
                # worker; we lose the race and converge as no-op.
                return WebhookFinalizationResult(applied=False)
            tx_obj.hold_named_lock(lock_name)

        # Capture the database clock once so the success / failure /
        # exhaustion branches all compare against the same "now".
        now = await self._db_now(conn)

        # ── Success path ────────────────────────────────────────────────────
        if outcome.succeeded:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM webhook_deliveries peer
                      WHERE peer.subscription_id = %s
                        AND peer.event_type = %s
                        AND peer.payload_hash = %s
                        AND peer.status = 'succeeded'
                        AND peer.id <> %s
                    )
                    """,
                    (
                        delivery["subscription_id"],
                        delivery["event_type"],
                        delivery["payload_hash"],
                        delivery_id,
                    ),
                )
                row = await cursor.fetchone()
            peer_succeeded = bool(row[0]) if row else False
            if peer_succeeded:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'abandoned',
                            superseded = 1,
                            response_status = %s,
                            response_body = %s,
                            error = %s,
                            lease_token = NULL,
                            lease_expires_at = NULL
                        WHERE id = %s
                          AND lease_token = %s
                          AND status IN ('pending', 'retrying')
                          AND superseded = 0
                        """,
                        (
                            outcome.response_status,
                            outcome.response_body,
                            outcome.error,
                            delivery_id,
                            lease_token,
                        ),
                    )
                    applied = (await self._row_count(cursor)) > 0
                return WebhookFinalizationResult(applied=applied, status="abandoned")

            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'succeeded',
                        superseded = 0,
                        response_status = %s,
                        response_body = %s,
                        error = NULL,
                        delivered_at = NOW(6),
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (
                        outcome.response_status,
                        outcome.response_body,
                        delivery_id,
                        lease_token,
                    ),
                )
                applied = (await self._row_count(cursor)) > 0
            if not applied:
                # The lease/token did not match (someone else already
                # terminalized this attempt).  Converge via the same
                # peer-succeeded abandonment, otherwise clear the stale
                # lease so recovery can pick it up.
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'abandoned',
                            superseded = 1,
                            response_status = %s,
                            response_body = %s,
                            error = %s,
                            lease_token = NULL,
                            lease_expires_at = NULL
                        WHERE id = %s
                          AND lease_token = %s
                          AND status IN ('pending', 'retrying')
                          AND superseded = 0
                        """,
                        (
                            outcome.response_status,
                            outcome.response_body,
                            outcome.error,
                            delivery_id,
                            lease_token,
                        ),
                    )
                    abandoned = (await self._row_count(cursor)) > 0
                if abandoned:
                    return WebhookFinalizationResult(
                        applied=True,
                        status="abandoned",
                    )
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE webhook_deliveries
                        SET lease_token = NULL,
                            lease_expires_at = NULL
                        WHERE id = %s AND lease_token = %s
                        """,
                        (delivery_id, lease_token),
                    )
                return WebhookFinalizationResult(applied=False)

            # Abandon free live successors that this success makes obsolete.
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT newer.id FROM webhook_deliveries newer
                    WHERE newer.subscription_id = %s
                      AND newer.event_type = %s
                      AND newer.payload_hash = %s
                      AND newer.attempt_num > %s
                      AND newer.status IN ('pending', 'retrying')
                      AND newer.superseded = 0
                      AND (newer.lease_token IS NULL OR newer.lease_expires_at < NOW(6))
                    ORDER BY newer.attempt_num ASC
                    """,
                    (
                        delivery["subscription_id"],
                        delivery["event_type"],
                        delivery["payload_hash"],
                        delivery["attempt_num"],
                    ),
                )
                successors = await _fetch_all_dicts(cursor)
            for successor in successors:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'abandoned',
                            superseded = 1,
                            status_updated_at = NOW(6),
                            lease_token = NULL,
                            lease_expires_at = NULL
                        WHERE id = %s
                          AND status IN ('pending', 'retrying')
                          AND superseded = 0
                          AND (lease_token IS NULL OR lease_expires_at < NOW(6))
                        """,
                        (successor["id"],),
                    )
            return WebhookFinalizationResult(applied=True, status="succeeded")

        # ── Failure path ────────────────────────────────────────────────────
        # Revoked subscription -> abandoned, no successor.
        if delivery["revoked"]:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned',
                        superseded = 0,
                        error = 'subscription revoked',
                        delivered_at = NOW(6),
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND lease_expires_at >= %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (delivery_id, lease_token, now),
                )
                applied = (await self._row_count(cursor)) > 0
            return WebhookFinalizationResult(
                applied=applied,
                status="abandoned" if applied else None,
            )

        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM webhook_deliveries peer
                  WHERE peer.subscription_id = %s
                    AND peer.event_type = %s
                    AND peer.payload_hash = %s
                    AND peer.status = 'succeeded'
                    AND peer.id <> %s
                )
                """,
                (
                    delivery["subscription_id"],
                    delivery["event_type"],
                    delivery["payload_hash"],
                    delivery_id,
                ),
            )
            row = await cursor.fetchone()
        peer_succeeded = bool(row[0]) if row else False
        if peer_succeeded:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned',
                        superseded = 1,
                        response_status = %s,
                        response_body = %s,
                        error = %s,
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND lease_expires_at >= %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (
                        outcome.response_status,
                        outcome.response_body,
                        outcome.error,
                        delivery_id,
                        lease_token,
                        now,
                    ),
                )
                applied = (await self._row_count(cursor)) > 0
            return WebhookFinalizationResult(applied=applied, status="abandoned")

        next_attempt = int(delivery["attempt_num"]) + 1
        if next_attempt > max_attempts:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned',
                        superseded = 0,
                        response_status = %s,
                        response_body = %s,
                        error = %s,
                        delivered_at = NOW(6),
                        lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s
                      AND lease_token = %s
                      AND lease_expires_at >= %s
                      AND status IN ('pending', 'retrying')
                      AND superseded = 0
                    """,
                    (
                        outcome.response_status,
                        outcome.response_body,
                        outcome.error,
                        delivery_id,
                        lease_token,
                        now,
                    ),
                )
                applied = (await self._row_count(cursor)) > 0
            return WebhookFinalizationResult(
                applied=applied,
                status="abandoned" if applied else None,
            )

        # Retryable failure: terminalize owned attempt, then enqueue
        # at most one successor at the configured backoff time.
        backoff_seconds = backoff_list[int(delivery["attempt_num"]) - 1]
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM webhook_deliveries newer
                  WHERE newer.subscription_id = %s
                    AND newer.event_type = %s
                    AND newer.payload_hash = %s
                    AND newer.attempt_num > %s
                )
                """,
                (
                    delivery["subscription_id"],
                    delivery["event_type"],
                    delivery["payload_hash"],
                    delivery["attempt_num"],
                ),
            )
            row = await cursor.fetchone()
        successor_exists = bool(row[0]) if row else False

        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'abandoned',
                    superseded = 1,
                    response_status = %s,
                    response_body = %s,
                    error = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                  AND lease_token = %s
                  AND lease_expires_at >= %s
                  AND status IN ('pending', 'retrying')
                  AND superseded = 0
                """,
                (
                    outcome.response_status,
                    outcome.response_body,
                    outcome.error,
                    delivery_id,
                    lease_token,
                    now,
                ),
            )
            applied = (await self._row_count(cursor)) > 0
        if not applied:
            return WebhookFinalizationResult(applied=False)
        successor_id: str | None = None
        if not successor_exists:
            # Compute scheduled_at via Python so the backoff is honored
            # even when the DB clock differs from the worker clock.
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
            # Convert to naive UTC for binding to DATETIME(6) under the
            # session UTC time_zone.
            if scheduled_at.tzinfo is not None:
                scheduled_at_naive = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                scheduled_at_naive = scheduled_at
            new_id = str(uuid.uuid4())
            # MySQL has no partial unique indexes; we emulate with the
            # live_chain_key generated column. ``ON DUPLICATE KEY UPDATE
            # id=id`` is the standard idiom for "insert if absent" and
            # works here because the generated column is NULL for all
            # terminal rows (the unique index allows multiple NULLs).
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO webhook_deliveries
                          (id, subscription_id, event_type, payload, payload_hash,
                           attempt_num, status, scheduled_at, writer_revision)
                        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                        """,
                        (
                            new_id,
                            delivery["subscription_id"],
                            delivery["event_type"],
                            delivery["payload"],
                            delivery["payload_hash"],
                            next_attempt,
                            scheduled_at_naive,
                            webhook_constants.NEW_CODE_WRITER_REVISION,
                        ),
                    )
                    successor_id = new_id
            except Exception as exc:
                # 1062 (duplicate key) means another writer raced us to
                # insert this chain's next attempt. That's fine — the
                # other writer's row owns the forward direction.
                if not _is_unique_violation(exc):
                    raise
                successor_id = None
        return WebhookFinalizationResult(
            applied=True,
            status="abandoned",
            successor_delivery_id=successor_id,
        )

    # ── audit-only response body capture ─────────────────────────────────────

    async def store_delivery_response_body(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        response_body: str,
    ) -> bool:
        conn = _mysql_tx(tx).conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "UPDATE webhook_deliveries SET response_body = %s WHERE id = %s",
                (response_body, delivery_id),
            )
            return (await self._row_count(cursor)) > 0

    # ── chain repair sweep ───────────────────────────────────────────────────

    async def repair_delivery_chains(self, tx: Transaction) -> int:
        """Idempotent sweep: terminalize live attempts that have been made
        obsolete by a newer attempt or a succeeded peer.  Mirrors the
        Postgres ``repair_delivery_chains`` / ``repair.WEBHOOK_RETRY_
        SUCCESSOR_REPAIR_SQL`` (repair.py:10-37) shape, adapted for
        MySQL/MariaDB.

        The ``(lease_token IS NULL OR lease_expires_at < NOW(6))`` clause
        preserves the live-worker invariant: never steal an unexpired
        lease; only terminalize rows that are safe to abandon.
        """
        conn = _mysql_tx(tx).conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE webhook_deliveries d
                SET status = 'abandoned',
                    superseded = 1,
                    status_updated_at = NOW(6),
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE d.status IN ('pending', 'retrying')
                  AND d.superseded = 0
                  AND (d.lease_token IS NULL OR d.lease_expires_at < NOW(6))
                  AND (
                    EXISTS (
                      SELECT 1 FROM webhook_deliveries newer
                      WHERE newer.subscription_id = d.subscription_id
                        AND newer.event_type = d.event_type
                        AND newer.payload_hash = d.payload_hash
                        AND newer.attempt_num > d.attempt_num
                    )
                    OR EXISTS (
                      SELECT 1 FROM webhook_deliveries peer
                      WHERE peer.subscription_id = d.subscription_id
                        AND peer.event_type = d.event_type
                        AND peer.payload_hash = d.payload_hash
                        AND peer.status = 'succeeded'
                    )
                  )
                """
            )
            return await self._row_count(cursor)

    # ── legacy dispatcher support (insert_subscription / fetch_deliveries) ──

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
        await self.create_subscription(
            tx,
            subscription_id=subscription_id,
            url=url,
            events=events,
            secret=secret or "",
            description=None,
            owner_id=owner_id,
            namespace=namespace,
        )
        return subscription_id

    async def fetch_deliveries(self, tx: Transaction, subscription_id: str | None = None) -> list[Row]:
        conn = _mysql_tx(tx).conn
        if subscription_id is None:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM webhook_deliveries ORDER BY created ASC"
                )
                return await _fetch_all_dicts(cursor)
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT * FROM webhook_deliveries WHERE subscription_id = %s ORDER BY created ASC",
                (subscription_id,),
            )
            return await _fetch_all_dicts(cursor)


# ── row normalizers ──────────────────────────────────────────────────────────


_MYSQL_WEBHOOK_CLAIM_SELECT = (
    "SELECT d.id, d.subscription_id, d.event_type, d.payload, d.payload_hash, "
    "d.attempt_num, d.status, d.response_status, d.response_body, d.error, "
    "d.scheduled_at, d.delivered_at, d.created, d.status_updated_at, "
    "d.superseded, d.lease_token, d.lease_expires_at, d.writer_revision, "
    "d.lease_token AS lease_token_echo, "
    "s.url, s.secret, s.revoked, s.owner_id, s.namespace "
    "FROM webhook_deliveries d "
    "JOIN webhook_subscriptions s ON s.id = d.subscription_id"
)


def _validate_webhook_scope(owner_id: str | None, namespace: str | None, method: str) -> None:
    if (owner_id is None) != (namespace is None):
        raise ValueError(
            f"{method} requires both owner_id and namespace to be set, "
            "or both to be None for a root/operator view"
        )


def _mysql_webhook_datetime(value: Any) -> datetime:
    """Promote a MySQL DATETIME(6) value into a tz-aware UTC datetime.

    MySQL DATETIME(6) is session-time-zone-relative; MysqlBackend.open
    pins ``SET time_zone = '+00:00'`` so the value comes back as a
    naive datetime already in UTC.  Anything else (TIMESTAMP, string
    from a backfill, etc.) is normalized to UTC for callers.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (bytes, bytearray)):
        parsed = datetime.fromisoformat(value.decode("utf-8"))
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"mysql: expected datetime, got {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mysql_webhook_subscription(row: Any) -> WebhookSubscriptionRecord:
    raw_events = row.get("events") if isinstance(row, dict) else None
    if raw_events is None:
        events: tuple[str, ...] = ()
    elif isinstance(raw_events, (bytes, bytearray)):
        events = tuple(json.loads(raw_events.decode("utf-8")))
    elif isinstance(raw_events, str):
        events = tuple(json.loads(raw_events))
    elif isinstance(raw_events, (list, tuple)):
        events = tuple(str(e) for e in raw_events)
    else:
        events = ()
    return WebhookSubscriptionRecord(
        id=str(row["id"]),
        url=row["url"],
        events=events,
        description=row.get("description"),
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        created=_mysql_webhook_datetime(row["created"]),
        revoked=bool(row["revoked"]),
        revoked_at=(
            _mysql_webhook_datetime(row["revoked_at"])
            if row.get("revoked_at") is not None
            else None
        ),
    )


def _mysql_webhook_delivery(row: Any) -> WebhookDeliveryRecord:
    status = row["status"]
    if status not in ("pending", "retrying", "succeeded", "abandoned"):
        raise ValueError(f"unexpected webhook_deliveries status {status!r}")
    return WebhookDeliveryRecord(
        id=str(row["id"]),
        subscription_id=str(row["subscription_id"]),
        event_type=row["event_type"],
        payload=row["payload"],
        payload_hash=row["payload_hash"],
        attempt_num=int(row["attempt_num"]),
        status=status,  # type: ignore[arg-type]
        response_status=row.get("response_status"),
        response_body=row.get("response_body"),
        error=row.get("error"),
        scheduled_at=_mysql_webhook_datetime(row["scheduled_at"]),
        delivered_at=(
            _mysql_webhook_datetime(row["delivered_at"])
            if row.get("delivered_at") is not None
            else None
        ),
        created=_mysql_webhook_datetime(row["created"]),
        status_updated_at=_mysql_webhook_datetime(row["status_updated_at"]),
        superseded=bool(row["superseded"]),
        lease_token=str(row["lease_token"]) if row.get("lease_token") is not None else None,
        lease_expires_at=(
            _mysql_webhook_datetime(row["lease_expires_at"])
            if row.get("lease_expires_at") is not None
            else None
        ),
        writer_revision=int(row["writer_revision"] or 0),
    )


def _mysql_webhook_claim(row: Any, lease_token: str, claim_now: datetime) -> WebhookDeliveryClaim:
    # ``claim_due_deliveries`` projects ``lease_token`` as
    # ``lease_token_echo`` for a sanity check that the token we wrote
    # round-tripped; ``claim_delivery`` does not because we never wrote
    # the value via a separate RETURNING projection.  We accept both.
    row_token = row.get("lease_token")
    if row_token is None:
        row_token = row.get("lease_token_echo")
    if row_token is not None and str(row_token) != lease_token:
        raise ValueError(
            "mysql: lease_token returned by UPDATE does not match caller-supplied token"
        )
    delivery = _mysql_webhook_delivery(row)
    return WebhookDeliveryClaim(
        delivery=delivery,
        lease_token=lease_token,
        lease_expires_at=_mysql_webhook_datetime(row["lease_expires_at"]),
        claim_db_now=claim_now,
        url=row["url"],
        secret=row.get("secret") or "",
        subscription_revoked=bool(row["revoked"]),
        owner_id=row["owner_id"],
        namespace=row["namespace"],
    )


def _mysql_webhook_chain_lock_name(delivery: Any) -> str:
    """Stable MySQL session-lock name for one webhook retry chain.

    Mirrors ``webhook_chain._delivery_chain_lock_key`` semantics.  MySQL
    named locks are scoped to the connection (``GET_LOCK`` returns the
    same handle for the same name on the same connection), so we hash
    the (subscription, event_type, payload_hash) triple and prefix it
    with a deterministic, short namespace tag to keep the lock name
    inside MySQL's 64-character limit.
    """
    key = (
        f"mnemos:webhook:chain:"
        f"{delivery['subscription_id']}:{delivery['event_type']}:{delivery['payload_hash']}"
    )
    # MySQL 5.7+ accepts up to 64 chars; the chain triple is well under.
    return key[:64]


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

    #: How a JSON-typed column is bound in an INSERT/UPDATE.
    #:
    #: MySQL has a real JSON type and wants the explicit cast. MariaDB does
    #: NOT support ``CAST(x AS JSON)`` at all -- its JSON is an alias for
    #: LONGTEXT with a json_valid() CHECK -- and raises
    #: ``(1064, "You have an error in your SQL syntax ... near 'JSON)'")``.
    #: MariadbFederationRepository overrides this to a plain placeholder.
    #:
    #: Found creating a federation peer on a live MariaDB host: every
    #: POST /v1/federation/peers returned 500, so a MariaDB node could not be
    #: given a peer and therefore could never federate.
    _JSON_BIND = "CAST(%s AS JSON)"

    #: How an existing TEXT/JSON column is read back as JSON in an expression.
    #: Same MariaDB limitation as _JSON_BIND: the cast is unsupported there,
    #: and the column is already LONGTEXT holding JSON, so it needs no cast.
    _JSON_METADATA_EXPR = "COALESCE(CAST(NULLIF(metadata, '') AS JSON), JSON_OBJECT())"
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
                  (%s, %s, %s, %s, %s, {json_bind},
                   {json_bind}, %s, %s, %s,
                   CURRENT_TIMESTAMP(6), CURRENT_TIMESTAMP(6))
                """.format(json_bind=self._JSON_BIND),
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
                assignments.append(f"{col} = {self._JSON_BIND}")
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
        memory_where = [_eligibility.eligible_for_federation("m")]
        tombstone_where = [
            _eligibility.eligible_for_federation_tombstone("m"),
            "m.consolidated_at IS NOT NULL",
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
        where = [_eligibility.eligible_for_federation("m"), "m.id = %s"]
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
                           {json_metadata},
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
                """.format(json_metadata=self._JSON_METADATA_EXPR),
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
        # Use the canonical federation-eligibility predicate (unaliased): it is
        # trusted-scope aware (world-read gate is opt-out via
        # MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE), always excludes the secret
        # vault, and always enforces the federation_source loop-guard — keeping
        # this push path consistent with feed_query/get_feed_memory.
        where = [_eligibility.eligible_for_federation("")]
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


# ── Backend facade ────────────────────────────────────────────────────────────


class MysqlOAuthRepository(MysqlBrowserOAuthMixin, MCPOAuthRepositoryMixin, OAuthRepository):
    """Native MySQL/MariaDB OAuth persistence; identifiers compare case-sensitively."""

    _mcp_insert_key_suffix = " ON DUPLICATE KEY UPDATE key_id = key_id"
    _oauth_duplicate = staticmethod(_is_unique_violation)

    def _mcp_timestamp(self, value: Any) -> datetime:
        return oauth_utc(value).replace(tzinfo=None)

    async def _mcp_fetch(self, tx: Transaction, sql: str, params: tuple = ()) -> Row | None:
        async with _mysql_tx(tx).conn.cursor() as cursor:
            await cursor.execute(sql.replace("?", "%s"), params)
            row = await _fetchone_dict(cursor)
            # Opaque MCP identifiers use VARBINARY so PAD SPACE collations
            # cannot turn an altered client/code/token into a valid credential.
            if row is not None:
                row = {
                    key: value.decode("utf-8") if isinstance(value, bytes) else value
                    for key, value in row.items()
                }
            return row

    async def _mcp_execute(self, tx: Transaction, sql: str, params: tuple = ()) -> int:
        async with _mysql_tx(tx).conn.cursor() as cursor:
            await cursor.execute(sql.replace("?", "%s"), params)
            return int(cursor.rowcount or 0)

    async def _oauth_fetch_all(self, tx: Transaction, sql: str, params: tuple = ()) -> list[Row]:
        async with _mysql_tx(tx).conn.cursor() as cursor:
            await cursor.execute(sql.replace("?", "%s"), params)
            return await _fetch_all_dicts(cursor)


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
    _supports_oauth_persistence = True

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
        self._oauth_repo = MysqlOAuthRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def capabilities(self) -> set[str]:
        return {CORE_CAPABILITY, STATE_CAPABILITY, FEDERATION_CAPABILITY, "oauth"}

    @property
    def audit_chain(self) -> Any | None:
        """No audit-chain implementation on the MySQL family yet.

        ``None`` is the documented contract for a backend that has not
        shipped the audit-chain rows -- callers treat it as
        ``MNEMOS_AUDIT_CHAIN=off``, and federation already guards with
        ``backend.audit_chain is not None``.

        The property has to exist for that guard to work. The backends are
        bare classes rather than subclasses of the ABC that declares it, so
        SqliteBackend / PostgresBackend / OracleBackend each define their
        own; Db2Backend inherits OracleBackend's. MysqlBackend defined
        neither, so the guard raised

          AttributeError: 'MariadbBackend' object has no attribute 'audit_chain'

        and every federation sync that applied a mutation failed with HTTP
        503. Measured on a live MariaDB host: it could pull nothing at all.
        """
        return None

    @property
    def capability_details(self) -> set[str]:
        return {*MYSQL_CAPABILITY_DETAILS, KG_CAPABILITY, STATE_DETAIL_CAPABILITY, "oauth"}

    async def record_usage_ledger(self, tx: Transaction, record: Any) -> Any:
        """Record model-token usage (KNEMON), mirroring the Postgres recorder.

        est_cost_usd is taken from the record when provided, else computed from
        ``model_registry`` MTok prices (reasoning tokens billed at the output
        rate). Subscription-plan rows zero the cost and set subscription_amortized.
        A missing model_registry match (non-subscription) logs price drift and
        defaults cost to 0 so the usage row is still recorded (fail-open on price).
        """
        from decimal import Decimal

        from mnemos.persistence.base import UsageLedgerResult

        conn = tx.conn
        async with conn.cursor() as cursor:
            # Resolve the plan's auth_method; subscription_plans may be absent on
            # MySQL (not in the init schema) — treat any lookup failure as 'api'.
            auth_method = "api"
            try:
                await cursor.execute(
                    "SELECT auth_method FROM subscription_plans "
                    "WHERE provider = %s AND plan_name = %s "
                    "AND effective_from <= CURRENT_DATE "
                    "AND (effective_until IS NULL OR effective_until >= CURRENT_DATE)",
                    (record.provider, record.tier),
                )
                plan_row = await _fetchone_dict(cursor)
                if plan_row and plan_row.get("auth_method"):
                    auth_method = str(plan_row["auth_method"]).lower()
            except Exception as exc:  # noqa: BLE001
                # subscription_plans isn't in the MySQL/MariaDB schema (KNEMON
                # subscription tracking isn't ported), so a missing-table lookup
                # falls back to api pricing. Re-raise any other error.
                errno = getattr(getattr(exc, "args", (None,))[0], "errno", None)
                msg = str(exc)
                if errno != 1146 and "1146" not in msg and "doesn't exist" not in msg:
                    raise
                auth_method = "api"
            is_subscription = auth_method == "subscription"

            registry_match = True
            if record.est_cost_usd is not None:
                cost = Decimal(0) if is_subscription else Decimal(record.est_cost_usd)
            elif is_subscription:
                cost = Decimal(0)
            else:
                await cursor.execute(
                    "SELECT input_cost_per_mtok, output_cost_per_mtok FROM model_registry "
                    "WHERE provider = %s AND model_id = %s",
                    (record.provider, record.model),
                )
                price = await _fetchone_dict(cursor)
                registry_match = price is not None
                in_rate = Decimal(str((price or {}).get("input_cost_per_mtok") or 0))
                out_rate = Decimal(str((price or {}).get("output_cost_per_mtok") or 0))
                cost = (
                    Decimal(record.tokens_in) * in_rate
                    + Decimal(record.tokens_out) * out_rate
                    + Decimal(record.tokens_reasoning) * out_rate
                ) / Decimal(1_000_000)

            if not is_subscription and not registry_match:
                _LOG.warning(
                    "usage_ledger model_registry price missing for provider=%s model=%s; recording est_cost_usd=0",
                    record.provider,
                    record.model,
                )

            await cursor.execute(
                """
                INSERT INTO usage_ledger (
                    provider, model, task_kind, tokens_in, tokens_out, tokens_reasoning,
                    est_cost_usd, latency_ms, outcome, caller_subsystem, tier,
                    session_id, request_count, plan_window_id, path_kind, subscription_amortized
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.provider,
                    record.model,
                    record.task_kind,
                    record.tokens_in,
                    record.tokens_out,
                    record.tokens_reasoning,
                    cost,
                    record.latency_ms,
                    record.outcome,
                    record.caller_subsystem,
                    record.tier,
                    record.session_id,
                    record.request_count,
                    record.plan_window_id,
                    record.path_kind or "api",
                    1 if is_subscription else 0,
                ),
            )
            new_id = getattr(cursor, "lastrowid", None)
            if not new_id:
                await cursor.execute("SELECT LAST_INSERT_ID() AS id")
                row = await _fetchone_dict(cursor)
                new_id = (row or {}).get("id")
            # Return the value as stored (DECIMAL(12,6) rounding), matching the
            # Postgres/Oracle recorders which return the inserted column value.
            await cursor.execute("SELECT est_cost_usd FROM usage_ledger WHERE id = %s", (new_id,))
            stored = await _fetchone_dict(cursor)
        stored_cost = (stored or {}).get("est_cost_usd")
        return UsageLedgerResult(
            id=int(new_id),
            est_cost_usd=Decimal(str(stored_cost)) if stored_cost is not None else cost,
        )

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT category, half_life_days, decay_kind, floor FROM memory_category_decay")
            return await _fetch_all_dicts(cursor)

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    half_life_days = VALUES(half_life_days),
                    decay_kind = VALUES(decay_kind),
                    floor = VALUES(floor)
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
        conn = tx.conn
        metadata_json = json.dumps(metadata or {})
        async with conn.cursor() as cursor:
            if entry_date is None:
                await cursor.execute(
                    "INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata) "
                    "VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s)",
                    (entry_id, owner_id, namespace, topic, content, metadata_json),
                )
            else:
                await cursor.execute(
                    "INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (entry_id, owner_id, namespace, entry_date, topic, content, metadata_json),
                )
            await cursor.execute(
                "SELECT id, entry_date, topic, content, metadata, created FROM journal WHERE id = %s",
                (entry_id,),
            )
            row = await _fetchone_dict(cursor)
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
        conn = tx.conn
        sql = (
            "SELECT id, entry_date, topic, content, metadata, created FROM journal "
            "WHERE owner_id = %s AND namespace = %s AND deleted_at IS NULL"
        )
        params: list[Any] = [owner_id, namespace]
        if entry_date is not None:
            sql += " AND entry_date = %s"
            params.append(entry_date)
        elif topic:
            sql += " AND topic = %s"
            params.append(topic)
        elif search:
            sql += " AND (LOWER(content) LIKE LOWER(%s) OR LOWER(topic) LIKE LOWER(%s))"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY created DESC LIMIT %s"
        params.append(limit)
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return await _fetch_all_dicts(cursor)

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM journal WHERE id = %s AND owner_id = %s AND namespace = %s AND deleted_at IS NULL",
                (entry_id, owner_id, namespace),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0

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

    async def insert_pantheon_routing_audit(
        self,
        tx: Transaction,
        record: Mapping[str, Any],
    ) -> None:
        cost_usd = record.get("cost_usd")
        async with _mysql_tx(tx).conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO pantheon_routing_audit
                       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
                        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        raise BackendCapabilityMissing("webhooks", type(self).__name__)

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit_repo

    @property
    def federation(self) -> FederationRepository:
        return self._federation_repo

    @property
    def oauth(self) -> OAuthRepository:
        return self._oauth_repo

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
                await _ensure_mysql_oauth_schema(conn)
                await _ensure_mysql_webhook_schema(conn)
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
                    "memory_branches",
                    {"deleted_at": "deleted_at TIMESTAMP(6) NULL"},
                )
                await _ensure_mysql_columns(
                    conn,
                    "entities",
                    {
                        "owner_id": "owner_id VARCHAR(256) NOT NULL DEFAULT 'default'",
                        "namespace": "namespace VARCHAR(256) NOT NULL DEFAULT 'default'",
                        "deleted_at": "deleted_at TIMESTAMP(6) NULL",
                    },
                )
                await _ensure_mysql_columns(
                    conn,
                    "sessions",
                    {
                        "namespace": "namespace VARCHAR(256) NOT NULL DEFAULT 'default'",
                        "deleted_at": "deleted_at TIMESTAMP(6) NULL",
                    },
                )
                await _ensure_mysql_columns(
                    conn,
                    "session_messages",
                    {"deleted_at": "deleted_at TIMESTAMP(6) NULL"},
                )
                await _ensure_mysql_columns(
                    conn,
                    "session_memory_injections",
                    {"deleted_at": "deleted_at TIMESTAMP(6) NULL"},
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
        except Exception:
            _LOG.exception("MysqlBackend.open failed while provisioning the required schema")
            raise

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
