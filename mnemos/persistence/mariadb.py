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

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

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
    _DDL_COMPRESSED_VARIANTS,
    _DDL_COMPRESSION_CANDIDATES,
    _DDL_COMPRESSION_QUEUE,
    _DDL_CONSULTATION_MEMORY_REFS,
    _DDL_FEDERATION_PEERS,
    _DDL_FEDERATION_SYNC_LOG,
    _DDL_GRAEAE_AUDIT_LOG,
    _DDL_GRAEAE_CONSULTATIONS,
    _DDL_KG_TRIPLES,
    _DDL_MEMORY_BRANCHES,
    _DDL_MEMORY_VERSIONS,
    _DDL_MODEL_REGISTRY,
    _DDL_MODEL_REGISTRY_SYNC_LOG,
    _DDL_STATE,
    _DEFAULT_EMBEDDING_DIM,
    _MysqlTransaction,
    _boosted_rank_supersession_sort_key,
    _content_hash,
    _ensure_mysql_columns,
    _fetch_all_dicts,
    _is_unique_violation,
    _is_vec_distance_unsupported,
    _mysql_tx,
    _parse_mysql_dsn,
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
    FULLTEXT INDEX idx_memories_ft (content),
    VECTOR INDEX (embedding)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_INIT_DDLS = [
    _DDL_MEMORIES,
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
        # Format embedding as MariaDB VEC_FromText literal; NULL when absent.
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
                        VEC_FromText(%s),
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

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        if not embedding:
            return
        self._require_dim(embedding, "upsert_memory_embedding")
        vec_literal = _validate_and_format_vector(embedding)
        conn = tx.conn
        async with conn.cursor() as cursor:
            await cursor.execute(
                "UPDATE memories SET embedding = VEC_FromText(%s) WHERE id = %s",
                (vec_literal, memory_id),
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

        # MariaDB VEC_DISTANCE_COSINE returns 0 for identical vectors and grows
        # with dissimilarity. Keep the SQL rank/order expression as the bare
        # distance so the native vector index can serve top-K; recency boost is
        # applied in Python after over-fetching candidates.
        rank_expr = "VEC_DISTANCE_COSINE(m.embedding, VEC_FromText(%s))"
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
    pass


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
