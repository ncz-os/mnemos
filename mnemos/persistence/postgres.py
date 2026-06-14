"""Postgres persistence backend.

Legacy memory/DAG helpers live directly behind this backend-neutral
persistence interface.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from mnemos.core.auth_context import UserContext
from mnemos.core.config import embed_http_model_override, hot_rs_enabled
from mnemos.core.native_accel import load_hot_rs
from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP
from mnemos.core.recommendation import choose_recommended_model
from mnemos.core.visibility import (
    read_visibility_predicate as _core_read_visibility_predicate,
    version_visibility_predicate as _core_version_visibility_predicate,
)
from mnemos.core import eligibility as _eligibility
from mnemos.persistence.base import (
    POSTGRES_CAPABILITY_DETAILS,
    AclRepository,
    AuditChainRepository,
    BranchRepository,
    CompressionQueueRepository,
    CompressionRepository,
    CompressionStatsRow,
    ConsultationAuditRepository,
    ConsultationsRepository,
    FederationRepository,
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
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core import webhook_constants
from mnemos.persistence import nats_events as persistence_nats_events

logger = logging.getLogger(__name__)
_RECENCY_E_FOLD_SECONDS = 7 * 24 * 60 * 60
_FEDERATION_NATS_MEMORY_ROW_COLS = (
    "id, content, category, subcategory, created, updated, metadata, "
    "quality_rating, verbatim_content, owner_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent, archived_at, "
    "federation_source, deleted_at, consolidated_into"
)
_USER_ID_SAFE = re.compile(r"[^a-zA-Z0-9._:-]+")


def _mint_user_id(provider: str, external_id: str) -> str:
    slug = _USER_ID_SAFE.sub("", f"{provider}:{external_id}")
    return slug[:64] or f"{provider}:{secrets.token_hex(6)}"


def _log_search_phase(
    trace_id: str | None,
    started_at: float | None,
    phase: str,
) -> None:
    if not trace_id or started_at is None:
        return
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info("[search:%s] %s done in %dms", trace_id, phase, elapsed_ms)


# Optional Rust hot-path accelerator. Loaded lazily so operators do not
# need the Rust wheel unless they opt in.
_HOT_RS = None
_HOT_RS_ENABLED = hot_rs_enabled()
if _HOT_RS_ENABLED:
    _HOT_RS = load_hot_rs(logger, "Postgres semantic rerank")


def _vector_to_float_list(vector: Sequence[float]) -> list[float]:
    return [float(value) for value in vector]


def _parse_pgvector_text(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(value) for value in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [part for part in raw.strip("[]").split(",") if part]
        if isinstance(parsed, list):
            try:
                return [float(value) for value in parsed]
            except (TypeError, ValueError):
                return []
    return []


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def _rerank_composite_python(
    query: Sequence[float],
    candidates: Sequence[Sequence[float]],
    recency_boost: Sequence[float],
    weight_cos: float,
    weight_recency: float,
    k: int,
) -> list[tuple[int, float]]:
    query_values = _vector_to_float_list(query)
    scores = [
        (
            idx,
            weight_cos * _cosine_similarity(query_values, _vector_to_float_list(candidate))
            + weight_recency * float(recency_boost[idx] if idx < len(recency_boost) else 0.0),
        )
        for idx, candidate in enumerate(candidates)
    ]
    scores.sort(key=lambda item: (-item[1], item[0]))
    return scores if k == 0 or k >= len(scores) else scores[:k]


def _rerank_composite(
    query: Sequence[float],
    candidates: Sequence[Sequence[float]],
    recency_boost: Sequence[float],
    weight_cos: float,
    weight_recency: float,
    k: int,
) -> list[tuple[int, float]]:
    if _HOT_RS is not None:
        try:
            result = _HOT_RS.rerank_composite(
                _vector_to_float_list(query),
                [_vector_to_float_list(candidate) for candidate in candidates],
                [float(value) for value in recency_boost],
                float(weight_cos),
                float(weight_recency),
                int(k),
            )
            return [(int(idx), float(score)) for idx, score in result]
        except Exception:
            pass
    return _rerank_composite_python(
        query,
        candidates,
        recency_boost,
        weight_cos,
        weight_recency,
        k,
    )


def _render_postgres_visibility(
    visibility: VisibilityFilter,
    *,
    start_idx: int = 1,
    table_alias: str = "",
) -> tuple[str, list[Any], int]:
    """Render a ``VisibilityFilter`` into a Postgres WHERE fragment.

    Returns ``(clause, params, next_idx)`` where ``clause`` is the SQL
    fragment using ``$N`` placeholders starting at ``start_idx``,
    ``params`` is the list of values to extend the caller's params
    list with (in placeholder order), and ``next_idx`` is the first
    free placeholder index after consuming ``params``.

    Returns ``("", [], start_idx)`` for ``ROOT_BYPASS`` with no
    namespace pin — the caller omits the WHERE entirely. The
    ``READABLE`` branch delegates to ``mnemos.core.visibility`` so the
    predicate stays one-to-one with the v1_multiuser RLS read policy.
    """
    p = f"{table_alias}." if table_alias else ""

    def _excl_clause(idx: int) -> tuple[str, list[Any], int]:
        excl = tuple(visibility.exclude_namespaces or ())
        if not excl:
            return "", [], idx
        placeholders = [f"${idx + i}" for i in range(len(excl))]
        return (
            f"({p}namespace IS NULL OR {p}namespace NOT IN ({', '.join(placeholders)}))",
            list(excl),
            idx + len(excl),
        )

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return _excl_clause(start_idx)
        clause = f"{p}namespace=${start_idx}"
        excl_clause, excl_params, next_idx = _excl_clause(start_idx + 1)
        if excl_clause:
            clause = f"{clause} AND {excl_clause}"
        return clause, [visibility.namespace] + excl_params, next_idx

    if visibility.namespace is None:
        return "1=0", [], start_idx

    if visibility.scope == VisibilityScope.OWN_ONLY:
        # Mutation path: strict owner_id + namespace match, with the
        # same namespace subtraction applied to every visibility scope.
        clause = f"{p}owner_id=${start_idx} AND {p}namespace=${start_idx + 1}"
        excl_clause, excl_params, next_idx = _excl_clause(start_idx + 2)
        if excl_clause:
            clause = f"{clause} AND {excl_clause}"
        return clause, [visibility.user_id, visibility.namespace] + excl_params, next_idx

    # READABLE: full v1_multiuser read predicate via core helper, plus
    # namespace pin appended after.
    clause, vis_params = _core_read_visibility_predicate(
        visibility.user_id or "",
        list(visibility.group_ids),
        start_idx,
        table_alias=table_alias,
    )
    next_idx = start_idx + len(vis_params)
    clause = f"{clause} AND {p}namespace=${next_idx}"
    vis_params = vis_params + [visibility.namespace]
    next_idx += 1
    excl_clause, excl_params, next_idx = _excl_clause(next_idx)
    if excl_clause:
        clause = f"{clause} AND {excl_clause}"
        vis_params += excl_params
    return clause, vis_params, next_idx


async def _fetch_sidecar(
    conn,
    *,
    table: str,
    columns: str,
    memory_id_column: str,
    memory_ids: Sequence[str],
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    bound_to_memories: bool,
    hard_limit: int,
    null_ok: bool = False,
    order_by: Optional[str] = None,
):
    if bound_to_memories and not memory_ids and not null_ok:
        return []

    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    if table in {"kg_triples", "memory_versions"}:
        conditions.append("deleted_at IS NULL")
    if bound_to_memories:
        if null_ok and memory_ids:
            conditions.append(f"({memory_id_column} IS NULL OR {memory_id_column} = ANY(${idx}::text[]))")
            params.append(list(memory_ids))
            idx += 1
        elif null_ok:
            conditions.append(f"{memory_id_column} IS NULL")
        else:
            conditions.append(f"{memory_id_column} = ANY(${idx}::text[])")
            params.append(list(memory_ids))
            idx += 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = f"ORDER BY {order_by}" if order_by else ""
    sql = f"SELECT {columns} FROM {table} {where} {order} LIMIT {hard_limit + 1}"
    return await conn.fetch(sql, *params)


class PostgresTransaction:
    """Transaction wrapper that keeps asyncpg private to the Postgres adapter."""

    def __init__(self, conn: asyncpg.Connection, tx: Any):
        self._conn = conn
        self._tx = tx
        self._closed = False
        self._after_commit: list[Callable[[], Awaitable[None] | None]] = []

    @property
    def conn(self) -> asyncpg.Connection:
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    async def commit(self) -> None:
        if self._closed:
            return
        await self._tx.commit()
        self._closed = True
        callbacks = self._after_commit
        self._after_commit = []
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("Postgres post-commit callback failed", exc_info=True)

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._tx.rollback()
        self._closed = True
        self._after_commit = []

    def add_after_commit(self, callback: Callable[[], Awaitable[None] | None]) -> None:
        if self._closed:
            raise RuntimeError("cannot register post-commit callback on a closed transaction")
        self._after_commit.append(callback)


def _postgres_tx(tx: Transaction) -> PostgresTransaction:
    if not isinstance(tx, PostgresTransaction):
        raise TypeError("Postgres repositories require a PostgresTransaction")
    return tx


def _pg_result_count(result: str | None) -> int:
    if not result:
        return 0
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (IndexError, ValueError):
        return 0


async def _queue_federation_nats_upsert_from_db(tx: PostgresTransaction, memory_id: str) -> None:
    if not persistence_nats_events.federation_nats_enabled():
        return
    row = await tx.conn.fetchrow(
        f"""
        SELECT {_FEDERATION_NATS_MEMORY_ROW_COLS}
        FROM memories
        WHERE id = $1
        """,
        memory_id,
    )
    _queue_federation_nats_upsert(tx, row)


def _queue_federation_nats_upsert(tx: PostgresTransaction, row: Row | None) -> None:
    if row is None or not persistence_nats_events.federation_nats_enabled():
        return
    event = persistence_nats_events.federation_memory_upsert_event(row)
    if event is None:
        return
    tx.add_after_commit(lambda event=event: persistence_nats_events.publish_federation_memory_upsert_event(event))


class PostgresMemoryRepository(MemoryRepository):
    # semantic_search emits ``similarity`` = 1 - (embedding <=> q) (pgvector
    # cosine similarity in [0,1], higher = better).
    SEMANTIC_SCORE_COLUMN = "similarity"
    SEMANTIC_SCORE_METRIC = "cosine_similarity"

    # Set by PostgresBackend on construction so search paths can fail
    # loudly on dim mismatches. None disables the check (e.g. tests
    # that bypass the backend). Mirrors SqliteMemoryRepository's
    # `_expected_embedding_dim` so the operator-facing error has the
    # same shape on both backends — surfaced 2026-05-08 by the
    # cross-code audit (#202).
    _expected_embedding_dim: int | None = None

    def _require_dim(self, embedding: Sequence[float], op: str) -> None:
        """Fail loudly if the embedding length doesn't match the
        configured dim. Without this guard, `embedding <=> $1::vector`
        in semantic_search rejects the cast at the asyncpg layer with
        a generic ``DataError``; the operator-facing message names
        the wrong layer (asyncpg type cast) instead of the actual
        cause (mismatched embedding model). The SQLite repository
        has the same guard for the same reason — keep both backends
        in lockstep so MNEMOS_EMBEDDING_DIM mismatches surface the
        same way regardless of profile.
        """
        expected = self._expected_embedding_dim
        if expected is None:
            return
        actual = len(embedding)
        if actual != expected:
            raise ValueError(
                f"Postgres embedding dim mismatch on {op}: got "
                f"{actual}-D vector but the configured "
                f"MNEMOS_EMBEDDING_DIM is {expected}. The embedding "
                f"endpoint may have been switched to a different "
                f"model. Verify INFERENCE_EMBED_HOST / model "
                f"selection and either restart with the matching "
                f"MNEMOS_EMBEDDING_DIM or swap the embedding "
                f"endpoint back to the model the DB was sized for."
            )

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None:
        # Inlined Postgres impl (matches oracle/db2/sqlite backends) so this
        # concrete backend owns the repository SQL directly.
        conn = _postgres_tx(tx).conn
        if user.role == "root":
            row = await conn.fetchrow(
                "SELECT 1 FROM memory_versions WHERE memory_id = $1 AND deleted_at IS NULL LIMIT 1",
                memory_id,
            )
            if not row:
                raise PermissionError("Memory not found")
            return
        vis_clause, vis_params = _core_read_visibility_predicate(
            user.user_id,
            list(user.group_ids),
            2,
        )
        ns_ph = f"${len(vis_params) + 2}"
        row = await conn.fetchrow(
            f"SELECT 1 FROM memories WHERE id = $1 "
            f"AND deleted_at IS NULL AND {vis_clause} "
            f"AND namespace = {ns_ph} LIMIT 1",
            memory_id,
            *vis_params,
            user.namespace,
        )
        if not row:
            raise PermissionError("Memory not found")

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if user.role == "root":
            anchor_scope = ""
            recursive_scope = ""
            params = (memory_id, branch, limit)
        else:
            vis_clause, vis_params = _core_version_visibility_predicate(
                user.user_id,
                start_param_idx=4,
                table_alias="mv",
            )
            ns_ph = f"${len(vis_params) + 4}"
            anchor_scope = f"AND {vis_clause} AND mv.namespace = {ns_ph}"
            recursive_scope = f"AND {vis_clause} AND mv.namespace = {ns_ph}"
            params = (memory_id, branch, limit, *vis_params, user.namespace)

        rows = await conn.fetch(
            f"""
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
                    mb.name = $2 AND
                    mb.head_version_id = mv.id
                )
                WHERE mv.memory_id = $1
                  AND mv.deleted_at IS NULL
                  AND mb.deleted_at IS NULL
                  {anchor_scope}
                UNION ALL
                -- Same-memory predicate (mv.memory_id = cw.memory_id)
                -- prevents corrupt parent_version_id from pulling another
                -- memory's version into this memory's log. Mirrors the HTTP
                -- log handler in api/routes/dag.py.
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
                WHERE cw.depth < $3
                  AND mv.deleted_at IS NULL
                  {recursive_scope}
            )
            SELECT
                commit_hash, version_num, branch, category, change_type,
                snapshot_at, snapshot_by, owner_id, namespace, permission_mode
            FROM commit_walk
            ORDER BY depth ASC
            LIMIT $3
            """,
            *params,
        )
        return list(rows)

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]:
        conn = _postgres_tx(tx).conn
        if user.role == "root":
            base_sql = (
                "SELECT content, version_num FROM memory_versions "
                "WHERE memory_id = $1 AND commit_hash = $2 "
                "AND deleted_at IS NULL"
            )
            return (
                await conn.fetchrow(base_sql, memory_id, commit_a),
                await conn.fetchrow(base_sql, memory_id, commit_b),
            )

        vis_clause, vis_params = _core_version_visibility_predicate(
            user.user_id,
            start_param_idx=3,
        )
        ns_ph = f"${len(vis_params) + 3}"
        gated_sql = (
            "SELECT content, version_num FROM memory_versions "
            "WHERE memory_id = $1 AND commit_hash = $2 "
            f"AND deleted_at IS NULL AND {vis_clause} AND namespace = {ns_ph}"
        )
        return (
            await conn.fetchrow(gated_sql, memory_id, commit_a, *vis_params, user.namespace),
            await conn.fetchrow(gated_sql, memory_id, commit_b, *vis_params, user.namespace),
        )

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        if user.role == "root":
            return await conn.fetchrow(
                """
                SELECT
                    commit_hash, version_num, branch, category, subcategory,
                    content, change_type, snapshot_at, snapshot_by
                FROM memory_versions
                WHERE memory_id = $1 AND commit_hash = $2
                  AND deleted_at IS NULL
                """,
                memory_id,
                commit_hash,
            )

        vis_clause, vis_params = _core_version_visibility_predicate(
            user.user_id,
            start_param_idx=3,
        )
        ns_ph = f"${len(vis_params) + 3}"
        return await conn.fetchrow(
            f"""
            SELECT
                commit_hash, version_num, branch, category, subcategory,
                content, change_type, snapshot_at, snapshot_by
            FROM memory_versions
            WHERE memory_id = $1 AND commit_hash = $2
              AND deleted_at IS NULL
              AND {vis_clause} AND namespace = {ns_ph}
            """,
            memory_id,
            commit_hash,
            *vis_params,
            user.namespace,
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
        conn = _postgres_tx(tx).conn
        conditions: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        idx = 1
        if effective_owner:
            conditions.append(f"owner_id = ${idx}")
            params.append(effective_owner)
            idx += 1
        if effective_ns:
            conditions.append(f"namespace = ${idx}")
            params.append(effective_ns)
            idx += 1
        if category:
            conditions.append(f"category = ${idx}")
            params.append(category)
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            "metadata, "
            # Provenance-bearing columns for MPF v0.2 emission. Morpheus
            # writes (provenance='morpheus_local', morpheus_run_id,
            # source_memories[]); federation pulls write
            # (federation_source). The serializer's _record_provenance_v0_2
            # helper uses these to populate PROV-DM wasGeneratedBy +
            # wasInfluencedBy with real lineage rather than heuristic
            # source_agent guesses.
            "provenance AS prov_kind, morpheus_run_id::text AS morpheus_run_id, "
            "source_memories, federation_source "
            "FROM memories "
            f"{where} "
            f"ORDER BY created ASC "
            f"LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])
        return await conn.fetch(sql, *params)

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        sql = "SELECT id, owner_id, namespace FROM memories WHERE id = ANY($1::text[]) AND deleted_at IS NULL"
        params: list[Any] = [list(referenced_ids)]
        if scope_owner is not None:
            sql += " AND owner_id = $2"
            params.append(scope_owner)
            if scope_namespace is not None:
                sql += " AND namespace = $3"
                params.append(scope_namespace)
        elif scope_namespace is not None:
            sql += " AND namespace = $2"
            params.append(scope_namespace)
        return await conn.fetch(sql, *params)

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
        pg_tx = _postgres_tx(tx)
        conn = pg_tx.conn
        # Format embedding as pgvector literal; NULL when not provided so
        # the column stays NULL and semantic_search filters it out until
        # backfill.  Inline in the INSERT so the vector commits atomically
        # with the row — no second round-trip needed.
        vec_str: str | None = None
        if embedding:
            self._require_dim(embedding, "insert_memory")
            vec_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        result = await conn.execute(
            """
            INSERT INTO memories (
                id, content, category, subcategory, metadata,
                quality_rating, verbatim_content, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                embedding, created, updated
            )
            VALUES (
                $1, $2, $3, $4, $5::jsonb,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15::vector, COALESCE($16, NOW()), COALESCE($17, NOW())
            )
            ON CONFLICT (id) DO NOTHING
            """,
            memory_id,
            content,
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
            vec_str,
            created,
            updated,
        )
        if _pg_result_count(result) > 0:
            await _queue_federation_nats_upsert_from_db(pg_tx, memory_id)
        return result

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        """Write a precomputed embedding vector to memories.embedding for the
        given memory_id. Idempotent; no-op when embedding is empty.

        Used by the create_memory path (mnemos/api/routes/memories.py) to
        attach an embedding inline after insert, and by
        scripts/backfill_embeddings.py to fill NULL rows. The embedding
        dim must match the configured `embedding_dim` (768 by default).
        """
        if not embedding:
            return
        self._require_dim(embedding, "upsert_memory_embedding")
        vec_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        await _postgres_tx(tx).conn.execute(
            "UPDATE memories SET embedding = $1::vector WHERE id = $2",
            vec_str,
            memory_id,
        )

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = _postgres_tx(tx).conn
        return await conn.fetchrow(
            "SELECT content, category, subcategory, "
            "metadata, quality_rating, owner_id, "
            "namespace, permission_mode, "
            "source_model, source_provider, "
            "source_session, source_agent, "
            "created, updated "
            "FROM memories WHERE id = $1 AND deleted_at IS NULL",
            memory_id,
        )

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        conn = _postgres_tx(tx).conn
        await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        conn = _postgres_tx(tx).conn
        return await conn.fetch(
            "SELECT DISTINCT memory_id FROM memory_versions WHERE memory_id = ANY($1::text[]) AND deleted_at IS NULL",
            list(memory_ids),
        )

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        conn = _postgres_tx(tx).conn
        return await conn.fetch(
            """
            SELECT m.id, m.content AS memory_content,
                   mv.content AS head_content
            FROM memories m
            LEFT JOIN memory_branches b
              ON b.memory_id = m.id AND b.name = 'main'
             AND b.deleted_at IS NULL
            LEFT JOIN memory_versions mv
              ON mv.id = b.head_version_id
             AND mv.deleted_at IS NULL
            WHERE m.id = ANY($1::text[])
              AND m.deleted_at IS NULL
            """,
            list(memory_ids),
        )

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        conn = _postgres_tx(tx).conn
        try:
            if user.role == "root":
                memories = await conn.fetch(
                    """
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE
                        m.deleted_at IS NULL
                        AND m.archived_at IS NULL
                        AND (
                            to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)
                            OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                        )
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT $2
                    """,
                    query,
                    limit,
                )
            else:
                vis_clause, vis_params = _core_read_visibility_predicate(
                    user.user_id,
                    list(user.group_ids),
                    start_param_idx=1,
                    table_alias="m",
                )
                ns_ph = f"${len(vis_params) + 1}"
                q_ph = f"${len(vis_params) + 2}"
                lim_ph = f"${len(vis_params) + 3}"
                memories = await conn.fetch(
                    f"""
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE m.deleted_at IS NULL
                      AND m.archived_at IS NULL
                      AND {vis_clause}
                      AND m.namespace = {ns_ph}
                      AND (
                          to_tsvector('english', m.content) @@ plainto_tsquery('english', {q_ph})
                          OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                      )
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT {lim_ph}
                    """,
                    *vis_params,
                    user.namespace,
                    query,
                    limit,
                )
            logger.info("[MNEMOS] Found %s memories for query '%s...'", len(memories), query[:30])
            return [{"id": memory["id"], "content": memory["content"]} for memory in memories]
        except Exception as exc:
            logger.warning("[MNEMOS] Search failed for '%s...': %s", query[:50], exc)
            return []

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
    ) -> tuple[list[Row], int]:
        conn = _postgres_tx(tx).conn
        where_parts: list[str] = ["deleted_at IS NULL"]
        if not include_archived:
            where_parts.append("archived_at IS NULL")
        params: list[Any] = []
        if category is not None:
            params.append(category)
            where_parts.append(f"category=${len(params)}")
        if subcategory is not None:
            params.append(subcategory)
            where_parts.append(f"subcategory=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=len(params) + 1,
        )
        if vis_clause:
            where_parts.append(vis_clause)
            params.extend(vis_params)
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        select_sql = (
            f"SELECT {_MEMORY_COLS} FROM memories{where_sql} "
            f"ORDER BY created DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        count_sql = f"SELECT COUNT(*) FROM memories{where_sql}"
        rows = await conn.fetch(select_sql, *params, limit, offset)
        total = await conn.fetchval(count_sql, *params)
        return list(rows), int(total or 0)

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        archived_clause = "" if include_archived else " AND archived_at IS NULL"
        if visibility.scope == VisibilityScope.ROOT_BYPASS and visibility.namespace is None:
            return await conn.fetchrow(
                f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1 AND deleted_at IS NULL{archived_clause}",
                memory_id,
            )
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=2,
        )
        sql = (
            f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1 AND deleted_at IS NULL{archived_clause} AND {vis_clause}"
        )
        return await conn.fetchrow(sql, memory_id, *vis_params)

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
        conn = _postgres_tx(tx).conn
        # $1 = memory_id, $2.. = field values, then visibility params,
        # so update_memory writes are atomic with their authorization
        # check (folded into the WHERE on the same UPDATE).
        keys = list(fields.keys())
        set_clauses = [f"{col}=${i + 2}" for i, col in enumerate(keys)]
        set_clauses.append("updated=NOW()")
        set_sql = ", ".join(set_clauses)
        values = [fields[k] for k in keys]
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=len(values) + 2,
        )
        if vis_clause:
            sql = (
                f"UPDATE memories SET {set_sql} "
                f"WHERE id=$1 AND deleted_at IS NULL AND {vis_clause} "
                f"RETURNING {_MEMORY_COLS}"
            )
            row = await conn.fetchrow(sql, memory_id, *values, *vis_params)
            if row is not None:
                await _queue_federation_nats_upsert_from_db(_postgres_tx(tx), memory_id)
            return row
        sql = f"UPDATE memories SET {set_sql} WHERE id=$1 AND deleted_at IS NULL RETURNING {_MEMORY_COLS}"
        row = await conn.fetchrow(sql, memory_id, *values)
        if row is not None:
            await _queue_federation_nats_upsert_from_db(_postgres_tx(tx), memory_id)
        return row

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        namespace_clause = "" if cross_namespace else "AND namespace=$3"
        params: list[Any] = [owner_id, content_hash]
        if not cross_namespace:
            params.append(namespace)
        return await conn.fetchrow(
            f"""
            SELECT id, last_recalled_at
            FROM memories
            WHERE owner_id=$1
              {namespace_clause}
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND content_hash=$2
            ORDER BY created ASC, id ASC
            LIMIT 1
            """,
            *params,
        )

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=2,
        )
        if vis_clause:
            sql = (
                "UPDATE memories "
                "SET recall_count = recall_count + 1, last_recalled_at = NOW() "
                f"WHERE id=$1 AND deleted_at IS NULL AND archived_at IS NULL AND {vis_clause} "
                f"RETURNING {_MEMORY_COLS}"
            )
            return await conn.fetchrow(sql, memory_id, *vis_params)
        return await conn.fetchrow(
            "UPDATE memories "
            "SET recall_count = recall_count + 1, last_recalled_at = NOW() "
            "WHERE id=$1 AND deleted_at IS NULL AND archived_at IS NULL "
            f"RETURNING {_MEMORY_COLS}",
            memory_id,
        )

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        return list(
            await conn.fetch(
                """
            SELECT
                owner_id,
                namespace,
                content_hash,
                COUNT(*)::int AS duplicate_count,
                ARRAY_AGG(id ORDER BY created ASC, id ASC) AS memory_ids,
                (ARRAY_AGG(id ORDER BY created ASC, id ASC))[1] AS canonical_id
            FROM memories
            WHERE deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND content_hash IS NOT NULL
              AND ($1::text IS NULL OR namespace=$1)
            GROUP BY owner_id, namespace, content_hash
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC
            """,
                namespace,
            )
        )

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories
            SET consolidated_into = $1,
                consolidated_at = NOW(),
                deleted_at = COALESCE(deleted_at, NOW()),
                updated = NOW()
            WHERE id = ANY($2::text[])
              AND id <> $1
              AND deleted_at IS NULL
              AND archived_at IS NULL
              AND consolidated_into IS NULL
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = $1
                    AND deleted_at IS NULL
                    AND archived_at IS NULL
                    AND consolidated_into IS NULL
              )
            """,
            canonical_id,
            list(duplicate_ids),
        )
        return _pg_result_count(result)

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
        conn = _postgres_tx(tx).conn
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=2,
        )
        if requested_by is not None:
            target_where = "id=$1 AND deleted_at IS NULL"
            if vis_clause:
                target_where = f"{target_where} AND {vis_clause}"
            audit_start = len(vis_params) + 2
            source_array = list(source) if source is not None else None
            return await conn.fetchrow(
                f"""
                WITH target AS (
                    SELECT owner_id, namespace, id, content, category, subcategory
                      FROM memories
                     WHERE {target_where}
                ), audit AS (
                    INSERT INTO deletion_log (
                        memory_id, content_hash, owner_id, namespace,
                        requested_by, requested_at, request_kind, reason, source
                    )
                    SELECT
                        id,
                        encode(digest(COALESCE(content, ''), 'sha256'), 'hex'),
                        owner_id,
                        namespace,
                        ${audit_start},
                        COALESCE(${audit_start + 1}::timestamptz, NOW()),
                        ${audit_start + 2},
                        ${audit_start + 3},
                        ${audit_start + 4}::text[]
                      FROM target
                    RETURNING 1
                )
                DELETE FROM memories m
                 USING target
                 WHERE m.id = target.id
                RETURNING
                    target.owner_id,
                    target.namespace,
                    target.id,
                    target.content,
                    target.category,
                    target.subcategory
                """,
                memory_id,
                *vis_params,
                requested_by,
                requested_at,
                request_kind,
                reason,
                source_array,
            )
        if vis_clause:
            sql = (
                "DELETE FROM memories "
                f"WHERE id=$1 AND deleted_at IS NULL AND {vis_clause} "
                "RETURNING owner_id, namespace, id, content, category, subcategory"
            )
            return await conn.fetchrow(sql, memory_id, *vis_params)
        return await conn.fetchrow(
            "DELETE FROM memories WHERE id=$1 AND deleted_at IS NULL "
            "RETURNING owner_id, namespace, id, content, category, subcategory",
            memory_id,
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
        search_trace_id: str | None = None,
        search_started_at: float | None = None,
    ) -> list[Row]:
        # Fail loudly on dim mismatches before the asyncpg cast layer
        # produces a vague DataError. Mirrors SqliteMemoryRepository.
        self._require_dim(embedding, "semantic_search")
        conn = _postgres_tx(tx).conn
        # $1 is the embedding vector, used in both SELECT (for the
        # similarity score) and ORDER BY. Passing as a parameter (not
        # interpolated) eliminates injection risk from a poisoned
        # embedding response.
        vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"
        params: list[Any] = [vec_str]
        conditions: list[str] = ["embedding IS NOT NULL", "deleted_at IS NULL"]
        if not include_archived:
            conditions.append("archived_at IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                params.append(val)
                conditions.append(f"{col}=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=len(params) + 1,
        )
        if vis_clause:
            conditions.append(vis_clause)
            params.extend(vis_params)
        candidate_limit = limit
        if boost_recency:
            candidate_limit = max(limit, min(limit * 4, 200))
        params.append(candidate_limit)
        recency_select = ""
        if boost_recency:
            recency_select = (
                ", embedding::text AS _embedding_text, "
                "EXP(-GREATEST(EXTRACT(EPOCH FROM (timezone('UTC', now()) - "
                "COALESCE(last_recalled_at, updated, created))), 0) / "
                f"{_RECENCY_E_FOLD_SECONDS}.0) AS _recency_boost"
            )
        sql = (
            f"SELECT {_MEMORY_COLS}, 1 - (embedding <=> $1::vector) AS similarity"
            f"{recency_select} "
            "FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY embedding <=> $1::vector LIMIT ${len(params)}"
        )
        rows = list(await conn.fetch(sql, *params))
        _log_search_phase(search_trace_id, search_started_at, "ann_scan")
        if not boost_recency or len(rows) <= 1:
            _log_search_phase(search_trace_id, search_started_at, "rerank")
            return rows[:limit]

        candidates = [_parse_pgvector_text(row.get("_embedding_text")) for row in rows]
        recency_boost = [
            0.0 if (row.get("superseded_by") or row.get("consolidated_into")) else float(row.get("_recency_boost") or 0.0)
            for row in rows
        ]
        weight_recency = max(0.0, min(1.0, float(recency_weight)))
        # Recency is an additive ordering boost, not a replacement for raw
        # semantic similarity. Keep cosine at full strength so old but very
        # strong semantic hits stay in the candidate window while recent near
        # ties can still move ahead.
        weight_cos = 1.0
        ranking = _rerank_composite(
            embedding,
            candidates,
            recency_boost,
            weight_cos,
            weight_recency,
            0,
        )
        reranked: list[Row] = []
        for idx, composite_score in ranking:
            row = rows[idx]
            enriched = dict(row.items()) if hasattr(row, "items") else dict(row)
            # Preserve the raw semantic similarity for min_score/OOD gates.
            # Recency is an ordering-only key; overwriting ``similarity``
            # would make a recent but semantically weak row bypass floors.
            enriched["_composite_score"] = composite_score
            enriched["superseded_by"] = enriched.get("superseded_by") or enriched.get("consolidated_into")
            reranked.append(enriched)
        reranked.sort(key=lambda r: bool(r.get("superseded_by") or r.get("consolidated_into")))
        _log_search_phase(search_trace_id, search_started_at, "rerank")
        return reranked[:limit]

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
        # plainto_tsquery treats user input as plain text — tsquery
        # operators like |, !, & are not interpreted. Prevents tsquery
        # operator injection.
        conn = _postgres_tx(tx).conn
        clean_query = query.strip()
        # FTS path: $1=query, $2=limit, filter+visibility params at $3+
        params: list[Any] = [clean_query, limit]
        conditions: list[str] = [
            "to_tsvector('english', content) @@ plainto_tsquery('english', $1)",
            "deleted_at IS NULL",
        ]
        if not include_archived:
            conditions.append("archived_at IS NULL")
        for col, val in (
            ("category", category),
            ("subcategory", subcategory),
            ("source_provider", source_provider),
            ("source_model", source_model),
            ("source_agent", source_agent),
        ):
            if val is not None:
                params.append(val)
                conditions.append(f"{col}=${len(params)}")
        vis_clause, vis_params, _ = _render_postgres_visibility(
            visibility,
            start_idx=len(params) + 1,
        )
        if vis_clause:
            conditions.append(vis_clause)
            params.extend(vis_params)
        sql = (
            f"SELECT {_MEMORY_COLS}, "
            "ts_rank(to_tsvector('english', content), "
            "plainto_tsquery('english', $1)) AS rank "
            "FROM memories "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY rank DESC LIMIT $2"
        )

        async def _ilike_search() -> list[Row]:
            like_q = f"%{query}%"
            ilike_params: list[Any] = [like_q, limit]
            ilike_conditions: list[str] = ["content ILIKE $1", "deleted_at IS NULL"]
            if not include_archived:
                ilike_conditions.append("archived_at IS NULL")
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    ilike_params.append(val)
                    ilike_conditions.append(f"{col}=${len(ilike_params)}")
            ilike_vis_clause, ilike_vis_params, _ = _render_postgres_visibility(
                visibility,
                start_idx=len(ilike_params) + 1,
            )
            if ilike_vis_clause:
                ilike_conditions.append(ilike_vis_clause)
                ilike_params.extend(ilike_vis_params)
            ilike_sql = (
                f"SELECT {_MEMORY_COLS} FROM memories "
                f"WHERE {' AND '.join(ilike_conditions)} "
                "ORDER BY created DESC LIMIT $2"
            )
            return list(await conn.fetch(ilike_sql, *ilike_params))

        try:
            rows = list(await conn.fetch(sql, *params))
            if rows:
                return rows
            return await _ilike_search()
        except Exception:
            # ILIKE fallback: same predicate shape, $1 becomes the LIKE
            # pattern, $2 still the limit. We also use it for zero FTS
            # results so exact-substring searches survive stale FTS
            # config or stop-word/tokenization misses.
            return await _ilike_search()

    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        conn = _postgres_tx(tx).conn
        total = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
        native = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NULL AND deleted_at IS NULL",
        )
        federated = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL AND deleted_at IS NULL",
        )
        peer_rows = await conn.fetch(
            "SELECT federation_source, COUNT(*) AS cnt FROM memories "
            "WHERE federation_source IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY federation_source ORDER BY cnt DESC",
        )
        cat_rows = await conn.fetch(
            "SELECT category, COUNT(*) AS cnt FROM memories WHERE deleted_at IS NULL GROUP BY category",
        )
        sub_rows = await conn.fetch(
            "SELECT category, subcategory, COUNT(*) AS cnt FROM memories "
            "WHERE subcategory IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY category, subcategory ORDER BY cnt DESC",
        )
        avg_quality = await conn.fetchval(
            "SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL AND deleted_at IS NULL",
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


class PostgresKGRepository(KGRepository):
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
        conn = _postgres_tx(tx).conn
        return await _fetch_sidecar(
            conn,
            table="kg_triples",
            columns=(
                "id, subject, predicate, object, subject_type, "
                "object_type, valid_from, valid_until, memory_id, "
                "confidence, created, owner_id, namespace"
            ),
            memory_id_column="memory_id",
            memory_ids=memory_ids,
            effective_owner=effective_owner,
            effective_ns=effective_ns,
            bound_to_memories=True,
            hard_limit=hard_limit,
            null_ok=include_unattached,
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
        conn = _postgres_tx(tx).conn
        return await conn.execute(
            """
            INSERT INTO kg_triples (
                id, subject, predicate, object,
                subject_type, object_type,
                valid_from, valid_until,
                memory_id, confidence, created,
                owner_id, namespace
            )
            VALUES (
                $1, $2, $3, $4,
                $5, $6,
                COALESCE($7, NOW()), $8,
                $9, COALESCE($10, 1.0),
                COALESCE($11, NOW()),
                $12, $13
            )
            ON CONFLICT (id) DO NOTHING
            """,
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
        )

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        conn = _postgres_tx(tx).conn
        return await conn.fetchrow(
            "SELECT subject, predicate, object, subject_type, "
            "object_type, memory_id, confidence, owner_id, "
            "namespace, valid_from, valid_until, created "
            "FROM kg_triples WHERE id = $1 AND deleted_at IS NULL",
            triple_id,
        )


class PostgresVersionRepository(VersionRepository):
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        # ``id`` and ``parent_version_id`` are PG UUID columns; cast to
        # text so callers (and the persistence-parity tests) see the
        # same str shape SQLite returns. Without the casts asyncpg
        # decodes UUIDs as ``uuid.UUID`` instances and equality against
        # ``str(uuid)`` fails.
        return await _fetch_sidecar(
            conn,
            table="memory_versions",
            columns=(
                "id::text AS id, memory_id, version_num, content, category, "
                "subcategory, metadata, verbatim_content, owner_id, "
                "namespace, permission_mode, source_model, source_provider, "
                "source_session, source_agent, snapshot_at, snapshot_by, "
                "change_type, commit_hash, "
                "parent_version_id::text AS parent_version_id, branch, "
                "merge_parents"
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
        conn = _postgres_tx(tx).conn
        return await conn.fetch(
            "SELECT id::text AS id, memory_id, owner_id, namespace "
            "FROM memory_versions WHERE id = ANY($1::uuid[]) "
            "AND deleted_at IS NULL",
            list(version_ids),
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
        conn = _postgres_tx(tx).conn
        return await conn.execute(
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
                $1::uuid, $2, $3, $4,
                $5, $6, $7::jsonb, $8,
                $9, $10, COALESCE($11, 600),
                $12, $13, $14, $15,
                COALESCE($16, NOW()), $17, COALESCE($18, 'create'),
                $19, $20::uuid, COALESCE($21, 'main'), $22::uuid[]
            )
            ON CONFLICT (id) DO NOTHING
            """,
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
            merge_parents,
        )

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        conn = _postgres_tx(tx).conn
        return await conn.fetchrow(
            "SELECT memory_id, owner_id, namespace, "
            "version_num, content, commit_hash, "
            "parent_version_id::text AS parent_version_id, "
            "branch, merge_parents, category, subcategory, "
            "metadata, verbatim_content, permission_mode, "
            "source_model, source_provider, source_session, "
            "source_agent, snapshot_at, snapshot_by, "
            "change_type "
            "FROM memory_versions WHERE id = $1::uuid AND deleted_at IS NULL",
            version_id,
        )


class PostgresBranchRepository(BranchRepository):
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]:
        conn = _postgres_tx(tx).conn

        async def _fetch_branch_start_by_commit() -> Any | None:
            if user.role == "root":
                return await conn.fetchrow(
                    "SELECT id, commit_hash FROM memory_versions "
                    "WHERE memory_id = $1 AND commit_hash = $2 "
                    "AND deleted_at IS NULL",
                    memory_id,
                    from_commit,
                )

            vis_clause, vis_params = _core_version_visibility_predicate(
                user.user_id,
                start_param_idx=3,
            )
            ns_ph = f"${len(vis_params) + 3}"
            return await conn.fetchrow(
                "SELECT id, commit_hash FROM memory_versions "
                "WHERE memory_id = $1 AND commit_hash = $2 "
                f"AND deleted_at IS NULL AND {vis_clause} AND namespace = {ns_ph}",
                memory_id,
                from_commit,
                *vis_params,
                user.namespace,
            )

        async def _fetch_main_branch_start() -> Any | None:
            if user.role == "root":
                return await conn.fetchrow(
                    """
                    SELECT mv.id, mv.commit_hash
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = 'main'
                      AND mv.deleted_at IS NULL
                      AND mb.deleted_at IS NULL
                    """,
                    memory_id,
                )

            vis_clause, vis_params = _core_version_visibility_predicate(
                user.user_id,
                start_param_idx=2,
                table_alias="mv",
            )
            ns_ph = f"${len(vis_params) + 2}"
            return await conn.fetchrow(
                f"""
                SELECT mv.id, mv.commit_hash
                FROM memory_versions mv
                INNER JOIN memory_branches mb ON mb.memory_id = mv.memory_id AND mb.head_version_id = mv.id
                WHERE mv.memory_id = $1 AND mb.name = 'main'
                  AND mv.deleted_at IS NULL
                  AND mb.deleted_at IS NULL
                  AND {vis_clause} AND mv.namespace = {ns_ph}
                """,
                memory_id,
                *vis_params,
                user.namespace,
            )

        async def _fetch_existing_branch() -> Any | None:
            if user.role == "root":
                return await conn.fetchrow(
                    "SELECT mb.head_version_id, mv.commit_hash "
                    "FROM memory_branches mb "
                    "INNER JOIN memory_versions mv "
                    "    ON mv.id = mb.head_version_id "
                    "   AND mv.memory_id = mb.memory_id "
                    "WHERE mb.memory_id = $1 AND mb.name = $2 "
                    "AND mb.deleted_at IS NULL AND mv.deleted_at IS NULL",
                    memory_id,
                    name,
                )

            vis_clause, vis_params = _core_version_visibility_predicate(
                user.user_id,
                start_param_idx=3,
                table_alias="mv",
            )
            ns_ph = f"${len(vis_params) + 3}"
            return await conn.fetchrow(
                "SELECT mb.head_version_id, mv.commit_hash "
                "FROM memory_branches mb "
                "INNER JOIN memory_versions mv "
                "    ON mv.id = mb.head_version_id "
                "   AND mv.memory_id = mb.memory_id "
                f"   AND {vis_clause} "
                f"   AND mv.namespace = {ns_ph} "
                "WHERE mb.memory_id = $1 AND mb.name = $2 "
                "AND mb.deleted_at IS NULL AND mv.deleted_at IS NULL",
                memory_id,
                name,
                *vis_params,
                user.namespace,
            )

        async def _handle_existing_branch(start: Any) -> dict[str, Any]:
            existing = await _fetch_existing_branch()
            if existing is None:
                return {
                    "success": False,
                    "error": (
                        "branch exists but its head is not visible "
                        "or points at a foreign memory version; "
                        "reconciliation required"
                    ),
                }

            head_matches = existing["head_version_id"] == start["id"]
            if from_commit is None or head_matches:
                return {
                    "success": True,
                    "memory_id": memory_id,
                    "branch": name,
                    "commit_hash": existing["commit_hash"],
                    "created_by": user.user_id,
                    "idempotent": True,
                }

            return {
                "success": False,
                "error": ("branch already exists at a different head; refusing to silently move it"),
            }

        async with conn.transaction():
            # Lock the live memory row for the duration of the transaction.
            # This closes the TOCTOU between auth check and branch insert.
            if user.role == "root":
                live = await conn.fetchrow(
                    "SELECT 1 FROM memories WHERE id = $1 AND deleted_at IS NULL FOR SHARE",
                    memory_id,
                )
            else:
                live = await conn.fetchrow(
                    "SELECT 1 FROM memories WHERE id = $1 "
                    "AND deleted_at IS NULL "
                    "AND owner_id = $2 AND namespace = $3 FOR SHARE",
                    memory_id,
                    user.user_id,
                    user.namespace,
                )
            if not live:
                return {"success": False, "error": "Memory not found"}

            if from_commit:
                start = await _fetch_branch_start_by_commit()
                if not start:
                    return {"success": False, "error": "Commit not found"}
            else:
                start = await _fetch_main_branch_start()
                if not start:
                    return {"success": False, "error": "main branch not found"}

            inserted = await conn.fetchrow(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (memory_id, name) DO NOTHING
                RETURNING head_version_id
                """,
                memory_id,
                name,
                start["id"],
                user.user_id,
            )

        if inserted is None:
            return await _handle_existing_branch(start)

        return {
            "success": True,
            "memory_id": memory_id,
            "branch": name,
            "commit_hash": start["commit_hash"],
            "created_by": user.user_id,
        }

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        conn = _postgres_tx(tx).conn
        await conn.execute(
            "DELETE FROM memory_branches WHERE memory_id = ANY($1::text[])",
            list(memory_ids),
        )

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if authorized_version_uuids is not None:
            return await conn.fetch(
                """
                SELECT DISTINCT ON (memory_id, branch)
                    memory_id, branch, id::text AS head_version_id
                FROM memory_versions
                WHERE memory_id = ANY($1::text[])
                  AND id = ANY($2::uuid[])
                  AND deleted_at IS NULL
                ORDER BY memory_id, branch, version_num DESC
                """,
                list(memory_ids),
                list(authorized_version_uuids),
            )
        return await conn.fetch(
            """
            SELECT DISTINCT ON (memory_id, branch)
                memory_id, branch, id::text AS head_version_id
            FROM memory_versions
            WHERE memory_id = ANY($1::text[])
              AND deleted_at IS NULL
            ORDER BY memory_id, branch, version_num DESC
            """,
            list(memory_ids),
        )

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        conn = _postgres_tx(tx).conn
        await conn.execute(
            """
            INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
            VALUES ($1, $2, $3, NULL)
            ON CONFLICT (memory_id, name) DO UPDATE
            SET head_version_id = EXCLUDED.head_version_id
            """,
            memory_id,
            branch,
            head_version_id,
        )


class PostgresCompressionRepository(CompressionRepository):
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        return await _fetch_sidecar(
            conn,
            table="memory_compressed_variants",
            columns=(
                "memory_id, owner_id, winner_candidate_id, engine_id, "
                "engine_version, compressed_content, compressed_tokens, "
                "compression_ratio, quality_score, composite_score, "
                "scoring_profile, judge_model, selected_at"
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
        conn = _postgres_tx(tx).conn
        exists = await conn.fetchval(
            "SELECT 1 FROM memory_compression_candidates WHERE id = $1::uuid AND memory_id = $2 AND owner_id = $3",
            candidate_id,
            memory_id,
            owner_id,
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
        conn = _postgres_tx(tx).conn
        return await conn.execute(
            """
            INSERT INTO memory_compressed_variants (
                memory_id, owner_id, winner_candidate_id,
                engine_id, engine_version, compressed_content,
                compressed_tokens, compression_ratio,
                quality_score, composite_score,
                scoring_profile, judge_model, selected_at
            )
            VALUES (
                $1, $2, $3::uuid,
                $4, $5, $6,
                $7, $8,
                $9, $10,
                COALESCE($11, 'balanced'), $12,
                COALESCE($13, NOW())
            )
            ON CONFLICT (memory_id) DO NOTHING
            """,
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
        )

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = _postgres_tx(tx).conn
        return await conn.fetchrow(
            "SELECT owner_id, winner_candidate_id::text "
            "AS winner_candidate_id, engine_id, "
            "engine_version, compressed_content, "
            "compressed_tokens, compression_ratio, "
            "quality_score, composite_score, "
            "scoring_profile, judge_model, selected_at "
            "FROM memory_compressed_variants "
            "WHERE memory_id = $1",
            memory_id,
        )

    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        conn = _postgres_tx(tx).conn
        total = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM memory_compressed_variants",
            )
            or 0
        )
        avg_ratio = await conn.fetchval(
            "SELECT AVG(v.compression_ratio) FROM memory_compressed_variants v",
        )
        unreviewed = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM memory_compressed_variants WHERE quality_score IS NULL",
            )
            or 0
        )
        return CompressionStatsRow(
            total_compressions=int(total),
            average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
            unreviewed_compressions=int(unreviewed),
        )


# ── compression-queue SQL (canonical) ───────────────────────────────────────
# These are the authoritative Postgres queue statements, moved here from
# mnemos/domain/compression/worker_contest.py + mnemos/domain/admin_lifecycle_repo.py
# so the queue mechanics live behind the persistence ABC
# (CompressionQueueRepository) and run identically on every backend
# (architectural law mem_1780005765033). CHILD C rewires the worker +
# admin routes onto this repo and deletes the domain-layer copies. Semantics
# preserved verbatim (codex round-28 hardened terminalization).
_PG_DEQUEUE_SQL = """
WITH next AS (
    SELECT id
    FROM memory_compression_queue
    WHERE status = 'pending'
    ORDER BY priority DESC, enqueued_at
    FOR UPDATE SKIP LOCKED
    LIMIT $1
)
UPDATE memory_compression_queue q
SET status      = 'running',
    started_at  = NOW(),
    attempts    = q.attempts + 1
FROM next
WHERE q.id = next.id
RETURNING q.id, q.memory_id, q.owner_id, q.reason,
          q.scoring_profile, q.attempts
"""

_PG_MARK_DONE_SQL = """
UPDATE memory_compression_queue
SET status      = 'done',
    finished_at = NOW(),
    error       = NULL
WHERE id = $1::uuid
"""

_PG_MARK_FAILED_SQL = """
UPDATE memory_compression_queue
SET status      = 'failed',
    finished_at = NOW(),
    error       = $2
WHERE id = $1::uuid
"""

# Reclaim 'running' rows stranded past the stale threshold. Terminalization:
#   * attempts >= max AND error is a recorded content failure (NOT NULL,
#     not an infra_retry: breadcrumb) -> 'failed'.
#   * attempts >= max but error NULL / infra_retry: -> reset 'pending' AND
#     decrement attempts (wedged-pool path).
#   * attempts < max -> reset 'pending', attempts preserved.
_PG_SWEEP_STALE_SQL = """
WITH stale AS (
    SELECT id, attempts, error
    FROM memory_compression_queue
    WHERE status = 'running'
      AND (started_at IS NULL
           OR started_at < NOW() - ($1::int * INTERVAL '1 second'))
    FOR UPDATE SKIP LOCKED
), classified AS (
    SELECT id,
           attempts,
           error,
           (attempts >= $2
            AND error IS NOT NULL
            AND error NOT LIKE 'infra_retry:%'
           ) AS terminalize
    FROM stale
)
UPDATE memory_compression_queue q
SET status      = CASE WHEN c.terminalize THEN 'failed' ELSE 'pending' END,
    started_at  = CASE WHEN c.terminalize THEN q.started_at ELSE NULL END,
    finished_at = CASE WHEN c.terminalize THEN NOW()        ELSE NULL END,
    attempts    = CASE
                    WHEN c.terminalize THEN q.attempts
                    WHEN c.attempts >= $2 THEN GREATEST(c.attempts - 1, 0)
                    ELSE q.attempts
                  END,
    error       = CASE
                    WHEN c.terminalize
                      THEN 'stranded_running: exceeded stale threshold after '
                           || c.attempts || ' attempts'
                    WHEN c.attempts >= $2
                      THEN 'infra_retry: stale-recovered without content-failure breadcrumb'
                    ELSE NULL
                  END
FROM classified c
WHERE q.id = c.id
RETURNING q.id, q.status, c.attempts
"""


class PostgresCompressionQueueRepository(CompressionQueueRepository):
    """Postgres impl of the v3.1 compression work queue (job 019e7049
    CHILD B). Wraps the canonical queue SQL behind the ABC with NO
    behaviour change — the contest semantics are identical to the prior
    asyncpg-direct path in worker_contest.py / admin_lifecycle_repo.py.
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
        conn = _postgres_tx(tx).conn
        known = await conn.fetch(
            "SELECT id, owner_id FROM memories WHERE id = ANY($1::text[]) AND deleted_at IS NULL",
            memory_ids,
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
            existing = await conn.fetchval(
                "SELECT 1 FROM memory_compression_queue WHERE memory_id = $1 AND status = 'pending' LIMIT 1",
                mid,
            )
            if existing:
                continue
            await conn.execute(
                "INSERT INTO memory_compression_queue "
                "(memory_id, owner_id, reason, priority, scoring_profile) "
                "VALUES ($1, $2, $3, $4, $5)",
                mid,
                owner_by_id[mid],
                reason,
                priority,
                scoring_profile,
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
        conn = _postgres_tx(tx).conn
        where_parts: list[str] = ["m.deleted_at IS NULL"]
        params: list[Any] = []
        if only_uncompressed:
            where_parts.append("NOT EXISTS (SELECT 1 FROM memory_compressed_variants v WHERE v.memory_id = m.id)")
        if category is not None:
            params.append(category)
            where_parts.append(f"m.category = ${len(params)}")
        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

        params.extend([reason, priority, scoring_profile, limit])
        reason_idx = len(params) - 3
        priority_idx = len(params) - 2
        profile_idx = len(params) - 1
        limit_idx = len(params)

        sql = (
            "INSERT INTO memory_compression_queue "
            "(memory_id, owner_id, reason, priority, scoring_profile) "
            "SELECT m.id, m.owner_id, "
            f"${reason_idx}, ${priority_idx}, ${profile_idx} "
            f"FROM memories m{where_sql} "
            "ORDER BY LENGTH(m.content) DESC "
            f"LIMIT ${limit_idx}"
        )
        return _pg_result_count(await conn.execute(sql, *params))

    async def dequeue_compression(
        self,
        tx: Transaction,
        *,
        limit: int,
    ) -> list[Row]:
        if limit <= 0:
            return []
        conn = _postgres_tx(tx).conn
        rows = await conn.fetch(_PG_DEQUEUE_SQL, int(limit))
        out: list[Row] = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])  # normalise UUID -> str for the ABC contract
            out.append(d)
        return out

    async def mark_compression_done(
        self,
        tx: Transaction,
        *,
        queue_id: str,
    ) -> None:
        await _postgres_tx(tx).conn.execute(_PG_MARK_DONE_SQL, str(queue_id))

    async def mark_compression_failed(
        self,
        tx: Transaction,
        *,
        queue_id: str,
        error: str,
    ) -> None:
        await _postgres_tx(tx).conn.execute(_PG_MARK_FAILED_SQL, str(queue_id), error)

    async def sweep_stale_compression(
        self,
        tx: Transaction,
        *,
        stale_threshold_secs: int,
        max_attempts: int,
    ) -> int:
        rows = await _postgres_tx(tx).conn.fetch(_PG_SWEEP_STALE_SQL, int(stale_threshold_secs), int(max_attempts))
        return len(rows)


class PostgresWebhookRepository(WebhookRepository):
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
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO webhook_subscriptions (id, url, events, secret, owner_id, namespace)
            VALUES ($1::uuid, $2, $3::text[], $4, $5, $6)
            """,
            subscription_id,
            url,
            list(events),
            secret or "",
            owner_id,
            namespace,
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
        conn = _postgres_tx(tx).conn
        query = """
            SELECT id, url, owner_id, namespace
            FROM webhook_subscriptions
            WHERE NOT revoked AND $1 = ANY(events)
        """
        args: list[Any] = [event_type]
        if owner_id is not None:
            args.append(owner_id)
            query += f" AND owner_id = ${len(args)}"
        if namespace is not None:
            args.append(namespace)
            query += f" AND namespace = ${len(args)}"
        subscriptions = await conn.fetch(query, *args)
        body = json.dumps(
            {"event": event_type, "timestamp": datetime.now(timezone.utc).isoformat(), "data": payload},
            separators=(",", ":"),
            sort_keys=True,
        )
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        delivery_ids: list[str] = []
        for sub in subscriptions:
            delivery_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO webhook_deliveries
                  (id, subscription_id, event_type, payload, payload_hash, status, writer_revision)
                VALUES ($1::uuid, $2, $3, $4, $5, 'pending', $6)
                """,
                delivery_id,
                sub["id"],
                event_type,
                body,
                body_hash,
                webhook_constants.NEW_CODE_WRITER_REVISION,
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
            await persistence_nats_events.publish_webhook_outbox_insert(
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
            return await _postgres_tx(tx).conn.fetch("SELECT * FROM webhook_deliveries ORDER BY created ASC")
        return await _postgres_tx(tx).conn.fetch(
            "SELECT * FROM webhook_deliveries WHERE subscription_id = $1::uuid ORDER BY created ASC",
            subscription_id,
        )


class PostgresConsultationAuditRepository(ConsultationAuditRepository):
    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        conn = _postgres_tx(tx).conn
        rows = await conn.fetch(
            """
            SELECT
                provider, model_id, display_name,
                capabilities,
                input_cost_per_mtok, output_cost_per_mtok,
                COALESCE(graeae_weight, 0) AS graeae_weight,
                context_window
            FROM model_registry
            WHERE available = true
            AND deprecated = false
            """
        )
        return choose_recommended_model(list(rows), task_type, cost_budget, quality_floor)

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
        conn = _postgres_tx(tx).conn
        try:
            row = await conn.fetchrow(
                "SELECT provider FROM model_registry WHERE model_id = $1   AND available = true AND deprecated = false",
                model,
            )
            if row is not None:
                return row["provider"]

            if "/" in model:
                head, tail = model.split("/", 1)
                head_registry = GRAEAE_REGISTRY_MAP.get(head, {"registry_provider": head})["registry_provider"]
                row = await conn.fetchrow(
                    "SELECT provider FROM model_registry "
                    "WHERE provider = $1 AND model_id = $2 "
                    "  AND available = true AND deprecated = false",
                    head_registry,
                    tail,
                )
                if row is not None:
                    return row["provider"]
        except Exception as exc:
            logger.warning("[MNEMOS] model_registry lookup failed for model=%s: %s", model, exc)
        return None

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        conn = _postgres_tx(tx).conn
        try:
            return await conn.fetch(
                """
                SELECT provider, model_id, display_name
                FROM model_registry
                WHERE available = true AND deprecated = false
                ORDER BY graeae_weight DESC NULLS LAST, model_id ASC
                """
            )
        except Exception as exc:
            logger.warning(
                "[/v1/models] model_registry query failed, returning an empty discovery list: %s",
                exc,
            )
            return []

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        conn = _postgres_tx(tx).conn
        row = await conn.fetchrow(
            """
            SELECT provider
            FROM model_registry
            WHERE model_id = $1
              AND available = true
              AND deprecated = false
            LIMIT 1
            """,
            model_id,
        )
        if row is not None:
            return row["provider"]
        return None

    # ── KNEMON Step 2: pricing ingest from llm_provider_registry.json ──────────

    async def upsert_model_pricing(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        price_in: float,
        price_out: float,
        price_cached: float,
    ) -> tuple[int, dict | None]:
        """Upsert price columns into model_registry. Returns (rows_updated, old_prices_or_None)."""
        conn = _postgres_tx(tx).conn
        # Read current prices to detect change
        row = await conn.fetchrow(
            "SELECT price_in, price_out, price_cached FROM model_registry WHERE provider = $1 AND model_id = $2",
            provider,
            model_id,
        )
        if row is None:
            return 0, None

        old = {
            "price_in": float(row["price_in"] or 0),
            "price_out": float(row["price_out"] or 0),
            "price_cached": float(row["price_cached"] or 0),
        }
        # Only update if pricing actually changed
        if (
            abs(old["price_in"] - price_in) < 0.000001
            and abs(old["price_out"] - price_out) < 0.000001
            and abs(old["price_cached"] - price_cached) < 0.000001
        ):
            return 0, None

        result = await conn.execute(
            "UPDATE model_registry SET price_in = $1, price_out = $2, "
            "price_cached = $3, price_updated_at = NOW() "
            "WHERE provider = $4 AND model_id = $5",
            price_in,
            price_out,
            price_cached,
            provider,
            model_id,
        )
        n = _pg_result_count(result)
        return n, old

    async def write_price_history(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        price_in: float,
        price_out: float,
        price_cached: float,
        prices: dict | None = None,
    ) -> None:
        """Write a price_history row for audit trail."""
        conn = _postgres_tx(tx).conn
        await conn.execute(
            "INSERT INTO price_history (id, provider, model_id, price_in, "
            "price_out, price_cached, prices, recorded_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())",
            str(uuid.uuid4()),
            provider,
            model_id,
            price_in,
            price_out,
            price_cached,
            json.dumps(prices or {}),
        )


class PostgresOAuthRepository(OAuthRepository):
    async def list_enabled_providers(self, tx: Transaction) -> list[Row]:
        return await _postgres_tx(tx).conn.fetch(
            "SELECT name, display_name, kind, enabled FROM oauth_providers WHERE enabled=TRUE ORDER BY display_name"
        )

    async def get_provider(self, tx: Transaction, name: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT name, kind, issuer_url, client_id, client_secret, scope, "
            "authorize_url, token_url, userinfo_url, enabled "
            "FROM oauth_providers WHERE name=$1",
            name,
        )

    async def provision_or_link_user(
        self,
        tx: Transaction,
        *,
        provider: str,
        external_id: str,
        claims: dict[str, Any],
    ) -> tuple[str, str]:
        conn = _postgres_tx(tx).conn
        raw_claims = json.dumps(claims)
        existing = await conn.fetchrow(
            "SELECT id, user_id FROM oauth_identities WHERE provider=$1 AND external_id=$2",
            provider,
            external_id,
        )
        if existing:
            await conn.execute(
                "UPDATE oauth_identities SET last_login_at=NOW(), raw_claims=$2::jsonb WHERE id=$1",
                existing["id"],
                raw_claims,
            )
            return existing["user_id"], str(existing["id"])

        email = claims.get("email")
        display_name = claims.get("name") or claims.get("preferred_username")
        email_verified_claim = claims.get("email_verified")
        if isinstance(email_verified_claim, bool):
            email_verified = email_verified_claim
        elif isinstance(email_verified_claim, str):
            email_verified = email_verified_claim.strip().lower() == "true"
        else:
            email_verified = False

        user_id = None
        if email and email_verified:
            link_target = await conn.fetchrow("SELECT id FROM users WHERE email=$1", email)
            if link_target:
                user_id = link_target["id"]
        if user_id is None:
            user_id = _mint_user_id(provider, external_id)
            await conn.execute(
                "INSERT INTO users (id, display_name, email, role) "
                "VALUES ($1, $2, $3, 'user') ON CONFLICT (id) DO NOTHING",
                user_id,
                display_name,
                email,
            )
        identity_id = await conn.fetchval(
            "INSERT INTO oauth_identities "
            "(user_id, provider, external_id, email, display_name, raw_claims, last_login_at) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW()) RETURNING id",
            user_id,
            provider,
            external_id,
            email,
            display_name,
            raw_claims,
        )
        return user_id, str(identity_id)

    async def create_session(
        self,
        tx: Transaction,
        *,
        session_id: str,
        user_id: str,
        identity_id: str | None,
        expires_at: Any,
        user_agent: str,
        ip_address: str | None,
    ) -> str:
        await _postgres_tx(tx).conn.execute(
            "INSERT INTO oauth_sessions "
            "(session_id, user_id, identity_id, expires_at, user_agent, ip_address) "
            "VALUES ($1, $2, $3::uuid, $4, $5, $6::inet)",
            session_id,
            user_id,
            identity_id,
            expires_at,
            user_agent,
            ip_address,
        )
        return session_id

    async def revoke_session(self, tx: Transaction, session_id: str) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "UPDATE oauth_sessions SET revoked=TRUE, revoked_at=NOW() WHERE session_id=$1 AND NOT revoked",
            session_id,
        )
        return _pg_result_count(result) > 0

    async def revoke_all_sessions(self, tx: Transaction, user_id: str) -> int:
        result = await _postgres_tx(tx).conn.execute(
            "UPDATE oauth_sessions SET revoked=TRUE, revoked_at=NOW() WHERE user_id=$1 AND NOT revoked",
            user_id,
        )
        return _pg_result_count(result)

    async def get_identity_for_session(self, tx: Transaction, session_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT i.id::text AS id, i.user_id, i.provider, i.external_id, i.email, "
            "i.display_name, i.last_login_at, i.created "
            "FROM oauth_sessions s JOIN oauth_identities i ON i.id = s.identity_id "
            "WHERE s.session_id=$1 AND NOT s.revoked",
            session_id,
        )


class PostgresAclRepository(AclRepository):
    async def grant_acl(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        principal: str,
        perm: int,
        granted_by: str | None,
    ) -> Row:
        conn = _postgres_tx(tx).conn
        return await conn.fetchrow(
            "INSERT INTO memory_acl (memory_id, principal, perm, granted_by) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (memory_id, principal) DO UPDATE "
            "SET perm = EXCLUDED.perm, granted_by = EXCLUDED.granted_by "
            "RETURNING memory_id, principal, perm, granted_by, created_at",
            memory_id,
            principal,
            perm,
            granted_by,
        )

    async def revoke_acl(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        principal: str,
    ) -> bool:
        status = await _postgres_tx(tx).conn.execute(
            "DELETE FROM memory_acl WHERE memory_id = $1 AND principal = $2",
            memory_id,
            principal,
        )
        return status.upper().startswith("DELETE") and not status.endswith(" 0")

    async def list_acl(self, tx: Transaction, memory_id: str) -> list[Row]:
        return await _postgres_tx(tx).conn.fetch(
            "SELECT memory_id, principal, perm, granted_by, created_at "
            "FROM memory_acl WHERE memory_id = $1 ORDER BY principal",
            memory_id,
        )

    async def is_group_admin(
        self,
        tx: Transaction,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        row = await _postgres_tx(tx).conn.fetchrow(
            "SELECT 1 FROM user_groups WHERE user_id = $1 AND group_id = $2 AND is_admin = TRUE",
            user_id,
            group_id,
        )
        return row is not None


class PostgresSessionsRepository(SessionsRepository):
    async def create_session(
        self,
        tx: Transaction,
        *,
        user_id: str,
        namespace: str,
        model: str,
        initial_context: str | None,
    ) -> Row:
        conn = _postgres_tx(tx).conn
        session_id = await conn.fetchval(
            "INSERT INTO sessions (user_id, namespace, model) VALUES ($1, $2, $3) RETURNING id",
            user_id,
            namespace,
            model,
        )
        if initial_context:
            await conn.execute(
                "INSERT INTO session_messages (session_id, role, content) VALUES ($1, 'system', $2)",
                session_id,
                initial_context,
            )
        return await conn.fetchrow(
            "SELECT id, created_at, model FROM sessions "
            "WHERE id=$1 AND user_id=$2 AND namespace=$3 AND deleted_at IS NULL",
            session_id,
            user_id,
            namespace,
        )

    async def get_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT * FROM sessions WHERE id=$1 AND user_id=$2 AND namespace=$3 AND deleted_at IS NULL",
            session_id,
            user_id,
            namespace,
        )

    async def list_injected_memory_ids(self, tx: Transaction, session_id: str, limit: int = 10) -> list[str]:
        rows = await _postgres_tx(tx).conn.fetch(
            "SELECT memory_id FROM session_memory_injections WHERE session_id=$1 AND deleted_at IS NULL "
            "GROUP BY memory_id ORDER BY MAX(injection_timestamp) DESC LIMIT $2",
            session_id,
            limit,
        )
        return [r["memory_id"] for r in rows]

    async def add_message(
        self,
        tx: Transaction,
        *,
        session_id: str,
        role: str,
        content: str,
        model: str | None = None,
        tokens_used: int | None = None,
        memories_injected: int | None = None,
    ) -> Any:
        return await _postgres_tx(tx).conn.fetchval(
            "INSERT INTO session_messages "
            "(session_id, role, content, model, tokens_used, memories_injected) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            session_id,
            role,
            content,
            model,
            tokens_used,
            memories_injected,
        )

    async def fetch_provider_history(self, tx: Transaction, session_id: str) -> list[Row]:
        return await _postgres_tx(tx).conn.fetch(
            """
            WITH first_system AS (
                SELECT id, role, content, timestamp FROM session_messages
                WHERE session_id=$1 AND role='system' AND deleted_at IS NULL
                ORDER BY timestamp ASC, id ASC LIMIT 1
            ), later_system AS (
                SELECT s.id, s.role, s.content, s.timestamp FROM session_messages s
                WHERE s.session_id=$1 AND s.role='system' AND s.deleted_at IS NULL
                AND s.id <> (SELECT id FROM first_system)
                ORDER BY s.timestamp DESC, s.id DESC LIMIT 4
            ), pinned AS (
                SELECT id, role, content, timestamp, 0 AS k FROM first_system
                UNION ALL SELECT id, role, content, timestamp, 0 AS k FROM later_system
            ), recent AS (
                SELECT id, role, content, timestamp, 1 AS k FROM session_messages
                WHERE session_id=$1 AND role <> 'system' AND deleted_at IS NULL
                ORDER BY timestamp DESC, id DESC LIMIT 10
            )
            SELECT role, content FROM (
                SELECT * FROM pinned UNION ALL SELECT * FROM recent
            ) all_msgs ORDER BY k, timestamp ASC, id ASC
            """,
            session_id,
        )

    async def add_memory_injections(
        self,
        tx: Transaction,
        *,
        session_id: str,
        message_id: Any,
        memory_ids: Sequence[str],
    ) -> None:
        conn = _postgres_tx(tx).conn
        for i, memory_id in enumerate(memory_ids):
            await conn.execute(
                "INSERT INTO session_memory_injections "
                "(session_id, message_id, memory_id, relevance_score) VALUES ($1, $2, $3, $4)",
                session_id,
                message_id,
                memory_id,
                0.9 - (i * 0.1),
            )

    async def update_metrics(
        self,
        tx: Transaction,
        *,
        session_id: str,
        user_id: str,
        namespace: str,
        tokens_used: int,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            "UPDATE sessions SET message_count=message_count+2, total_tokens=total_tokens+$2, "
            "last_activity=NOW() WHERE id=$1 AND user_id=$3 AND namespace=$4 AND deleted_at IS NULL",
            session_id,
            tokens_used,
            user_id,
            namespace,
        )

    async def fetch_history(self, tx: Transaction, session_id: str, limit: int, offset: int) -> tuple[list[Row], int]:
        conn = _postgres_tx(tx).conn
        rows = await conn.fetch(
            "SELECT role, content, timestamp, model FROM session_messages "
            "WHERE session_id=$1 AND deleted_at IS NULL ORDER BY timestamp ASC LIMIT $2 OFFSET $3",
            session_id,
            limit,
            offset,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM session_messages WHERE session_id=$1 AND deleted_at IS NULL",
            session_id,
        )
        return list(rows), int(total or 0)

    async def delete_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM sessions WHERE id=$1 AND user_id=$2 AND namespace=$3 AND deleted_at IS NULL",
            session_id,
            user_id,
            namespace,
        )
        return _pg_result_count(result) > 0


class PostgresConsultationsRepository(ConsultationsRepository):
    async def resolve_tier_lineup(self, tx: Transaction, tier: str) -> list[Row]:
        if tier == "frontier":
            sql = (
                "SELECT DISTINCT ON (provider) provider, model_id FROM model_registry "
                "WHERE available=true AND deprecated=false "
                "AND ((arena_rank IS NOT NULL AND arena_rank <= 5) OR graeae_weight >= 0.95) "
                "ORDER BY provider, graeae_weight DESC NULLS LAST, arena_rank ASC NULLS LAST"
            )
        elif tier == "premium":
            sql = (
                "SELECT DISTINCT ON (provider) provider, model_id FROM model_registry "
                "WHERE available=true AND deprecated=false "
                "AND ((arena_rank IS NOT NULL AND arena_rank BETWEEN 6 AND 15) "
                "OR (graeae_weight >= 0.85 AND graeae_weight < 0.95)) "
                "ORDER BY provider, graeae_weight DESC NULLS LAST, arena_rank ASC NULLS LAST"
            )
        else:
            sql = (
                "SELECT DISTINCT ON (provider) provider, model_id FROM model_registry "
                "WHERE available=true AND deprecated=false AND graeae_weight >= 0.75 "
                "AND input_cost_per_mtok IS NOT NULL AND output_cost_per_mtok IS NOT NULL "
                "ORDER BY provider, (input_cost_per_mtok + output_cost_per_mtok) ASC"
            )
        return await _postgres_tx(tx).conn.fetch(sql)

    async def resolve_models(self, tx: Transaction, model_ids: Sequence[str]) -> list[Row]:
        return await _postgres_tx(tx).conn.fetch(
            "SELECT provider, model_id FROM model_registry "
            "WHERE model_id = ANY($1::text[]) AND available=true AND deprecated=false",
            list(model_ids),
        )

    async def create_consultation_with_audit(self, tx: Transaction, **kwargs: Any) -> Any:
        conn = _postgres_tx(tx).conn
        row = await conn.fetchrow(
            "INSERT INTO graeae_consultations "
            "(prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, owner_id, namespace) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
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
        )
        consultation_id = row["id"]
        prompt_hash = hashlib.sha256(kwargs["prompt"].encode()).hexdigest()
        response_hash = hashlib.sha256(kwargs["consensus_response"].encode()).hexdigest()
        await conn.execute("SELECT pg_advisory_xact_lock(285734657)")
        prev = await conn.fetchrow("SELECT id, chain_hash FROM graeae_audit_log ORDER BY sequence_num DESC LIMIT 1")
        prev_chain = prev["chain_hash"] if prev else kwargs["genesis_hash"]
        prev_id = prev["id"] if prev else None
        chain_hash = hashlib.sha256((prev_chain + prompt_hash + response_hash).encode()).hexdigest()
        await conn.execute(
            "INSERT INTO graeae_audit_log "
            "(consultation_id, prompt, prompt_hash, provider, response_text, response_hash, "
            "chain_hash, prev_id, prev_chain_hash, task_type, quality_score) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            consultation_id,
            kwargs["prompt"],
            prompt_hash,
            kwargs["winning_muse"],
            kwargs["consensus_response"],
            response_hash,
            chain_hash,
            prev_id,
            prev_chain,
            kwargs["task_type"] or "reasoning",
            kwargs["consensus_score"],
        )
        for memory_id in kwargs["memory_ids"]:
            await conn.execute(
                "INSERT INTO consultation_memory_refs (consultation_id, memory_id, injected_at) "
                "VALUES ($1, $2, NOW()) ON CONFLICT DO NOTHING",
                consultation_id,
                memory_id,
            )
        return consultation_id

    async def list_audit_log(
        self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None, limit: int, offset: int
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if root and namespace is None:
            return await conn.fetch(
                "SELECT id, sequence_num, consultation_id, prompt_hash, response_hash, chain_hash, prev_id, "
                "task_type, provider, quality_score, created_at FROM graeae_audit_log WHERE deleted_at IS NULL "
                "ORDER BY sequence_num DESC LIMIT $1 OFFSET $2",
                limit,
                offset,
            )
        if root:
            return await conn.fetch(
                "SELECT al.id, al.sequence_num, al.consultation_id, al.prompt_hash, al.response_hash, al.chain_hash, "
                "al.prev_id, al.task_type, al.provider, al.quality_score, al.created_at "
                "FROM graeae_audit_log al JOIN graeae_consultations c ON c.id=al.consultation_id "
                "WHERE c.namespace=$1 AND c.deleted_at IS NULL AND al.deleted_at IS NULL "
                "ORDER BY al.sequence_num DESC LIMIT $2 OFFSET $3",
                namespace,
                limit,
                offset,
            )
        return await conn.fetch(
            "WITH visible AS (SELECT al.id, al.sequence_num AS global_sequence_num, al.consultation_id, "
            "al.prompt_hash, al.response_hash, al.task_type, al.provider, al.quality_score, al.created_at, "
            "ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
            "LAG(al.id) OVER (ORDER BY al.sequence_num ASC) AS scoped_prev_id "
            "FROM graeae_audit_log al JOIN graeae_consultations c ON c.id=al.consultation_id "
            "WHERE c.owner_id=$1 AND c.namespace=$2 AND c.deleted_at IS NULL AND al.deleted_at IS NULL) "
            "SELECT id, scoped_sequence_num AS sequence_num, consultation_id, prompt_hash, response_hash, "
            "NULL::text AS chain_hash, scoped_prev_id AS prev_id, task_type, provider, quality_score, created_at "
            "FROM visible ORDER BY global_sequence_num DESC LIMIT $3 OFFSET $4",
            user_id,
            namespace,
            limit,
            offset,
        )

    async def fetch_audit_chain(self, tx: Transaction, *, root: bool, user_id: str, namespace: str | None) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if root and namespace is None:
            return await conn.fetch(
                "SELECT sequence_num, prompt_hash, response_hash, chain_hash, prev_id "
                "FROM graeae_audit_log ORDER BY sequence_num ASC"
            )
        owner_clause = "" if root else "c.owner_id = $2 AND "
        params = (namespace,) if root else (namespace, user_id)
        return await conn.fetch(
            "SELECT al.sequence_num, ROW_NUMBER() OVER (ORDER BY al.sequence_num ASC) AS scoped_sequence_num, "
            "al.prompt_hash, al.response_hash, al.chain_hash, al.prev_id, al.prev_chain_hash, "
            "prev.chain_hash AS expected_prev_hash FROM graeae_audit_log al "
            "JOIN graeae_consultations c ON c.id=al.consultation_id "
            "LEFT JOIN LATERAL (SELECT chain_hash FROM graeae_audit_log WHERE sequence_num < al.sequence_num "
            "ORDER BY sequence_num DESC LIMIT 1) prev ON TRUE "
            f"WHERE {owner_clause}c.namespace=$1 AND c.deleted_at IS NULL AND al.deleted_at IS NULL "
            "ORDER BY al.sequence_num ASC",
            *params,
        )

    async def get_consultation(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> Row | None:
        conn = _postgres_tx(tx).conn
        if root and namespace is None:
            return await conn.fetchrow(
                "SELECT id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id=$1 AND deleted_at IS NULL",
                consultation_id,
            )
        if root:
            return await conn.fetchrow(
                "SELECT id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, created "
                "FROM graeae_consultations WHERE id=$1 AND namespace=$2 AND deleted_at IS NULL",
                consultation_id,
                namespace,
            )
        return await conn.fetchrow(
            "SELECT id, prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode, created "
            "FROM graeae_consultations WHERE id=$1 AND owner_id=$2 AND namespace=$3 AND deleted_at IS NULL",
            consultation_id,
            user_id,
            namespace,
        )

    async def get_consultation_artifacts(
        self, tx: Transaction, *, consultation_id: str, root: bool, user_id: str, namespace: str | None
    ) -> tuple[Row | None, list[Row]]:
        conn = _postgres_tx(tx).conn
        consultation = await self.get_consultation(
            tx, consultation_id=consultation_id, root=root, user_id=user_id, namespace=namespace
        )
        if not consultation:
            return None, []
        refs = await conn.fetch(
            "SELECT memory_id, injected_at FROM consultation_memory_refs WHERE consultation_id=$1 ORDER BY injected_at",
            consultation_id,
        )
        return consultation, list(refs)


class PostgresFederationRepository(FederationRepository):
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

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: Any | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        conn = _postgres_tx(tx).conn
        if updated_after is not None and id_after is not None:
            return await conn.fetch(
                """
                SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated
                FROM memories
                WHERE deleted_at IS NULL
                  AND (updated > $1 OR (updated = $1 AND id > $2))
                ORDER BY updated ASC, id ASC
                LIMIT $3
                """,
                updated_after,
                id_after,
                limit,
            )
        return await conn.fetch(
            """
            SELECT id, content, category, subcategory, metadata, owner_id, namespace, updated
            FROM memories
            WHERE deleted_at IS NULL
            ORDER BY updated ASC, id ASC
            LIMIT $1
            """,
            limit,
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
        return await _postgres_tx(tx).conn.fetchrow(
            """
            INSERT INTO federation_peers
              (name, base_url, auth_token, namespace_filter, category_filter,
               enabled, sync_interval_secs, compat_mode)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            name,
            base_url,
            auth_token,
            list(namespace_filter) if namespace_filter is not None else None,
            list(category_filter) if category_filter is not None else None,
            enabled,
            sync_interval_secs,
            compat_mode,
        )

    async def list_peers(self, tx: Transaction) -> list[Row]:
        return list(await _postgres_tx(tx).conn.fetch("SELECT * FROM federation_peers ORDER BY name"))

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT * FROM federation_peers WHERE id = $1::uuid",
            peer_id,
        )

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        set_clauses = [f"{col}=${i + 2}" for i, col in enumerate(updates.keys())]
        set_clauses.append("updated=NOW()")
        return await _postgres_tx(tx).conn.fetchrow(
            f"UPDATE federation_peers SET {', '.join(set_clauses)} WHERE id=$1::uuid RETURNING *",
            peer_id,
            *updates.values(),
        )

    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO federation_peers (id, base_url, name, auth_token, enabled)
            VALUES ($1::uuid, $2, $3, '', $4)
            ON CONFLICT (id) DO UPDATE
            SET base_url = EXCLUDED.base_url,
                name = EXCLUDED.name,
                enabled = EXCLUDED.enabled
            """,
            peer_id,
            base_url,
            name,
            enabled,
        )

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM federation_peers WHERE id = $1::uuid",
            peer_id,
        )
        return _pg_result_count(result) > 0

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        return list(
            await _postgres_tx(tx).conn.fetch(
                """
            SELECT id::text, started_at, finished_at, memories_pulled,
                   memories_new, memories_updated, error,
                   cursor_before, cursor_after
            FROM federation_sync_log
            WHERE peer_id = $1::uuid
            ORDER BY started_at DESC
            LIMIT $2
            """,
                peer_id,
                limit,
            )
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
        memory_query_parts = [_eligibility.eligible_for_federation("m")]
        tombstone_query_parts = [
            "m.federation_source IS NULL",
            "m.deleted_at IS NULL",
            "m.consolidated_into IS NOT NULL",
            "m.consolidated_at IS NOT NULL",
        ]
        args: list[Any] = []
        if since_updated is not None:
            args.append(since_updated)
            since_updated_arg = len(args)
            args.append(since_id)
            since_id_arg = len(args)
            memory_query_parts.append(
                f"(m.updated > ${since_updated_arg} OR (m.updated = ${since_updated_arg} AND m.id > ${since_id_arg}))"
            )
            tombstone_query_parts.append(
                f"(m.consolidated_at > ${since_updated_arg} "
                f"OR (m.consolidated_at = ${since_updated_arg} AND m.id > ${since_id_arg}))"
            )
        if namespaces:
            args.append(list(namespaces))
            memory_query_parts.append(f"m.namespace = ANY(${len(args)})")
            tombstone_query_parts.append(f"m.namespace = ANY(${len(args)})")
        if categories:
            args.append(list(categories))
            memory_query_parts.append(f"m.category = ANY(${len(args)})")
            tombstone_query_parts.append(f"m.category = ANY(${len(args)})")
        args.append(limit)

        if prefer_compressed:
            use_variant = (
                "m.archived_at IS NULL "
                "AND v.compressed_content IS NOT NULL "
                "AND (2 * octet_length(to_json(v.compressed_content)::text)) "
                "  < (octet_length(to_json(m.content)::text) "
                "     + COALESCE(octet_length(to_json(m.verbatim_content)::text), 0))"
            )
            content_select = f"CASE WHEN {use_variant} THEN v.compressed_content ELSE m.content END AS content,"
            compressed_select = (
                f"CASE WHEN {use_variant} THEN v.compressed_content ELSE NULL::text END AS compressed_content,"
            )
            verbatim_select = f"CASE WHEN {use_variant} THEN NULL ELSE m.verbatim_content END AS verbatim_content,"
            join_compressed = "LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id "
        else:
            content_select = "m.content,"
            compressed_select = "NULL::text AS compressed_content,"
            verbatim_select = "m.verbatim_content,"
            join_compressed = ""

        # v6.1 F-1.2: optional embedding column for the copy_embeddings flow.
        # See docs/v6.1-federation-embeddings-copy.md. Default off preserves
        # v6.0 wire format. embedding_model is a literal so the caller doesn't
        # need a separate roundtrip; the receiver enforces match against its
        # local embedder before accepting the bytes.
        if include_embedding:
            from mnemos.core.config import get_settings as _gs

            try:
                _http_model = embed_http_model_override()
                _embed_model = _http_model or (_gs().providers.inference_embed_model or "").strip() or "unknown"
            except Exception:
                _embed_model = "unknown"
            # Trailing-comma terminators: rely on the always-present _trailer
            # column at the end of the SELECT lists so we can drop in
            # additional columns without re-counting commas every change.
            embedding_select_memory = f"m.embedding AS embedding, '{_embed_model}' AS embedding_model,"
            embedding_select_tombstone = "NULL::vector AS embedding, NULL::text AS embedding_model,"
        else:
            embedding_select_memory = ""
            embedding_select_tombstone = ""

        memory_where_clause = " AND ".join(memory_query_parts)
        tombstone_where_clause = " AND ".join(tombstone_query_parts)
        return list(
            await _postgres_tx(tx).conn.fetch(
                f"""
            SELECT *
            FROM (
                SELECT NULL::text AS type,
                       m.id, {content_select}
                       m.category, m.subcategory, m.metadata,
                       m.quality_rating, {verbatim_select}
                       m.owner_id, m.namespace,
                       m.permission_mode, m.source_model, m.source_provider,
                       m.source_session, m.source_agent, m.created, m.updated,
                       m.archived_at,
                       NULL::text AS consolidated_into,
                       NULL::timestamptz AS consolidated_at,
                       {compressed_select}
                       {embedding_select_memory}
                       NULL::text AS _trailer
                FROM memories m
                {join_compressed}
                WHERE {memory_where_clause}

                UNION ALL

                SELECT 'consolidation'::text AS type,
                       m.id,
                       NULL::text AS content,
                       NULL::text AS category,
                       NULL::text AS subcategory,
                       NULL::jsonb AS metadata,
                       NULL::int AS quality_rating,
                       NULL::text AS verbatim_content,
                       NULL::text AS owner_id,
                       m.namespace,
                       NULL::smallint AS permission_mode,
                       NULL::text AS source_model,
                       NULL::text AS source_provider,
                       NULL::text AS source_session,
                       NULL::text AS source_agent,
                       m.created,
                       m.consolidated_at AS updated,
                       NULL::timestamptz AS archived_at,
                       m.consolidated_into,
                       m.consolidated_at,
                       NULL::text AS compressed_content,
                       {embedding_select_tombstone}
                       NULL::text AS _trailer
                FROM memories m
                WHERE {tombstone_where_clause}
            ) feed
            ORDER BY updated ASC, id ASC
            LIMIT ${len(args)}
            """,
                *args,
            )
        )

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        query_parts = [_eligibility.eligible_for_federation("m"), "m.id = $1"]
        args: list[Any] = [memory_id]
        if namespaces:
            args.append(list(namespaces))
            query_parts.append(f"m.namespace = ANY(${len(args)})")
        if categories:
            args.append(list(categories))
            query_parts.append(f"m.category = ANY(${len(args)})")
        where_clause = " AND ".join(query_parts)
        return await _postgres_tx(tx).conn.fetchrow(
            f"""
            SELECT id, content, category, subcategory, metadata, quality_rating,
                   verbatim_content, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   created, updated, archived_at
            FROM memories m
            WHERE {where_clause}
            """,
            *args,
        )

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        # v6.1 F-1: copy_embeddings (migration 0028) — COALESCE so rows
        # from pre-migration DB return 0 instead of NULL.
        return await _postgres_tx(tx).conn.fetchrow(
            """
            SELECT id::text, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor,
                   compat_mode,
                   COALESCE(copy_embeddings, 0) AS copy_embeddings
            FROM federation_peers WHERE id = $1::uuid
            """,
            peer_id,
        )

    async def update_peer_schema_check(self, tx: Transaction, peer_id: str, peer_version: str | None) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET peer_mnemos_version = $2, last_schema_check_at = NOW()
            WHERE id = $1::uuid
            """,
            peer_id,
            peer_version,
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
        conn = _postgres_tx(tx).conn
        await self.update_peer_schema_check(tx, peer_id, peer_version)
        log_id = await conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id,
            cursor_before,
        )
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
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW()
                                  - (sync_interval_secs || ' seconds')::interval
                                  + INTERVAL '60 seconds',
                    last_error = $2,
                    last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id,
                error,
            )
        else:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(),
                    last_error = $2,
                    last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id,
                error,
            )

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        return await _postgres_tx(tx).conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id,
            cursor_before,
        )

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
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_sync_log
            SET finished_at = NOW(),
                memories_pulled = $2,
                memories_new = $3,
                memories_updated = $4,
                error = $5,
                cursor_after = $6
            WHERE id = $1::uuid
            """,
            log_id,
            memories_pulled,
            memories_new,
            memories_updated,
            error,
            cursor_after,
        )

    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET last_sync_at = NOW(), last_error = $2, last_error_at = NOW()
            WHERE id = $1::uuid
            """,
            peer_id,
            error,
        )

    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            UPDATE federation_peers
            SET last_sync_at = NOW(),
                last_sync_cursor = $2,
                last_error = NULL,
                last_error_at = NULL,
                total_pulled = total_pulled + $3
            WHERE id = $1::uuid
            """,
            peer_id,
            cursor,
            total_pulled,
        )

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        return list(
            await _postgres_tx(tx).conn.fetch(
                """
            SELECT id::text, name, sync_interval_secs, last_sync_at
            FROM federation_peers
            WHERE enabled
              AND (last_sync_at IS NULL
                   OR last_sync_at + (sync_interval_secs || ' seconds')::interval <= NOW())
            ORDER BY COALESCE(
                last_sync_at + (sync_interval_secs || ' seconds')::interval,
                'epoch'::timestamptz
            )
            LIMIT $1
            """,
                limit,
            )
        )

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT federation_remote_updated FROM memories WHERE id = $1 AND deleted_at IS NULL",
            local_id,
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
            await _postgres_tx(tx).conn.execute(
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'federation', $8, 644,
                        $9, $10, $11, $12, $13, $14::timestamptz, NOW(),
                        $14::timestamptz)
                """,
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
            )
            return True
        except asyncpg.UniqueViolationError:
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
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories SET
              content = $2, category = $3, subcategory = $4,
              metadata = $5::jsonb, verbatim_content = $6,
              quality_rating = $7, namespace = $8,
              federation_remote_updated = $9::timestamptz,
              updated = $9::timestamptz
            WHERE id = $1
              AND deleted_at IS NULL
              AND (
                  federation_remote_updated IS NULL
                  OR federation_remote_updated < $9::timestamptz
              )
            """,
            local_id,
            content,
            category,
            subcategory,
            metadata_json,
            verbatim_content,
            quality_rating,
            namespace,
            remote_updated,
        )
        return _pg_result_count(result) > 0

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
        result = await _postgres_tx(tx).conn.execute(
            """
            UPDATE memories
            SET consolidated_into = $2,
                consolidated_at = COALESCE($3::timestamptz, NOW()),
                permission_mode = 400,
                metadata = COALESCE(metadata, '{}'::jsonb)
                    || jsonb_build_object(
                        'federation_consolidation', jsonb_build_object(
                            'remote_id', $4,
                            'remote_consolidated_into', $5,
                            'peer', $6
                        )
                    )
            WHERE id = $1
              AND deleted_at IS NULL
              AND consolidated_into IS DISTINCT FROM $2
              AND EXISTS (
                  SELECT 1 FROM memories
                  WHERE id = $2 AND deleted_at IS NULL
              )
            """,
            local_id,
            local_canonical_id,
            consolidated_at,
            remote_id,
            canonical_remote_id,
            peer_name,
        )
        return _pg_result_count(result) > 0

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        local_id = f"fed:{peer_name}:{memory_id}"
        result = await _postgres_tx(tx).conn.execute(
            """
            DELETE FROM memories
            WHERE id = $1
              AND federation_source = $2
              AND deleted_at IS NULL
            """,
            local_id,
            peer_name,
        )
        return _pg_result_count(result)


class PostgresStateRepository(StateRepository):
    """state.value is now TEXT on PG (migrations_v4_2_state_value_text.sql).

    Pass-through with no JSON shape coupling — the column matches
    SqliteStateRepository's TEXT contract exactly. Callers who want
    JSON shape (e.g. the HTTP /v1/state route) wrap their payloads
    in json.dumps at the API edge.
    """

    async def get(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> Row | None:
        return await _postgres_tx(tx).conn.fetchrow(
            "SELECT key, value, updated::text AS updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = $1 AND namespace = $2 AND key = $3 "
            "AND deleted_at IS NULL",
            owner_id,
            namespace,
            key,
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
        return await _postgres_tx(tx).conn.fetchrow(
            """
            INSERT INTO state (owner_id, namespace, key, value, updated)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (owner_id, namespace, key) DO UPDATE
            SET value = $4,
                updated = NOW(),
                version = state.version + 1
            WHERE state.deleted_at IS NULL
            RETURNING key, value, updated::text AS updated, version, owner_id, namespace
            """,
            owner_id,
            namespace,
            key,
            value,
        )

    async def delete(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND key = $3 AND deleted_at IS NULL",
            owner_id,
            namespace,
            key,
        )
        return _pg_result_count(result) > 0

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        args: list[Any] = [owner_id, namespace]
        sql = (
            "SELECT key, updated::text AS updated, version, owner_id, namespace FROM state "
            "WHERE owner_id = $1 AND namespace = $2 "
            "AND deleted_at IS NULL ORDER BY key"
        )
        if limit is not None:
            args.extend([limit, offset])
            sql += " LIMIT $3 OFFSET $4"
        return list(await _postgres_tx(tx).conn.fetch(sql, *args))

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        result = await _postgres_tx(tx).conn.execute(
            "DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND deleted_at IS NULL",
            owner_id,
            namespace,
        )
        return _pg_result_count(result)


def _iso_or_none(value: Any) -> str | None:
    """Coerce a datetime / None to ISO 8601 string or None."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class PostgresAuditChainRepository(AuditChainRepository):
    """Postgres impl of the v6.2 M-2.2.1 audit chain repository.

    Tables: ``memory_audit_chain`` + ``memory_audit_roots``
    (migrations 0029 + 0030; shipped at 614d483).

    Atomicity: all operations run inside the caller's tx so audit
    inserts and the memory UPSERT commit together. The sealer's
    claim/stamp pair uses ``FOR UPDATE SKIP LOCKED`` so concurrent
    sealer instances coexist (single-row lock per entry).
    """

    async def get_latest_audit_entry(
        self,
        tx: Transaction,
        memory_id: bytes,
    ) -> Row | None:
        row = await _postgres_tx(tx).conn.fetchrow(
            """
            SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM memory_audit_chain
            WHERE memory_id = $1
            ORDER BY signed_at DESC
            LIMIT 1
            """,
            memory_id,
        )
        return dict(row) if row is not None else None

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
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO memory_audit_chain (
                entry_id, memory_id, prev_entry_id, prev_entry_hash,
                op, payload_hash, writer_id, writer_pubkey,
                signature, signed_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
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
        )

    async def claim_unsealed_window(
        self,
        tx: Transaction,
        *,
        max_window_seconds: int,
        limit: int,
    ) -> list[Row]:
        """Claim oldest unsealed entries whose signed_at is older than
        ``now - max_window_seconds``. SKIP LOCKED lets concurrent
        sealer instances coexist (Postgres 9.5+; available since
        forever in our supported range).
        """
        rows = await _postgres_tx(tx).conn.fetch(
            """
            SELECT entry_id, signature, signed_at
            FROM memory_audit_chain
            WHERE global_root IS NULL
              AND signed_at <= NOW() - ($1 || ' seconds')::interval
            ORDER BY signed_at ASC, entry_id ASC
            LIMIT $2
            FOR UPDATE SKIP LOCKED
            """,
            str(max_window_seconds),
            limit,
        )
        return [dict(r) for r in rows]

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
        # Use unnest + WITH ORDINALITY to preserve per-id seq order.
        await _postgres_tx(tx).conn.execute(
            """
            WITH ordered AS (
                SELECT unnest($1::bytea[]) AS entry_id,
                       generate_series($3::bigint,
                                       $3::bigint + array_length($1, 1) - 1) AS seq
            )
            UPDATE memory_audit_chain m
            SET global_root = $2,
                global_seq = o.seq
            FROM ordered o
            WHERE m.entry_id = o.entry_id
            """,
            entry_ids,
            global_root,
            starting_seq,
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
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO memory_audit_roots (
                global_root, window_start, window_end, entry_count,
                root_signature, signer_pubkey, sealed_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            global_root,
            window_start,
            window_end,
            entry_count,
            root_signature,
            signer_pubkey,
            sealed_at,
        )

    async def list_window_entries(
        self,
        tx: Transaction,
        global_root: bytes,
    ) -> list[Row]:
        rows = await _postgres_tx(tx).conn.fetch(
            """
            SELECT entry_id, memory_id, signature, signed_at,
                   global_seq, payload_hash, op
            FROM memory_audit_chain
            WHERE global_root = $1
            ORDER BY signed_at ASC, entry_id ASC
            """,
            global_root,
        )
        return [dict(r) for r in rows]

    async def get_audit_entry_by_id(
        self,
        tx: Transaction,
        entry_id: bytes,
    ) -> Row | None:
        row = await _postgres_tx(tx).conn.fetchrow(
            """
            SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM memory_audit_chain
            WHERE entry_id = $1
            """,
            entry_id,
        )
        return dict(row) if row is not None else None

    async def get_chain_stats(self, tx: Transaction) -> dict:
        conn = _postgres_tx(tx).conn
        chain_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::bigint AS total_entries,
                COUNT(*) FILTER (WHERE global_root IS NULL)::bigint AS unsealed_count,
                MIN(signed_at) FILTER (WHERE global_root IS NULL) AS oldest_unsealed_signed_at
            FROM memory_audit_chain
            """
        )
        root_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::bigint AS sealed_root_count,
                MAX(sealed_at) AS last_sealed_at
            FROM memory_audit_roots
            """
        )
        return {
            "total_entries": int(chain_stats["total_entries"] or 0),
            "unsealed_count": int(chain_stats["unsealed_count"] or 0),
            "oldest_unsealed_signed_at": _iso_or_none(chain_stats["oldest_unsealed_signed_at"]),
            "sealed_root_count": int(root_stats["sealed_root_count"] or 0),
            "last_sealed_at": _iso_or_none(root_stats["last_sealed_at"]),
        }

    async def get_latest_audit_entries_batch(
        self,
        tx: Transaction,
        memory_ids: list[bytes],
    ) -> dict[bytes, Row]:
        """Batch via DISTINCT ON (memory_id) ... ORDER BY memory_id,
        signed_at DESC. Postgres-specific (DISTINCT ON not portable);
        backends without it fall back to the base impl.
        """
        if not memory_ids:
            return {}
        rows = await _postgres_tx(tx).conn.fetch(
            """
            SELECT DISTINCT ON (memory_id)
                   entry_id, memory_id, prev_entry_id, prev_entry_hash,
                   op, payload_hash, writer_id, writer_pubkey,
                   signature, signed_at, global_root, global_seq
            FROM memory_audit_chain
            WHERE memory_id = ANY($1::bytea[])
            ORDER BY memory_id, signed_at DESC
            """,
            memory_ids,
        )
        return {r["memory_id"]: dict(r) for r in rows}


