"""Focused fake persistence backend for document-import route tests.

The document import surface is backend-neutral.  These tests therefore use a
recording implementation of the repository methods exercised by the route,
rather than emulating a driver connection or issuing real database writes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any


class _FakeMemoryRepository:
    """Record document memory inserts and return their canonical IDs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def insert_memory(self, tx: Any, **kwargs: Any) -> str:
        self.calls.append(("insert_memory", kwargs))
        return str(kwargs["memory_id"])


class _FakeWebhookRepository:
    """Record transactional outbox events emitted by document imports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch_event(
        self,
        tx: Any,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        self.calls.append(
            (
                "dispatch_event",
                {
                    "event_type": event_type,
                    "payload": payload,
                    "owner_id": owner_id,
                    "namespace": namespace,
                },
            )
        )
        return []


class FakeBackend:
    """Persistence-backend implementation required by document import tests."""

    supports_webhooks = True

    def __init__(self) -> None:
        self.memories = _FakeMemoryRepository()
        self.webhooks = _FakeWebhookRepository()
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transactional(self):
        tx = SimpleNamespace(_fake=True, conn=SimpleNamespace())
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def close(self) -> None:
        return None


def install_fake_backend(monkeypatch: Any) -> FakeBackend:
    """Install an isolated recording backend in the lifecycle singleton."""
    import mnemos.core.lifecycle as lifecycle

    backend = FakeBackend()
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_rls_enabled", False)
    monkeypatch.setattr(lifecycle, "_cache", None)
    return backend


# ---------------------------------------------------------------------------
# Conn-backed fake backend for the portability suite
# ---------------------------------------------------------------------------
#
# The portability tests predate the backend-neutral rewiring: they build a
# mock asyncpg connection whose ``fetch(sql, *args)`` routes to a row set by
# SQL SUBSTRING (``routed_rows={"FROM kg_triples": [...]}``).
#
# Rather than rewrite ~140 call sites, this adapter presents the persistence
# ABC surface on top of that same mock: each repository method issues the SQL
# the legacy Postgres repo issued and delegates to the mock. Substring routing
# therefore keeps working unchanged, and the mock still records every call in
# ``fetch_calls`` / ``executes`` for the handful of tests that assert on it.
#
# This is a TEST DOUBLE, not a backend. It deliberately reproduces the old
# Postgres SQL so those tests keep exercising charon's ORCHESTRATION (tenant
# scope, redaction, cursor handling, error paths) which is what they are for.
# Whether the real ABC methods produce correct SQL per dialect is verified
# elsewhere, by the live round-trip tests and by core's own parity suite.


class _FakeTx:
    """Transaction handle exposing the savepoint contract charon now uses."""

    def __init__(self, conn):
        self.conn = conn
        self.savepoints = 0

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def savepoint(self):
        self.savepoints += 1
        outer = self

        class _Ctx:
            async def __aenter__(self_):
                return outer

            async def __aexit__(self_, *exc):
                # Propagate exactly like a real savepoint: the exception keeps
                # travelling, the enclosing transaction stays usable.
                return False

        return _Ctx()


# The legacy Postgres repo passed these to conn.execute() in a fixed
# positional order, and several tests assert on args BY INDEX. Emitting the
# kwargs in the same order keeps those index-based assertions meaningful
# instead of silently shifting what index N refers to.
_INSERT_MEMORY_ORDER = (
    "memory_id",
    "content",
    "category",
    "subcategory",
    "metadata_json",
    "quality_rating",
    "verbatim_content",
    "owner_id",
    "namespace",
    "permission_mode",
    "source_model",
    "source_provider",
    "source_session",
    "source_agent",
    "created",
    "updated",
)
_INSERT_KG_ORDER = (
    "triple_id",
    "subject",
    "predicate",
    "obj",
    "subject_type",
    "object_type",
    "valid_from",
    "valid_until",
    "memory_id",
    "confidence",
    "created",
    "owner_id",
    "namespace",
)
_INSERT_VERSION_ORDER = (
    "version_id",
    "memory_id",
    "version_num",
    "content",
    "category",
    "subcategory",
    "metadata_json",
    "verbatim_content",
    "owner_id",
    "namespace",
    "permission_mode",
    "source_model",
    "source_provider",
    "source_session",
    "source_agent",
    "snapshot_at",
    "snapshot_by",
    "change_type",
    "commit_hash",
    "parent_version_id",
    "branch",
    "merge_parents",
)
_INSERT_VARIANT_ORDER = (
    "memory_id",
    "owner_id",
    "winner_candidate_id",
    "engine_id",
    "engine_version",
    "compressed_content",
    "compressed_tokens",
    "compression_ratio",
    "quality_score",
    "composite_score",
    "scoring_profile",
    "judge_model",
    "selected_at",
)


def _ordered(kw, order):
    """Kwargs as legacy positional args; unknown keys appended, stably."""
    args = [kw[name] for name in order if name in kw]
    args.extend(kw[k] for k in kw if k not in order)
    return args


class _ConnMemories:
    def __init__(self, conn):
        self._c = conn

    async def fetch_memory_export(
        self,
        tx,
        *,
        effective_owner,
        effective_ns,
        category,
        limit,
        offset,
        include_secrets=False,
        record_cursor=None,
    ):
        conditions = ["deleted_at IS NULL"]
        params = []
        idx = 1
        if not include_secrets:
            conditions.append(f"(namespace IS NULL OR namespace <> ${idx})")
            params.append("vault")
            idx += 1
        for col, val in (("owner_id", effective_owner), ("namespace", effective_ns), ("category", category)):
            if val:
                conditions.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1
        if record_cursor is not None:
            conditions.append(f"(created, id) > (${idx}, ${idx + 1})")
            params.extend(record_cursor)
            idx += 2
        sql = (
            "SELECT id, content, category, subcategory, created, updated, "
            "owner_id, namespace, permission_mode, quality_rating, "
            "source_model, source_provider, source_session, source_agent, "
            "metadata, verbatim_content, "
            "provenance AS prov_kind, morpheus_run_id::text AS morpheus_run_id, "
            "source_memories, federation_source "
            "FROM memories WHERE " + " AND ".join(conditions) + " "
            f"ORDER BY created ASC, id ASC LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])
        return await self._c.fetch(sql, *params)

    async def fetch_visible_export_memory_ids(
        self, tx, *, memory_ids, effective_owner, effective_ns, include_secrets=False
    ):
        if not memory_ids:
            return set()
        conditions = ["id = ANY($1::text[])", "deleted_at IS NULL"]
        params = [list(memory_ids)]
        for column, value in (("owner_id", effective_owner), ("namespace", effective_ns)):
            if value:
                params.append(value)
                conditions.append(f"{column} = ${len(params)}")
        if not include_secrets:
            params.append("vault")
            conditions.append(f"(namespace IS NULL OR namespace <> ${len(params)})")
        rows = await self._c.fetch("SELECT id FROM memories WHERE " + " AND ".join(conditions), *params)
        return {r["id"] for r in rows}

    async def fetch_deletion_log_for_export(
        self,
        tx,
        *,
        effective_owner,
        effective_ns,
        hard_limit,
        from_executed_at=None,
        to_executed_at=None,
        cursor_executed_at=None,
        cursor_id=None,
        export_as_of=None,
        include_secrets=False,
    ):
        conditions = []
        params = []
        idx = 1
        if not include_secrets:
            conditions.append(f"(namespace IS NULL OR namespace <> ${idx})")
            params.append("vault")
            idx += 1
        for col, val in (("owner_id", effective_owner), ("namespace", effective_ns)):
            if val:
                conditions.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1
        for expr, val in (
            ("executed_at >= ${}::timestamptz", from_executed_at),
            ("executed_at <= ${}::timestamptz", to_executed_at),
        ):
            if val:
                conditions.append(expr.format(idx))
                params.append(val)
                idx += 1
        if cursor_executed_at and cursor_id:
            conditions.append(f"(executed_at, id) > (${idx}::timestamptz, ${idx + 1}::uuid)")
            params.extend([cursor_executed_at, cursor_id])
            idx += 2
        if export_as_of:
            conditions.append(f"executed_at <= ${idx}::timestamptz")
            params.append(export_as_of)
            idx += 1
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return await self._c.fetch(
            "SELECT id::text AS id, memory_id, content_hash, owner_id, namespace, "
            "requested_by, requested_at, executed_at, request_kind, reason, source "
            f"FROM deletion_log {where} "
            f"ORDER BY executed_at ASC, id ASC LIMIT {hard_limit + 1}",
            *params,
        )

    async def fetch_referenced_memory_allowlist(self, tx, *, referenced_ids, scope_owner=None, scope_namespace=None):
        sql = "SELECT id, owner_id, namespace FROM memories WHERE id = ANY($1::text[]) AND deleted_at IS NULL"
        params = [list(referenced_ids)]
        if scope_owner is not None:
            sql += " AND owner_id = $2"
            params.append(scope_owner)
            if scope_namespace is not None:
                sql += " AND namespace = $3"
                params.append(scope_namespace)
        elif scope_namespace is not None:
            sql += " AND namespace = $2"
            params.append(scope_namespace)
        return await self._c.fetch(sql, *params)

    async def insert_memory(self, tx, **kw):
        return await self._c.execute("INSERT INTO memories ...", *_ordered(kw, _INSERT_MEMORY_ORDER))

    async def fetch_memory_by_id(self, tx, memory_id):
        return await self._c.fetchrow(
            "SELECT content, category, subcategory, metadata, quality_rating, "
            "verbatim_content, owner_id, namespace, permission_mode, "
            "source_model, source_provider, source_session, source_agent, "
            "created, updated FROM memories WHERE id = $1 AND deleted_at IS NULL",
            memory_id,
        )

    async def set_suppress_version_snapshot(self, tx):
        await self._c.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")

    async def fetch_versioned_memory_ids(self, tx, memory_ids):
        return await self._c.fetch(
            "SELECT DISTINCT memory_id FROM memory_versions WHERE memory_id = ANY($1::text[]) AND deleted_at IS NULL",
            list(memory_ids),
        )

    async def fetch_memory_head_checks(self, tx, memory_ids):
        return await self._c.fetch(
            "SELECT m.id, m.content AS memory_content, mv.content AS head_content "
            "FROM memories m LEFT JOIN memory_branches b ON b.memory_id = m.id "
            "LEFT JOIN memory_versions mv ON mv.id = b.head_version_id "
            "WHERE m.id = ANY($1::text[])",
            list(memory_ids),
        )


class _ConnKG:
    def __init__(self, conn):
        self._c = conn

    async def fetch_kg_triples_for_export(
        self,
        tx,
        *,
        memory_ids,
        effective_owner,
        effective_ns,
        include_unattached,
        hard_limit,
        include_secrets=False,
    ):
        if not memory_ids and not include_unattached:
            return []
        return await self._c.fetch(
            "SELECT id, subject, predicate, object, subject_type, object_type, "
            "valid_from, valid_until, memory_id, confidence, created, owner_id, "
            "namespace FROM kg_triples WHERE deleted_at IS NULL AND "
            f"memory_id = ANY($1::text[]) LIMIT {hard_limit + 1}",
            list(memory_ids),
        )

    async def insert_kg_triple(self, tx, **kw):
        return await self._c.execute("INSERT INTO kg_triples ...", *_ordered(kw, _INSERT_KG_ORDER))

    async def fetch_kg_triple_by_id(self, tx, triple_id):
        return await self._c.fetchrow(
            "SELECT subject, predicate, object, subject_type, object_type, "
            "memory_id, confidence, owner_id, namespace, valid_from, valid_until, "
            "created FROM kg_triples WHERE id = $1 AND deleted_at IS NULL",
            triple_id,
        )


class _ConnVersions:
    def __init__(self, conn):
        self._c = conn

    async def fetch_memory_versions_for_export(
        self,
        tx,
        *,
        memory_ids,
        effective_owner,
        effective_ns,
        hard_limit,
        include_secrets=False,
    ):
        if not memory_ids:
            return []
        return await self._c.fetch(
            "SELECT id::text AS id, memory_id, version_num, content, category, "
            "subcategory, metadata, verbatim_content, owner_id, namespace, "
            "permission_mode, source_model, source_provider, source_session, "
            "source_agent, snapshot_at, snapshot_by, change_type, commit_hash, "
            "parent_version_id::text AS parent_version_id, branch, merge_parents "
            "FROM memory_versions WHERE deleted_at IS NULL AND "
            "memory_id = ANY($1::text[]) "
            "ORDER BY memory_id ASC, branch ASC, version_num ASC "
            f"LIMIT {hard_limit + 1}",
            list(memory_ids),
        )

    async def fetch_memory_versions_by_ids(self, tx, version_ids):
        return await self._c.fetch(
            "SELECT id::text AS id, memory_id, owner_id, namespace "
            "FROM memory_versions WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL",
            list(version_ids),
        )

    async def insert_memory_version(self, tx, **kw):
        return await self._c.execute("INSERT INTO memory_versions ...", *_ordered(kw, _INSERT_VERSION_ORDER))

    async def fetch_memory_version_by_id(self, tx, version_id):
        return await self._c.fetchrow(
            "SELECT memory_id, owner_id, namespace, version_num, content, "
            "commit_hash, parent_version_id::text AS parent_version_id, branch, "
            "merge_parents, category, subcategory, metadata, verbatim_content, "
            "permission_mode, source_model, source_provider, source_session, "
            "source_agent, snapshot_at, snapshot_by, change_type "
            "FROM memory_versions WHERE id = $1::uuid AND deleted_at IS NULL",
            version_id,
        )


class _ConnBranches:
    def __init__(self, conn):
        self._c = conn

    async def fetch_memory_branch_heads(self, tx, memory_ids, *, authorized_version_uuids=None):
        # Multi-line on purpose. The legacy query was formatted this way, so
        # the single-line substring "FROM memory_versions WHERE memory_id =
        # ANY" -- which suites use to route fetch_versioned_memory_ids -- does
        # NOT match it. Collapsing it to one line silently routes branch-head
        # reads to the versioned-ids row set, which lacks `branch`.
        return await self._c.fetch(
            """
            SELECT DISTINCT ON (memory_id, branch)
                memory_id, branch, id::text AS head_version_id
            FROM memory_versions
            WHERE memory_id = ANY($1::text[])
              AND deleted_at IS NULL
            ORDER BY memory_id, branch, version_num DESC
            """,
            list(memory_ids),
            list(authorized_version_uuids) if authorized_version_uuids is not None else None,
        )

    async def upsert_memory_branch_head(self, tx, *, memory_id, branch, head_version_id):
        await self._c.execute("INSERT INTO memory_branches ...", memory_id, branch, head_version_id)

    async def delete_memory_branches_for_memories(self, tx, memory_ids):
        await self._c.execute(
            "DELETE FROM memory_branches WHERE memory_id = ANY($1::text[])",
            list(memory_ids),
        )


class _ConnCompression:
    def __init__(self, conn):
        self._c = conn

    async def fetch_compressed_variants_for_export(
        self,
        tx,
        *,
        memory_ids,
        effective_owner,
        hard_limit,
    ):
        if not memory_ids:
            return []
        return await self._c.fetch(
            "SELECT memory_id, owner_id, winner_candidate_id, engine_id, "
            "engine_version, compressed_content, compressed_tokens, "
            "compression_ratio, quality_score, composite_score, scoring_profile, "
            "judge_model, selected_at FROM memory_compressed_variants "
            f"WHERE memory_id = ANY($1::text[]) LIMIT {hard_limit + 1}",
            list(memory_ids),
        )

    async def compression_candidate_exists(self, tx, *, candidate_id, memory_id, owner_id):
        fetchval = getattr(self._c, "fetchval", None)
        if fetchval is not None:
            return bool(
                await fetchval(
                    "SELECT 1 FROM memory_compression_candidates "
                    "WHERE id = $1::uuid AND memory_id = $2 AND owner_id = $3",
                    candidate_id,
                    memory_id,
                    owner_id,
                )
            )
        row = await self._c.fetchrow(
            "SELECT 1 FROM memory_compression_candidates WHERE id = $1::uuid AND memory_id = $2 AND owner_id = $3",
            candidate_id,
            memory_id,
            owner_id,
        )
        return bool(row)

    async def insert_compressed_variant(self, tx, **kw):
        return await self._c.execute(
            "INSERT INTO memory_compressed_variants ...",
            *_ordered(kw, _INSERT_VARIANT_ORDER),
        )

    async def fetch_compressed_variant_by_memory_id(self, tx, memory_id):
        return await self._c.fetchrow(
            "SELECT owner_id, winner_candidate_id::text AS winner_candidate_id, "
            "engine_id, engine_version, compressed_content, compressed_tokens, "
            "compression_ratio, quality_score, composite_score, scoring_profile, "
            "judge_model, selected_at FROM memory_compressed_variants "
            "WHERE memory_id = $1",
            memory_id,
        )


class ConnBackedBackend:
    """Persistence-ABC facade over a mock asyncpg connection (tests only)."""

    supports_webhooks = False
    audit_chain = None

    def __init__(self, conn):
        self.conn = conn
        self.memories = _ConnMemories(conn)
        self.kg_triples = _ConnKG(conn)
        self.memory_versions = _ConnVersions(conn)
        self.memory_branches = _ConnBranches(conn)
        self.compression = _ConnCompression(conn)
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transactional(self, *, isolation=None, readonly=False):
        self.last_isolation = isolation
        self.last_readonly = readonly
        tx = _FakeTx(self.conn)
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def fetch_server_now(self, tx):
        row = await self.conn.fetchrow("SELECT now() AS now")
        if row is None:
            from datetime import UTC, datetime

            return datetime.now(UTC)
        try:
            return row["now"]
        except (KeyError, IndexError, TypeError):
            from datetime import UTC, datetime

            return datetime.now(UTC)

    async def close(self):
        return None


def install_conn_backend(monkeypatch, conn):
    """Install a ConnBackedBackend over ``conn`` as the lifecycle backend."""
    import mnemos.core.lifecycle as lc

    backend = ConnBackedBackend(conn)
    monkeypatch.setattr(lc, "_persistence_backend", backend)
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_rls_enabled", False)
    monkeypatch.setattr(lc, "_cache", None)
    return backend
