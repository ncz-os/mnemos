"""Backend-neutral persistence interfaces for MNEMOS.

D.1 backend abstraction is complete for the primary memory graph,
federation, and state key-value surfaces; API and domain orchestration
code should depend on this facade instead of driver-specific SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncContextManager, Literal, Protocol, TypeAlias, Union, runtime_checkable

from fastapi import HTTPException

from mnemos.core.auth_context import UserContext
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter


class DuplicateMemoryError(ValueError):
    """Raised when an explicit memory id already exists."""


@dataclass(frozen=True)
class MemoryStatsRow:
    """Backend-neutral aggregate snapshot for ``GET /stats``.

    One round-trip per backend. ``avg_quality_rating`` is ``None`` when
    no scored rows exist; the handler picks the published default.
    """

    total_memories: int
    native_memories: int
    federated_memories: int
    memories_by_peer: dict[str, int] = field(default_factory=dict)
    memories_by_category: dict[str, int] = field(default_factory=dict)
    memories_by_subcategory: dict[str, dict[str, int]] = field(default_factory=dict)
    avg_quality_rating: float | None = None


@dataclass(frozen=True)
class CompressionStatsRow:
    """Backend-neutral aggregate snapshot for the compression slice of
    ``GET /stats``."""

    total_compressions: int
    average_compression_ratio: float | None
    unreviewed_compressions: int


@dataclass(frozen=True)
class UsageLedgerRecord:
    """Input payload for a usage_ledger insert."""

    provider: str
    model: str
    task_kind: str
    tokens_in: int
    tokens_out: int
    tokens_reasoning: int
    latency_ms: int
    outcome: str
    caller_subsystem: str
    tier: str
    session_id: str | None = None
    request_count: int = 1
    plan_window_id: str | None = None
    path_kind: str = "api"


@dataclass(frozen=True)
class UsageLedgerResult:
    """Backend-neutral result returned after recording usage."""

    id: int
    est_cost_usd: Decimal


@runtime_checkable
class Transaction(Protocol):
    """Backend-neutral transaction handle.

    Repository methods accept this protocol instead of exposing driver-specific
    connection objects. Concrete repositories are responsible for translating
    the handle into their backend's private connection/session type.
    """

    async def commit(self) -> None:
        """Commit the transaction."""
        ...

    async def rollback(self) -> None:
        """Rollback the transaction."""
        ...


class MemoryRepository(ABC):
    """Memory row, memory export, and memory DAG read operations."""

    @abstractmethod
    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: UserContext) -> None: ...

    @abstractmethod
    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: UserContext,
    ) -> list[Row]: ...

    @abstractmethod
    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: UserContext,
    ) -> tuple[Row | None, Row | None]: ...

    @abstractmethod
    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: UserContext,
    ) -> Row | None: ...

    @abstractmethod
    async def fetch_memory_export(
        self,
        tx: Transaction,
        *,
        effective_owner: str | None,
        effective_ns: str | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]: ...

    @abstractmethod
    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]: ...

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None: ...

    @abstractmethod
    async def set_suppress_version_snapshot(self, tx: Transaction) -> None: ...

    @abstractmethod
    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]: ...

    @abstractmethod
    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]: ...

    @abstractmethod
    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    # --- v4.1 handler-through-backend surface ---------------------------------

    @abstractmethod
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
        """List memories under the given visibility filter, ordered
        ``created DESC``.

        Returns ``(rows, total_count)`` where ``total_count`` is the
        ``COUNT(*)`` over the same predicate (pre-LIMIT/OFFSET) so the
        handler can populate paged response totals without a second
        round-trip.
        """
        ...

    @abstractmethod
    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        """Fetch a memory by id, applying the visibility filter.

        Returns ``None`` when the memory does not exist OR when the
        filter excludes it. The 404-vs-403 distinction is intentionally
        collapsed at this layer to keep cross-tenant existence
        invisible; the handler returns 404 for both.
        """
        ...

    @abstractmethod
    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        """Apply ``fields`` patch to a memory. Returns the updated row,
        or ``None`` if the memory does not exist or the filter excludes
        it. Mutation paths use ``VisibilityScope.OWN_ONLY`` — non-owner
        callers cannot edit a row they merely have read access to via
        group/world bits.

        ``fields`` keys are validated and translated by the handler;
        the repository assumes they map cleanly to memory columns.
        """
        ...

    @abstractmethod
    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        """Find an active memory with identical normalized content."""
        ...

    @abstractmethod
    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        """Increment recall counters for one memory and return it."""
        ...

    @abstractmethod
    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        """Return active duplicate-content groups for ARTEMIS sweeps."""
        ...

    @abstractmethod
    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        """Soft-consolidate duplicate memories into a canonical row."""
        ...

    @abstractmethod
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
        """Delete a memory if it exists and the filter admits.

        Returns the deleted row metadata if a row was deleted. Non-owner
        callers see ``None`` even for memories they could otherwise read.
        """
        ...

    @abstractmethod
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
        """Vector search over memory embeddings, applying visibility.

        Returns full memory rows (not the join-only shape used by the
        legacy SQLite helper), so the handler can hand them straight to
        ``row_to_memory`` without a second fetch.

        Vector ranking is backend-owned: Postgres ranks with pgvector
        ``ORDER BY embedding <=>`` and SQLite ranks in SQL via
        ``mnemos_cosine_similarity``. Postgres can optionally rerank a
        wider vector candidate set with a decayed recency boost.
        """
        ...

    @abstractmethod
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
        """Full-text search over memory content, applying visibility."""
        ...

    @abstractmethod
    async def gather_stats(self, tx: Transaction) -> MemoryStatsRow:
        """Aggregate counters used by ``GET /stats``. System-level view
        with no visibility filter — only operators reach this path."""
        ...


class KGRepository(ABC):
    """Knowledge graph triple persistence."""

    @abstractmethod
    async def fetch_kg_triples_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        include_unattached: bool,
        hard_limit: int,
    ) -> list[Row]: ...

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None: ...


class VersionRepository(ABC):
    """Memory version persistence and topology lookups."""

    @abstractmethod
    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]: ...

    @abstractmethod
    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]: ...

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None: ...


class BranchRepository(ABC):
    """Memory branch persistence."""

    @abstractmethod
    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: UserContext,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None: ...

    @abstractmethod
    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]: ...

    @abstractmethod
    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None: ...


class CompressionRepository(ABC):
    """Compressed memory variant persistence."""

    @abstractmethod
    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]: ...

    @abstractmethod
    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool: ...

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None: ...

    @abstractmethod
    async def gather_stats(self, tx: Transaction) -> CompressionStatsRow:
        """Aggregate compression counters used by ``GET /stats``."""
        ...


class WebhookRepository(ABC):
    """Webhook persistence surface.

    The v4.0 webhook outbox contract requires that every event-producing
    write commit a ``webhook_attempts`` row in the same database
    transaction as the triggering data write. ``enqueue_webhook_attempt``
    is the backend-neutral entry point for that — both backends
    implement it so handlers can preserve the transactional outbox
    property without reaching into ``mnemos.webhooks`` from inside a
    repository (forbidden by the persistence-no-upward-deps contract).
    """

    @abstractmethod
    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        """Append ``webhook_deliveries`` rows for every matching
        subscription, inside ``tx``, and return their delivery IDs.

        Both backends must atomically commit these rows alongside the
        triggering data write — that is the v4.0 outbox contract. The
        delivery worker reads the queue separately and performs the
        HTTP send; this method never dispatches over HTTP, despite the
        legacy name. The returned IDs let callers schedule the delivery
        attempt via ``mnemos.core.lifecycle._schedule_delivery_attempt``
        once the outer transaction has committed.
        """
        ...


class ConsultationAuditRepository(ABC):
    """OpenAI-compatible gateway and consultation audit persistence lookups."""

    @abstractmethod
    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]: ...

    @abstractmethod
    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None: ...

    @abstractmethod
    async def fetch_available_models(self, tx: Transaction) -> list[Row]: ...

    @abstractmethod
    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None: ...

    # ── model-registry WRITES (daily provider sync; backend-overridable) ───────
    # Non-abstract so existing backends keep instantiating; backends that own a
    # live model_registry (Oracle) override these.
    async def upsert_model(self, tx: Transaction, model: dict[str, Any]) -> bool:
        raise NotImplementedError("upsert_model not implemented for this backend")

    async def mark_models_unavailable(
        self, tx: Transaction, provider: str, seen_model_ids: Sequence[str]
    ) -> int:
        raise NotImplementedError("mark_models_unavailable not implemented for this backend")

    async def write_model_sync_log(
        self,
        tx: Transaction,
        *,
        provider: str,
        models_found: int,
        added: int,
        updated: int,
        deprecated: int,
        error: str | None,
        duration_ms: int,
    ) -> None:
        raise NotImplementedError("write_model_sync_log not implemented for this backend")

    async def update_arena_score(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        family: str,
        arena_score: float,
        arena_rank: int,
        graeae_weight: float,
    ) -> int:
        raise NotImplementedError("update_arena_score not implemented for this backend")


class OAuthRepository(ABC):
    """OAuth provider, identity, and browser-session persistence."""

    @abstractmethod
    async def list_enabled_providers(self, tx: Transaction) -> list[Row]: ...

    @abstractmethod
    async def get_provider(self, tx: Transaction, name: str) -> Row | None: ...

    @abstractmethod
    async def provision_or_link_user(
        self,
        tx: Transaction,
        *,
        provider: str,
        external_id: str,
        claims: dict[str, Any],
    ) -> tuple[str, str]: ...

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    async def revoke_session(self, tx: Transaction, session_id: str) -> bool: ...

    @abstractmethod
    async def revoke_all_sessions(self, tx: Transaction, user_id: str) -> int: ...

    @abstractmethod
    async def get_identity_for_session(self, tx: Transaction, session_id: str) -> Row | None: ...


class SessionsRepository(ABC):
    """Stateful chat session persistence."""

    @abstractmethod
    async def create_session(
        self,
        tx: Transaction,
        *,
        user_id: str,
        namespace: str,
        model: str,
        initial_context: str | None,
    ) -> Row: ...

    @abstractmethod
    async def get_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> Row | None: ...

    @abstractmethod
    async def list_injected_memory_ids(self, tx: Transaction, session_id: str, limit: int = 10) -> list[str]: ...

    @abstractmethod
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
    ) -> Any: ...

    @abstractmethod
    async def fetch_provider_history(self, tx: Transaction, session_id: str) -> list[Row]: ...

    @abstractmethod
    async def add_memory_injections(
        self,
        tx: Transaction,
        *,
        session_id: str,
        message_id: Any,
        memory_ids: Sequence[str],
    ) -> None: ...

    @abstractmethod
    async def update_metrics(
        self,
        tx: Transaction,
        *,
        session_id: str,
        user_id: str,
        namespace: str,
        tokens_used: int,
    ) -> None: ...

    @abstractmethod
    async def fetch_history(
        self, tx: Transaction, session_id: str, limit: int, offset: int
    ) -> tuple[list[Row], int]: ...

    @abstractmethod
    async def delete_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> bool: ...


class ConsultationsRepository(ABC):
    """GRAEAE consultation persistence separate from model recommendation audit."""

    @abstractmethod
    async def resolve_tier_lineup(self, tx: Transaction, tier: str) -> list[Row]: ...

    @abstractmethod
    async def resolve_models(self, tx: Transaction, model_ids: Sequence[str]) -> list[Row]: ...

    @abstractmethod
    async def create_consultation_with_audit(
        self,
        tx: Transaction,
        *,
        prompt: str,
        task_type: str,
        consensus_response: str,
        consensus_score: float,
        winning_muse: str | None,
        cost: float,
        latency_ms: int,
        mode: str,
        owner_id: str,
        namespace: str,
        memory_ids: Sequence[str],
        genesis_hash: str,
    ) -> Any: ...

    @abstractmethod
    async def list_audit_log(
        self,
        tx: Transaction,
        *,
        root: bool,
        user_id: str,
        namespace: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]: ...

    @abstractmethod
    async def fetch_audit_chain(
        self,
        tx: Transaction,
        *,
        root: bool,
        user_id: str,
        namespace: str | None,
    ) -> list[Row]: ...

    @abstractmethod
    async def get_consultation(
        self,
        tx: Transaction,
        *,
        consultation_id: str,
        root: bool,
        user_id: str,
        namespace: str | None,
    ) -> Row | None: ...

    @abstractmethod
    async def get_consultation_artifacts(
        self,
        tx: Transaction,
        *,
        consultation_id: str,
        root: bool,
        user_id: str,
        namespace: str | None,
    ) -> tuple[Row | None, list[Row]]: ...


class FederationRepository(ABC):
    """Federation persistence surface."""

    @abstractmethod
    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: Any | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]: ...

    @abstractmethod
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
    ) -> Row: ...

    @abstractmethod
    async def list_peers(self, tx: Transaction) -> list[Row]: ...

    @abstractmethod
    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None: ...

    @abstractmethod
    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None: ...

    @abstractmethod
    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None: ...

    @abstractmethod
    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool: ...

    @abstractmethod
    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]: ...

    @abstractmethod
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
        """Return federation feed rows.

        When ``include_embedding=True``, rows additionally include the
        ``embedding`` column (raw vector bytes/list) and ``embedding_model``
        literal column.  Used by the v6.1 F-1 ``copy_embeddings`` flow so
        replicas can ingest pre-computed vectors instead of re-embedding.
        Default ``False`` preserves v6.0 wire format / bandwidth profile.
        See ``docs/v6.1-federation-embeddings-copy.md``.
        """
        ...

    @abstractmethod
    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None: ...

    @abstractmethod
    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None: ...

    @abstractmethod
    async def update_peer_schema_check(
        self,
        tx: Transaction,
        peer_id: str,
        peer_version: str | None,
    ) -> None: ...

    @abstractmethod
    async def record_schema_abort(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        peer_version: str | None,
        cursor_before: Any,
        error: str,
        is_transient: bool,
    ) -> None: ...

    @abstractmethod
    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any: ...

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None: ...

    @abstractmethod
    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None: ...

    @abstractmethod
    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]: ...

    @abstractmethod
    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None: ...

    @abstractmethod
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
    ) -> bool: ...

    @abstractmethod
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
    ) -> bool: ...

    @abstractmethod
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
    ) -> bool: ...

    @abstractmethod
    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int: ...


class StateRepository(ABC):
    """State key-value persistence surface."""

    @abstractmethod
    async def get(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> Row | None: ...

    @abstractmethod
    async def set(
        self,
        tx: Transaction,
        key: str,
        value: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        expires_at: Any | None = None,
    ) -> Row | None: ...

    @abstractmethod
    async def delete(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> bool: ...

    @abstractmethod
    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]: ...

    @abstractmethod
    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int: ...


class AuditChainRepository(ABC):
    """v6.2 M-2.2.1 per-memory append-only audit chain.

    Backends provide two operations: fetch the latest entry for a
    memory (so the next builder call has prev_entry_id +
    prev_entry_hash) and atomically insert a new entry alongside the
    memory upsert. Sealer worker (mnemos/workers/audit_sealer.py)
    additionally claims unsealed-window entries via SELECT ... FOR
    UPDATE SKIP LOCKED and stamps global_root + global_seq columns.

    Schema reference: db/migrations*/0029_memory_audit_chain.sql +
    0030_memory_audit_roots.sql (shipped at 614d483).

    Implementations are intentionally minimal — the canonical
    bytes/signing/hash logic lives in mnemos/audit/ and is backend-
    agnostic. The backend only persists the bytes.
    """

    supports_webhooks = True

    @abstractmethod
    async def get_latest_audit_entry(
        self,
        tx: Transaction,
        memory_id: bytes,
    ) -> Row | None:
        """Return the most-recent audit chain row for ``memory_id``.

        Used by the route handler to populate the next entry's
        ``prev_entry_id`` and ``prev_entry_hash``. Returns ``None`` for
        first-write memories. Implementations should ``ORDER BY
        signed_at DESC LIMIT 1`` over the memory_audit_chain table.
        """
        ...

    @abstractmethod
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
        """Insert a signed audit entry; commits in the caller's tx so
        the memory UPSERT and the audit entry are atomic.

        ``op`` MUST be one of: ``create``, ``update``, ``delete``,
        ``archive``, ``replicate`` (enforced by CHECK constraint;
        callers usually pass a build_entry() AuditOp Literal).
        """
        ...

    @abstractmethod
    async def claim_unsealed_window(
        self,
        tx: Transaction,
        *,
        max_window_seconds: int,
        limit: int,
    ) -> list[Row]:
        """Sealer-side: claim the next unsealed window.

        Backends pick rows where ``global_root IS NULL`` AND
        ``signed_at <= now - max_window_seconds``, oldest-first, up
        to ``limit`` entries, using a SKIP-LOCKED row lock so multiple
        sealer instances coexist safely. Caller computes the Merkle
        root + signs it + writes ``memory_audit_roots`` + UPDATEs
        these rows' ``global_root`` + ``global_seq`` in the same tx.
        """
        ...

    @abstractmethod
    async def stamp_window_with_root(
        self,
        tx: Transaction,
        *,
        entry_ids: list[bytes],
        global_root: bytes,
        starting_seq: int,
    ) -> None:
        """Sealer-side: UPDATE memory_audit_chain SET global_root,
        global_seq for the given entry_ids. Order preserved — entry
        at position ``i`` gets ``starting_seq + i``.
        """
        ...

    @abstractmethod
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
        """Sealer-side: INSERT into memory_audit_roots."""
        ...

    @abstractmethod
    async def list_window_entries(
        self,
        tx: Transaction,
        global_root: bytes,
    ) -> list[Row]:
        """Return all entries sealed under ``global_root`` ordered by
        (signed_at, entry_id) -- the SAME order the sealer used to
        compute the Merkle leaves. Critical for the inclusion-proof
        endpoint to reconstruct the tree deterministically.

        Returns ``[]`` when no entries match (root unknown OR
        empty-window seal). Caller treats empty as 404.
        """
        ...

    @abstractmethod
    async def get_audit_entry_by_id(
        self,
        tx: Transaction,
        entry_id: bytes,
    ) -> Row | None:
        """Fetch a single audit entry by its primary-key ``entry_id``.

        Used by the inclusion-proof endpoint to look up the target
        entry without going through ``get_latest_audit_entry`` (which
        scans by memory_id). Returns ``None`` when entry_id is
        unknown. Caller treats None as 404.
        """
        ...

    @abstractmethod
    async def get_chain_stats(self, tx: Transaction) -> dict:
        """Return per-backend audit-chain health snapshot.

        Used by the `/v1/audit/health` endpoint + operator dashboards.
        Returns:
            {
                "total_entries": int,
                "unsealed_count": int,
                "oldest_unsealed_signed_at": str | None,  # ISO 8601
                "sealed_root_count": int,
                "last_sealed_at": str | None,             # ISO 8601
            }
        """
        ...

    async def get_latest_audit_entries_batch(
        self,
        tx: Transaction,
        memory_ids: list[bytes],
    ) -> dict[bytes, Row]:
        """Batch version of ``get_latest_audit_entry`` for N memories.

        Default fallback impl serially calls
        ``get_latest_audit_entry`` per id; backends override with a
        single SQL query (typically `WHERE memory_id = ANY(...)` +
        window function or a CTE picking the max signed_at per
        memory_id). The federation-feed audit-head piggyback hot-path
        is the canonical caller -- N+1 audit reads otherwise.

        Returns a dict keyed by ``memory_id`` for entries that have
        any audit history; absent keys mean no audit entries for
        that memory_id.
        """
        result: dict[bytes, Row] = {}
        for mid in memory_ids:
            row = await self.get_latest_audit_entry(tx, mid)
            if row is not None:
                result[mid] = row
        return result


class CompressionQueueRepository(ABC):
    """v3.1 distillation/compression work queue — backend-agnostic.

    GAP 1 of job 019e7049: the queue + worker-pool claim were written
    directly against asyncpg/Postgres (``workers/distillation.py``
    imports ``asyncpg``; ``domain/compression/worker_contest.py`` runs
    raw ``FOR UPDATE SKIP LOCKED``; ``domain/admin_lifecycle_repo.py``
    enqueue is asyncpg-only). On Oracle the admin enqueue routes 503 and
    the contest never drains. This ABC moves the queue mechanics behind
    the persistence surface so every hive backend (Postgres, Oracle,
    DB2, MySQL) runs the contest with an IDENTICAL schema + feature set
    (architectural law mem_1780005765033). SQLite implements it for
    ABC-completeness only — not a hive target.

    The six primitives below are the SQL-level contract. Worker-side
    orchestration (asyncpg pool management, infra-retry connection
    resets) stays in the worker layer and is rewired to call these
    primitives in CHILD C.

    Schema reference (canonical): db/migrations_v3_1_compression.sql
    (Postgres) + db/migrations_oracle/0040_memory_compression_queue_parity.sql.
    Columns: id, memory_id, owner_id, reason, status, priority,
    scoring_profile, attempts, enqueued_at, started_at, finished_at,
    error.

    Concurrency contract: ``dequeue`` and ``sweep_stale`` MUST claim
    rows with a SKIP-LOCKED row lock so multiple contest workers
    coexist without double-processing. Backends without SKIP LOCKED
    (SQLite) serialise via a single-writer transaction
    (``BEGIN IMMEDIATE``) instead.

    GRAEAE consult 1c3e8a7f (athena/hephaestus/metis).
    """

    @abstractmethod
    async def enqueue_compression(
        self,
        tx: Transaction,
        *,
        memory_ids: list[str],
        reason: str,
        priority: int,
        scoring_profile: str,
    ) -> list[str]:
        """Enqueue specific memories for compression.

        Skips ids that don't resolve to a live (non-deleted) memory;
        resolves each row's ``owner_id`` from ``memories`` and inserts a
        ``pending`` queue row. Returns the list of memory_ids that were
        actually enqueued (subset of ``memory_ids``). ``reason`` is one
        of ``on_write|manual|scheduled|reprocess``; ``scoring_profile``
        is ``balanced|quality_first|speed_first|custom`` (CHECK-enforced).
        Queue-row ids are DB-default generated (the INSERT omits ``id`` on
        every backend, matching PG's ``gen_random_uuid()`` default).
        """
        ...

    @abstractmethod
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
        """Bulk-enqueue eligible memories, longest-content-first.

        Selects live memories (optionally filtered by ``category`` and,
        when ``only_uncompressed``, those with no row in
        ``memory_compressed_variants``), ordered by content length DESC,
        capped at ``limit``, and inserts a ``pending`` queue row for
        each in a single set-based statement. Returns the number of rows
        enqueued.
        """
        ...

    @abstractmethod
    async def dequeue_compression(
        self,
        tx: Transaction,
        *,
        limit: int,
    ) -> list[Row]:
        """Atomically claim the next ``limit`` pending tasks.

        Selects ``status = 'pending'`` rows ordered by ``priority DESC,
        enqueued_at`` under a SKIP-LOCKED row lock, flips them to
        ``running`` (stamping ``started_at`` and incrementing
        ``attempts``) in the same statement/transaction, and returns the
        claimed rows with at least ``id, memory_id, owner_id, reason,
        scoring_profile, attempts``. Returns ``[]`` when the queue is
        empty or all candidates are locked by peers.
        """
        ...

    @abstractmethod
    async def mark_compression_done(
        self,
        tx: Transaction,
        *,
        queue_id: str,
    ) -> None:
        """Mark a claimed task ``done`` (sets finished_at, clears error)."""
        ...

    @abstractmethod
    async def mark_compression_failed(
        self,
        tx: Transaction,
        *,
        queue_id: str,
        error: str,
    ) -> None:
        """Mark a claimed task ``failed`` (sets finished_at + error)."""
        ...

    @abstractmethod
    async def sweep_stale_compression(
        self,
        tx: Transaction,
        *,
        stale_threshold_secs: int,
        max_attempts: int,
    ) -> int:
        """Reclaim ``running`` rows stranded past ``stale_threshold_secs``.

        Claims stale rows under SKIP-LOCKED and applies the
        terminalization rules (identical on every backend):

        * ``attempts >= max_attempts`` AND ``error`` is a recorded
          content/contest failure (NOT NULL and not an ``infra_retry:``
          breadcrumb) → mark ``failed``.
        * ``attempts >= max_attempts`` but ``error`` is NULL or an
          ``infra_retry:`` breadcrumb → reset to ``pending`` AND
          decrement ``attempts`` (the wedged-pool path: don't
          terminalize a content-OK row from pure infra pressure).
        * ``attempts < max_attempts`` → reset to ``pending`` (next
          dequeue retries), attempts preserved.

        Returns the number of rows reclaimed/terminalized.
        """
        ...


CapabilityName: TypeAlias = Literal[
    "core",
    "oauth",
    "sessions",
    "consultations",
    "federation",
    "audit",
    "state",
]


CORE_CAPABILITY: CapabilityName = "core"
OAUTH_CAPABILITY: CapabilityName = "oauth"
SESSIONS_CAPABILITY: CapabilityName = "sessions"
CONSULTATIONS_CAPABILITY: CapabilityName = "consultations"
FEDERATION_CAPABILITY: CapabilityName = "federation"
AUDIT_CAPABILITY: CapabilityName = "audit"
STATE_CAPABILITY: CapabilityName = "state"
ALL_CAPABILITIES: frozenset[CapabilityName] = frozenset(
    {
        CORE_CAPABILITY,
        OAUTH_CAPABILITY,
        SESSIONS_CAPABILITY,
        CONSULTATIONS_CAPABILITY,
        FEDERATION_CAPABILITY,
        AUDIT_CAPABILITY,
        STATE_CAPABILITY,
    }
)

DetailedCapabilityName: TypeAlias = str

MEMORY_CRUD_CAPABILITY: DetailedCapabilityName = "memory_crud"
VECTOR_SEARCH_CAPABILITY: DetailedCapabilityName = "vector_search"
FTS_CAPABILITY: DetailedCapabilityName = "fts"
WEBHOOKS_CAPABILITY: DetailedCapabilityName = "webhooks"
JOURNAL_CAPABILITY: DetailedCapabilityName = "journal"
LEDGER_CAPABILITY: DetailedCapabilityName = "ledger"
KG_CAPABILITY: DetailedCapabilityName = "kg"
VERSIONS_CAPABILITY: DetailedCapabilityName = "versions"
BRANCHES_CAPABILITY: DetailedCapabilityName = "branches"
COMPRESSION_CAPABILITY: DetailedCapabilityName = "compression"
COMPRESSION_QUEUE_CAPABILITY: DetailedCapabilityName = "compression_queue"
OAUTH_DETAIL_CAPABILITY: DetailedCapabilityName = "oauth"
SESSIONS_DETAIL_CAPABILITY: DetailedCapabilityName = "sessions"
CONSULTATIONS_DETAIL_CAPABILITY: DetailedCapabilityName = "consultations"
FEDERATION_DETAIL_CAPABILITY: DetailedCapabilityName = "federation"
STATE_DETAIL_CAPABILITY: DetailedCapabilityName = "state"
AUDIT_DETAIL_CAPABILITY: DetailedCapabilityName = "audit"
ROW_LEVEL_SECURITY_CAPABILITY: DetailedCapabilityName = "row_level_security"
LISTEN_NOTIFY_CAPABILITY: DetailedCapabilityName = "listen_notify"
ADVISORY_LOCKS_CAPABILITY: DetailedCapabilityName = "advisory_locks"

FULL_STORAGE_CAPABILITY_DETAILS: frozenset[DetailedCapabilityName] = frozenset(
    {
        MEMORY_CRUD_CAPABILITY,
        VECTOR_SEARCH_CAPABILITY,
        FTS_CAPABILITY,
        WEBHOOKS_CAPABILITY,
        JOURNAL_CAPABILITY,
        LEDGER_CAPABILITY,
        KG_CAPABILITY,
        VERSIONS_CAPABILITY,
        BRANCHES_CAPABILITY,
        COMPRESSION_CAPABILITY,
        OAUTH_DETAIL_CAPABILITY,
        SESSIONS_DETAIL_CAPABILITY,
        CONSULTATIONS_DETAIL_CAPABILITY,
        FEDERATION_DETAIL_CAPABILITY,
        STATE_DETAIL_CAPABILITY,
        AUDIT_DETAIL_CAPABILITY,
    }
)

POSTGRES_CAPABILITY_DETAILS: frozenset[DetailedCapabilityName] = frozenset(
    {
        *FULL_STORAGE_CAPABILITY_DETAILS,
        ROW_LEVEL_SECURITY_CAPABILITY,
        LISTEN_NOTIFY_CAPABILITY,
        ADVISORY_LOCKS_CAPABILITY,
    }
)

MYSQL_CAPABILITY_DETAILS: frozenset[DetailedCapabilityName] = frozenset(
    {
        MEMORY_CRUD_CAPABILITY,
        VECTOR_SEARCH_CAPABILITY,
        FTS_CAPABILITY,
    }
)


class BackendCapabilityMissing(HTTPException):
    """Raised when a caller reaches a repository unsupported by a backend."""

    def __init__(self, capability: str, backend_name: str | None = None, status_code: int = 503):
        self.capability = capability
        self.backend_name = backend_name
        suffix = f" for {backend_name}" if backend_name else ""
        super().__init__(
            status_code=status_code,
            detail=f"persistence backend does not support {capability!r}{suffix}",
        )


class PersistenceCapabilityBase(Protocol):
    """Common facade shape shared by every persistence capability."""

    def transactional(self) -> AsyncContextManager[Transaction]:
        """Open a backend-neutral transaction context."""
        ...

    @property
    def capabilities(self) -> set[str]:
        """Capability names implemented by this backend."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


@runtime_checkable
class CorePersistence(PersistenceCapabilityBase, Protocol):
    """Core memory/category/search persistence surface."""

    _supports_core_persistence: Literal[True]

    async def record_usage_ledger(
        self,
        tx: Transaction,
        record: UsageLedgerRecord,
    ) -> UsageLedgerResult:
        """Record model-token usage.

        Only the Postgres backend implements KNEMON MVP Step 1.
        """
        raise NotImplementedError("usage_ledger is Postgres-only")

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        """Return rows from the per-category decay table."""
        raise NotImplementedError("category decay is not implemented")

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        """Insert or update one per-category decay row."""
        raise NotImplementedError("category decay is not implemented")

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
        """Create one journal entry."""
        raise NotImplementedError("journal persistence is not implemented")

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
        """List journal entries within one owner namespace."""
        raise NotImplementedError("journal persistence is not implemented")

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        """Delete one journal entry by scoped id."""
        raise NotImplementedError("journal persistence is not implemented")

    @property
    def memories(self) -> MemoryRepository: ...

    @property
    def kg_triples(self) -> KGRepository: ...

    @property
    def memory_versions(self) -> VersionRepository: ...

    @property
    def memory_branches(self) -> BranchRepository: ...

    @property
    def compression(self) -> CompressionRepository: ...

    @property
    def compression_queue(self) -> CompressionQueueRepository: ...

    @property
    def webhooks(self) -> WebhookRepository: ...

    @property
    def consultations_audit(self) -> ConsultationAuditRepository: ...


@runtime_checkable
class OAuthPersistence(PersistenceCapabilityBase, Protocol):
    """OAuth provider, identity, token, and browser-session persistence."""

    _supports_oauth_persistence: Literal[True]

    @property
    def oauth(self) -> OAuthRepository: ...


@runtime_checkable
class SessionsPersistence(PersistenceCapabilityBase, Protocol):
    """Chat session and session-log persistence."""

    _supports_sessions_persistence: Literal[True]

    @property
    def sessions(self) -> SessionsRepository: ...


@runtime_checkable
class ConsultationsPersistence(PersistenceCapabilityBase, Protocol):
    """GRAEAE consultation persistence."""

    _supports_consultations_persistence: Literal[True]

    @property
    def consultations(self) -> ConsultationsRepository: ...


@runtime_checkable
class FederationPersistence(PersistenceCapabilityBase, Protocol):
    """Federation peers, sync log, and feed-query persistence."""

    _supports_federation_persistence: Literal[True]

    @property
    def federation(self) -> FederationRepository: ...


@runtime_checkable
class AuditPersistence(PersistenceCapabilityBase, Protocol):
    """Memory audit-chain and audit-root persistence."""

    _supports_audit_persistence: Literal[True]

    @property
    def audit_chain(self) -> AuditChainRepository | None:
        """v6.2 M-2.2.1 audit chain repository.

        Returns ``None`` on backends that haven't shipped the audit
        chain rows yet — callers should treat None as
        ``MNEMOS_AUDIT_CHAIN=off`` (no audit writes attempted).
        Concrete backends override this property when the implementation
        lands.
        """
        return None


@runtime_checkable
class StatePersistence(PersistenceCapabilityBase, Protocol):
    """Job-state, distillation-state, and generic state-kv persistence."""

    _supports_state_persistence: Literal[True]

    @property
    def state_kv(self) -> StateRepository: ...


PersistenceBackend: TypeAlias = Union[
    CorePersistence,
    OAuthPersistence,
    SessionsPersistence,
    ConsultationsPersistence,
    FederationPersistence,
    AuditPersistence,
    StatePersistence,
]


def has_capability(backend: object, capability: str) -> bool:
    capabilities = getattr(backend, "capabilities", set())
    return capability in capabilities


# ── Feature-layer support matrix (GRAEAE consult de8f4b2b, 2026-06-01) ────────
# Each install layer needs a set of backend capabilities. A backend "supports" a
# layer only if it implements all required capabilities — derived from the
# existing per-backend ``capabilities`` set, so no per-backend edits are needed.
# core is always supported. See docs/LAYERED_INSTALL.md.
#   graeae -> "consultations": GRAEAE persists muse consultations; a backend
#             lacking it (e.g. a Db2 build that NotImplementedErrors consultation
#             persistence) cannot serve GRAEAE and fails fast at startup.
#   hive   -> no ADDITIONAL persistence-backend capability: the hive job bus is a
#             self-contained SQLite store, and KNEMON usage_ledger recording is
#             best-effort (degrades, never loses the row). The hive layer's real
#             requirement is GRAEAE (enforced by Settings.enforce_layer_direction
#             + the graeae gate), so it transitively needs "consultations".
LAYER_REQUIRED_CAPABILITIES: dict[str, set[str]] = {
    "core": set(),
    "graeae": {"consultations"},
    "hive": set(),
}


def backend_supported_layers(backend: object) -> set[str]:
    """Return the install layers a backend can fully serve (always incl. core)."""
    caps = set(getattr(backend, "capabilities", set()))
    supported = {"core"}
    for layer, required in LAYER_REQUIRED_CAPABILITIES.items():
        if layer == "core":
            continue
        if required <= caps:
            supported.add(layer)
    return supported


def assert_backend_supports_layers(backend: object, active_layers: set[str]) -> None:
    """Fail fast at startup if the backend cannot serve an enabled layer."""
    unsupported = set(active_layers) - backend_supported_layers(backend)
    if unsupported:
        backend_name = type(backend).__name__
        raise NotImplementedError(
            f"persistence backend {backend_name!r} does not support enabled "
            f"layer(s): {sorted(unsupported)}. Disable the layer "
            f"(MNEMOS_ENABLE_*) or choose a backend that implements it. "
            f"See docs/LAYERED_INSTALL.md."
        )


def capability_details_for_backend(backend: object) -> set[str]:
    details = getattr(backend, "capability_details", None)
    if details is not None:
        return set(details)

    legacy = set(getattr(backend, "capabilities", set()) or set())
    out: set[str] = set()
    if CORE_CAPABILITY in legacy:
        out.update(
            {
                MEMORY_CRUD_CAPABILITY,
                VECTOR_SEARCH_CAPABILITY,
                FTS_CAPABILITY,
                WEBHOOKS_CAPABILITY,
                JOURNAL_CAPABILITY,
                LEDGER_CAPABILITY,
                KG_CAPABILITY,
                VERSIONS_CAPABILITY,
                BRANCHES_CAPABILITY,
                COMPRESSION_CAPABILITY,
            }
        )
    if OAUTH_CAPABILITY in legacy:
        out.add(OAUTH_DETAIL_CAPABILITY)
    if SESSIONS_CAPABILITY in legacy:
        out.add(SESSIONS_DETAIL_CAPABILITY)
    if CONSULTATIONS_CAPABILITY in legacy:
        out.add(CONSULTATIONS_DETAIL_CAPABILITY)
    if FEDERATION_CAPABILITY in legacy:
        out.add(FEDERATION_DETAIL_CAPABILITY)
    if STATE_CAPABILITY in legacy:
        out.add(STATE_DETAIL_CAPABILITY)
    if AUDIT_CAPABILITY in legacy:
        out.add(AUDIT_DETAIL_CAPABILITY)
    if getattr(backend, "supports_row_level_security", False):
        out.add(ROW_LEVEL_SECURITY_CAPABILITY)
    if getattr(backend, "supports_listen_notify", False):
        out.add(LISTEN_NOTIFY_CAPABILITY)
    if getattr(backend, "supports_advisory_locks", False):
        out.add(ADVISORY_LOCKS_CAPABILITY)
    return out


def require_capability(backend: object, capability: str) -> None:
    if not has_capability(backend, capability):
        raise BackendCapabilityMissing(capability, type(backend).__name__)


@asynccontextmanager
async def null_transaction(tx: Transaction) -> AsyncIterator[Transaction]:
    """Yield an existing transaction without managing its lifecycle."""
    yield tx