class PostgresBackend:
    """Postgres persistence facade backed by an asyncpg pool."""

    _supports_core_persistence = True
    _supports_oauth_persistence = True
    _supports_sessions_persistence = True
    _supports_consultations_persistence = True
    _supports_federation_persistence = True
    _supports_audit_persistence = True
    _supports_state_persistence = True

    supports_listen_notify = True
    supports_advisory_locks = True
    supports_row_level_security = True
    supports_pgvector = True

    def __init__(self, pool: asyncpg.Pool, settings: Any):
        self._pool = pool
        self._settings = settings
        self._memories = PostgresMemoryRepository()
        # Wire the configured embedding dim into the memory repo so
        # semantic_search fails loudly on dim mismatches with the
        # operator-friendly error rather than the generic asyncpg
        # cast error. Settings shape mirrors what SqliteBackend uses.
        try:
            self._memories._expected_embedding_dim = int(getattr(settings.database, "embedding_dim", 768))
        except (AttributeError, TypeError, ValueError):
            # Defensive: tests or stripped-down settings shapes may
            # not carry `database.embedding_dim`. Leave the slot as
            # None so the guard is a no-op rather than a hard failure
            # at construction.
            self._memories._expected_embedding_dim = None
        self._kg_triples = PostgresKGRepository()
        self._memory_versions = PostgresVersionRepository()
        self._memory_branches = PostgresBranchRepository()
        self._compression = PostgresCompressionRepository()
        self._compression_queue = PostgresCompressionQueueRepository()
        self._webhooks = PostgresWebhookRepository()
        self._consultations_audit = PostgresConsultationAuditRepository()
        self._oauth = PostgresOAuthRepository()
        self._sessions = PostgresSessionsRepository()
        self._consultations = PostgresConsultationsRepository()
        self._federation = PostgresFederationRepository()
        self._state_kv = PostgresStateRepository()
        self._audit_chain = PostgresAuditChainRepository()
        self._acl = PostgresAclRepository()
        self._closed = False

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def capabilities(self) -> set[str]:
        return {"core", "oauth", "sessions", "consultations", "federation", "audit", "state", "acl"}

    @property
    def capability_details(self) -> set[str]:
        return set(POSTGRES_CAPABILITY_DETAILS)

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        async with self._pool.acquire() as conn:
            raw_tx = conn.transaction()
            await raw_tx.start()
            tx = PostgresTransaction(conn, raw_tx)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    async def record_usage_ledger(
        self,
        tx: Transaction,
        record: UsageLedgerRecord,
    ) -> UsageLedgerResult:
        row = await _postgres_tx(tx).conn.fetchrow(
            """
            WITH resolved_prices AS (
                SELECT input_cost_per_mtok, output_cost_per_mtok, raw
                FROM model_registry
                WHERE provider=$1 AND model_id=$2
            ),
            resolved_plan AS (
                SELECT auth_method
                FROM subscription_plans
                WHERE provider=$1 AND plan_name=$10
                  AND effective_from <= CURRENT_DATE
                  AND (effective_until IS NULL OR effective_until >= CURRENT_DATE)
            ),
            inserted AS (
                INSERT INTO usage_ledger (
                    provider, model, task_kind, tokens_in, tokens_out,
                    tokens_reasoning, est_cost_usd, latency_ms, outcome,
                    caller_subsystem, tier, session_id, request_count,
                    plan_window_id, path_kind, subscription_amortized
                )
                SELECT
                    $1, $2, $3, $4, $5, $6,
                    CASE WHEN COALESCE(pl.auth_method, 'api') = 'subscription' THEN 0
                         ELSE (($4::NUMERIC * COALESCE(rp.input_cost_per_mtok, 0)::NUMERIC)
                              + ($5::NUMERIC * COALESCE(rp.output_cost_per_mtok, 0)::NUMERIC)
                              + ($6::NUMERIC * COALESCE(
                                  NULLIF(rp.raw->>'reasoning_cost_per_mtok', '')::NUMERIC,
                                  rp.output_cost_per_mtok,
                                  0
                                )::NUMERIC)) / 1000000
                    END,
                    $7, $8, $9, $10, $11, $12, $13, $14,
                    COALESCE(pl.auth_method, 'api') = 'subscription'
                FROM (SELECT 1) seed
                LEFT JOIN resolved_prices rp ON TRUE
                LEFT JOIN resolved_plan pl ON TRUE
                RETURNING id, est_cost_usd
            )
            SELECT id, est_cost_usd,
                   EXISTS(SELECT 1 FROM resolved_prices) AS registry_match,
                   COALESCE((SELECT auth_method FROM resolved_plan), 'api') AS auth_method
            FROM inserted
            """,
            record.provider,
            record.model,
            record.task_kind,
            record.tokens_in,
            record.tokens_out,
            record.tokens_reasoning,
            record.latency_ms,
            record.outcome,
            record.caller_subsystem,
            record.tier,
            record.session_id,
            record.request_count,
            record.plan_window_id,
            record.path_kind or "api",
        )
        if row is None:
            raise RuntimeError("usage_ledger insert returned no row")
        if row["auth_method"] != "subscription" and not row["registry_match"]:
            logger.warning(
                "usage_ledger model_registry price missing for provider=%s model=%s; recording est_cost_usd=0",
                record.provider,
                record.model,
            )
        return UsageLedgerResult(id=int(row["id"]), est_cost_usd=row["est_cost_usd"])

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        rows = await _postgres_tx(tx).conn.fetch(
            "SELECT category, half_life_days, decay_kind, floor FROM memory_category_decay"
        )
        return [dict(row) for row in rows]

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        await _postgres_tx(tx).conn.execute(
            """
            INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (category) DO UPDATE SET
                half_life_days = EXCLUDED.half_life_days,
                decay_kind = EXCLUDED.decay_kind,
                floor = EXCLUDED.floor
            """,
            category,
            half_life_days,
            decay_kind,
            floor,
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
        conn = _postgres_tx(tx).conn
        metadata_json = json.dumps(metadata or {})
        if entry_date is None:
            row = await conn.fetchrow(
                """
                INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                VALUES ($1, $2, $3, CURRENT_DATE, $4, $5, $6::jsonb)
                RETURNING id, entry_date::text, topic, content, metadata, created::text
                """,
                entry_id,
                owner_id,
                namespace,
                topic,
                content,
                metadata_json,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                RETURNING id, entry_date::text, topic, content, metadata, created::text
                """,
                entry_id,
                owner_id,
                namespace,
                entry_date,
                topic,
                content,
                metadata_json,
            )
        if row is None:
            raise RuntimeError("journal insert returned no row")
        return dict(row)

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
        conn = _postgres_tx(tx).conn
        if entry_date is not None:
            rows = await conn.fetch(
                """
                SELECT id, entry_date::text, topic, content, metadata, created::text
                FROM journal WHERE owner_id = $1 AND namespace = $2 AND entry_date = $3
                  AND deleted_at IS NULL
                ORDER BY created DESC LIMIT $4
                """,
                owner_id,
                namespace,
                entry_date,
                limit,
            )
        elif topic:
            rows = await conn.fetch(
                """
                SELECT id, entry_date::text, topic, content, metadata, created::text
                FROM journal WHERE owner_id = $1 AND namespace = $2 AND topic = $3
                  AND deleted_at IS NULL
                ORDER BY created DESC LIMIT $4
                """,
                owner_id,
                namespace,
                topic,
                limit,
            )
        elif search:
            rows = await conn.fetch(
                """
                SELECT id, entry_date::text, topic, content, metadata, created::text
                FROM journal WHERE owner_id = $1 AND namespace = $2 AND (content ILIKE $3 OR topic ILIKE $3)
                  AND deleted_at IS NULL
                ORDER BY created DESC LIMIT $4
                """,
                owner_id,
                namespace,
                f"%{search}%",
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, entry_date::text, topic, content, metadata, created::text
                FROM journal WHERE owner_id = $1 AND namespace = $2
                  AND deleted_at IS NULL
                ORDER BY created DESC LIMIT $3
                """,
                owner_id,
                namespace,
                limit,
            )
        return [dict(row) for row in rows]

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        result = await _postgres_tx(tx).conn.execute(
            """
            DELETE FROM journal
            WHERE id = $1 AND owner_id = $2 AND namespace = $3
              AND deleted_at IS NULL
            """,
            entry_id,
            owner_id,
            namespace,
        )
        return result != "DELETE 0"

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
    def acl(self) -> AclRepository:
        return self._acl

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
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._pool, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._closed = True
