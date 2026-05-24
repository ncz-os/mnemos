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
from typing import Any, AsyncContextManager, Protocol, runtime_checkable

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


class PersistenceBackend(ABC):
    """Top-level facade exposing backend-specific repository families."""

    @abstractmethod
    def transactional(self) -> AsyncContextManager[Transaction]:
        """Open a backend-neutral transaction context."""
        ...

    @property
    @abstractmethod
    def memories(self) -> MemoryRepository: ...

    @property
    @abstractmethod
    def kg_triples(self) -> KGRepository: ...

    @property
    @abstractmethod
    def memory_versions(self) -> VersionRepository: ...

    @property
    @abstractmethod
    def memory_branches(self) -> BranchRepository: ...

    @property
    @abstractmethod
    def compression(self) -> CompressionRepository: ...

    @property
    @abstractmethod
    def webhooks(self) -> WebhookRepository: ...

    @property
    @abstractmethod
    def consultations_audit(self) -> ConsultationAuditRepository: ...

    @property
    @abstractmethod
    def oauth(self) -> OAuthRepository: ...

    @property
    @abstractmethod
    def sessions(self) -> SessionsRepository: ...

    @property
    @abstractmethod
    def consultations(self) -> ConsultationsRepository: ...

    @property
    @abstractmethod
    def federation(self) -> FederationRepository: ...

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

    @abstractmethod
    async def ping(self) -> bool: ...

    @property
    @abstractmethod
    def state_kv(self) -> StateRepository: ...

    @abstractmethod
    async def close(self) -> None: ...


@asynccontextmanager
async def null_transaction(tx: Transaction) -> AsyncIterator[Transaction]:
    """Yield an existing transaction without managing its lifecycle."""
    yield tx
