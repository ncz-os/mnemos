"""MariaDB 11.7+ persistence backend for MNEMOS.

MariaDB is wire-compatible with ``aiomysql``, so this backend reuses the
MySQL pool, transaction, cursor, and repository machinery. The only SQL
dialect differences are the native vector constructors, distance functions,
and memory-table vector DDL.

Positioning — this is MNEMOS's **default open-source / self-hosted vector
backend**. MariaDB ships native vector search (``VECTOR`` columns,
``VEC_DISTANCE_COSINE`` / ``VEC_FromText``, HNSW ``VECTOR INDEX``) in its
**free Community** edition, and is the default "MySQL" package on most Linux
distros — so a self-hosted operator gets working semantic search with no
license. (Contrast the ``mysql`` backend, whose ``VECTOR_DISTANCE`` is gated
behind MySQL Enterprise / HeatWave — that one targets the Enterprise/cloud
audience: RDS/Aurora MySQL, HeatWave.)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from mnemos.core import eligibility as _eligibility
from mnemos.persistence.base import (
    BranchRepository,
    CORE_CAPABILITY,
    CompressionQueueRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FEDERATION_CAPABILITY,
    FederationRepository,
    KG_CAPABILITY,
    KGRepository,
    MYSQL_CAPABILITY_DETAILS,
    MemoryRepository,
    STATE_CAPABILITY,
    STATE_DETAIL_CAPABILITY,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.mysql import (
    _DDL_COMPRESSION_QUEUE,
    _DDL_CONSULTATION_MEMORY_REFS,
    _DDL_FEDERATION_PEERS,
    _DDL_FEDERATION_SYNC_LOG,
    _DDL_GRAEAE_AUDIT_LOG,
    _DDL_GRAEAE_CONSULTATIONS,
    _DDL_KG_TRIPLES,
    _DDL_MODEL_REGISTRY,
    _DDL_MODEL_REGISTRY_SYNC_LOG,
    _DDL_STATE,
    _DEFAULT_EMBEDDING_DIM,
    _MysqlTransaction,
    _boosted_rank_supersession_sort_key,
    _content_hash,
    _cosine_distance_python,
    _ensure_mysql_columns,
    _fetch_all_dicts,
    _is_unique_violation,
    _is_vec_distance_unsupported,
    _mysql_tx,
    _parse_mysql_dsn,
    _rank_score_sort_key,
    _render_visibility,
    _validate_and_format_vector,
    create_mysql_pool,
    MysqlBackend,
    MysqlBranchRepository,
    MysqlCompressionQueueRepository,
    MysqlCompressionRepository,
    MysqlConsultationAuditRepository,
    MysqlFederationRepository,
    MysqlKGRepository,
    MysqlMemoryRepository,
    MysqlStateRepository,
    MysqlVersionRepository,
    MysqlWebhookRepository,
)
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter

_LOG = logging.getLogger(__name__)

_VECTOR_COLUMN = f"VECTOR({_DEFAULT_EMBEDDING_DIM}) NOT NULL"

_DDL_MEMORIES = """\
CREATE TABLE IF NOT EXISTS memories (
    id                VARCHAR(64) CHARACTER SET ascii NOT NULL,
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
    PRIMARY KEY (id),
    INDEX idx_memories_ns_cat  (namespace, category),
    INDEX idx_memories_owner   (owner_id, namespace),
    INDEX idx_memories_hash    (content_hash),
    INDEX idx_memories_federation_remote (federation_source, federation_remote_updated),
    INDEX idx_memories_push (federation_source, federation_last_pushed_at),
    FULLTEXT INDEX idx_memories_ft (content)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_MEMORY_EMBEDDINGS = f"""\
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id  VARCHAR(64) CHARACTER SET ascii NOT NULL,
    embedding  {_VECTOR_COLUMN},
    PRIMARY KEY (memory_id),
    VECTOR INDEX (embedding),
    CONSTRAINT fk_memory_embeddings_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_COMPRESSION_CANDIDATES = """\
CREATE TABLE IF NOT EXISTS memory_compression_candidates (
    id                  VARCHAR(64)  NOT NULL DEFAULT (UUID()),
    memory_id           VARCHAR(64) CHARACTER SET ascii NOT NULL,
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
    memory_id            VARCHAR(64) CHARACTER SET ascii NOT NULL,
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

_DDL_MEMORY_VERSIONS = """\
CREATE TABLE IF NOT EXISTS memory_versions (
    id                VARCHAR(64)   NOT NULL,
    memory_id         VARCHAR(64) CHARACTER SET ascii NOT NULL,
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
    memory_id       VARCHAR(64) CHARACTER SET ascii NOT NULL,
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
    _DDL_MEMORY_EMBEDDINGS,
    _DDL_FEDERATION_PEERS,
    _DDL_FEDERATION_SYNC_LOG,
    _DDL_MEMORY_VERSIONS,
    _DDL_MEMORY_BRANCHES,
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


def _parse_mariadb_dsn(dsn: str) -> dict[str, Any]:
    """Parse ``mariadb://user:pass@host:port/db`` using the MySQL parser."""
    return _parse_mysql_dsn(dsn)


async def create_mariadb_pool(
    dsn: str,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    settings: Any = None,
) -> Any:
    """Create an aiomysql async connection pool for MariaDB."""
    return await create_mysql_pool(dsn, min_size=min_size, max_size=max_size, settings=settings)


class MariadbMemoryRepository(MysqlMemoryRepository):
    """MariaDB 11.7+ memory repository with MariaDB vector SQL."""

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
                        created, updated
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
                inserted = bool(cursor.rowcount)
                if inserted and vec_literal is not None:
                    await cursor.execute(
                        """
                        INSERT INTO memory_embeddings(memory_id, embedding)
                        VALUES (%s, VEC_FromText(%s))
                        """,
                        (memory_id, vec_literal),
                    )
                return "INSERT 0 1" if inserted else "INSERT 0 0"
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        if not embedding:
            return
        self._require_dim(embedding, "upsert_memory_embedding")
        vec_literal = _validate_and_format_vector(embedding)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO memory_embeddings(memory_id, embedding)
                VALUES (%s, VEC_FromText(%s))
                ON DUPLICATE KEY UPDATE embedding = VEC_FromText(%s)
                """,
                (memory_id, vec_literal, vec_literal),
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
        if not embedding:
            return []
        self._require_dim(embedding, "semantic_search")
        vec_literal = _validate_and_format_vector(embedding)
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

        # MariaDB VEC_DISTANCE_COSINE returns 0 for identical vectors and grows
        # with dissimilarity. Keep the SQL rank/order expression as the bare
        # distance so the native vector index can serve top-K; recency boost is
        # applied in Python after over-fetching candidates.
        rank_expr = "VEC_DISTANCE_COSINE(me.embedding, VEC_FromText(%s))"
        candidate_limit = max(limit, min(limit * 4, 200)) if boost_recency else limit

        # Bind the VEC_FromText placeholder before the rest of the params.
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
                      JOIN memory_embeddings me ON me.memory_id = m.id
                     WHERE {" AND ".join(where)}
                     ORDER BY rank_score ASC
                     LIMIT %s
                    """,
                    vec_params,
                )
                rows = await _fetch_all_dicts(cursor)
        except Exception as exc:
            if _is_vec_distance_unsupported(exc):
                # Keep degraded search on the same join-table storage shape.
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
        """Fallback semantic search using the MariaDB embedding join table."""
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
                       VEC_ToText(me.embedding) AS embedding_json
                  FROM memories m
                  JOIN memory_embeddings me ON me.memory_id = m.id
                 WHERE {" AND ".join(where)}
                """,
                params,
            )
            raw_rows = await _fetch_all_dicts(cursor)

        today = datetime.now(timezone.utc).date()
        w = float(recency_weight)
        for row in raw_rows:
            emb_json = row.pop("embedding_json", None)
            try:
                emb = json.loads(emb_json) if emb_json else None
                dist = _cosine_distance_python(query_vec, emb) if emb else 1.0
            except (json.JSONDecodeError, ValueError, TypeError):
                dist = 1.0
            row["rank_score"] = dist

        if boost_recency:
            raw_rows.sort(key=lambda row: _boosted_rank_supersession_sort_key(row, today=today, recency_weight=w))
        else:
            raw_rows.sort(key=_rank_score_sort_key)
        return raw_rows[:limit]


class MariadbKGRepository(MysqlKGRepository):
    pass


class MariadbVersionRepository(MysqlVersionRepository):
    pass


class MariadbBranchRepository(MysqlBranchRepository):
    pass


class MariadbCompressionRepository(MysqlCompressionRepository):
    pass


class MariadbCompressionQueueRepository(MysqlCompressionQueueRepository):
    pass


class MariadbWebhookRepository(MysqlWebhookRepository):
    pass


class MariadbConsultationAuditRepository(MysqlConsultationAuditRepository):
    pass


class MariadbFederationRepository(MysqlFederationRepository):
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
            from mnemos.core.config import embed_http_model_override
            from mnemos.core.config import get_settings as _gs

            try:
                http_model = embed_http_model_override()
                embed_model = http_model or (_gs().providers.inference_embed_model or "").strip() or "unknown"
            except Exception:
                embed_model = "unknown"
            join_embedding = "LEFT JOIN memory_embeddings me ON me.memory_id = m.id"
            embed_select_memory = "VEC_ToText(me.embedding) AS embedding, %s AS embedding_model,"
            embed_select_tombstone = "NULL AS embedding, NULL AS embedding_model,"
            select_params = [embed_model]
        else:
            join_embedding = ""
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
                    {join_embedding}
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


class MariadbStateRepository(MysqlStateRepository):
    pass


class MariadbBackend(MysqlBackend):
    """MariaDB 11.7+ persistence facade backed by an aiomysql connection pool."""

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    supports_mysql_vector = True
    supports_mariadb_vector = True
    supports_webhooks = False
    inline_embedding_searchable = True
    _supports_core_persistence = True

    def __init__(self, pool: Any, settings: Any) -> None:
        self._pool = pool
        self._settings = settings
        self._closed = False
        self._memories_repo = MariadbMemoryRepository()
        try:
            self._memories_repo._expected_embedding_dim = int(
                getattr(settings.database, "embedding_dim", _DEFAULT_EMBEDDING_DIM)
            )
        except (AttributeError, TypeError, ValueError):
            self._memories_repo._expected_embedding_dim = _DEFAULT_EMBEDDING_DIM
        self._kg_triples_repo = MariadbKGRepository()
        self._memory_versions_repo = MariadbVersionRepository()
        self._memory_branches_repo = MariadbBranchRepository()
        self._compression_repo = MariadbCompressionRepository()
        self._compression_queue_repo = MariadbCompressionQueueRepository()
        self._consultations_audit_repo = MariadbConsultationAuditRepository()
        self._federation_repo = MariadbFederationRepository()
        self._state_kv_repo = MariadbStateRepository()

    @property
    def capabilities(self) -> set[str]:
        return {CORE_CAPABILITY, STATE_CAPABILITY, FEDERATION_CAPABILITY}

    @property
    def capability_details(self) -> set[str]:
        return {*MYSQL_CAPABILITY_DETAILS, KG_CAPABILITY, STATE_DETAIL_CAPABILITY}

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
        return MariadbWebhookRepository()

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit_repo

    @property
    def federation(self) -> FederationRepository:
        return self._federation_repo

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv_repo

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

    async def open(self) -> None:
        """Validate pool connectivity and apply UTC + init DDL."""
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
                "MariadbBackend.open probe failed (%s); backend remains open but first acquire() may also fail.",
                exc,
            )


__all__ = [
    "MariadbBackend",
    "MariadbBranchRepository",
    "MariadbCompressionQueueRepository",
    "MariadbCompressionRepository",
    "MariadbConsultationAuditRepository",
    "MariadbFederationRepository",
    "MariadbKGRepository",
    "MariadbMemoryRepository",
    "MariadbStateRepository",
    "MariadbVersionRepository",
    "MariadbWebhookRepository",
    "_MysqlTransaction",
    "_parse_mariadb_dsn",
    "create_mariadb_pool",
]
