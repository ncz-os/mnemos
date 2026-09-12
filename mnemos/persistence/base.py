"""EPIMONE — the backend-neutral persistence layer for MNEMOS.

EPIMONE (Greek Επιμονή, "persistence / perseverance / tenacity") is the
storage abstraction layer: a single ``abc.ABC`` repository contract that
every subsystem and all of ``domain/`` + ``api/`` depend on, with swappable
backends behind it (SQLite by default; PostgreSQL, Oracle, IBM Db2, MySQL).
It is the trunk of the system, not a carve-out leaf — pull it and nothing
has a place to live. The import path stays ``mnemos.persistence.*``;
EPIMONE is the name of the layer, not a module rename.

D.1 backend abstraction is complete for the primary memory graph,
federation, and state key-value surfaces; API and domain orchestration
code should depend on this facade instead of driver-specific SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    est_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class UsageLedgerResult:
    """Backend-neutral result returned after recording usage."""

    id: int
    est_cost_usd: Decimal


WebhookDeliveryStatus: TypeAlias = Literal["pending", "retrying", "succeeded", "abandoned"]

WEBHOOK_LIVE_STATUSES: frozenset[WebhookDeliveryStatus] = frozenset(("pending", "retrying"))
WEBHOOK_TERMINAL_STATUSES: frozenset[WebhookDeliveryStatus] = frozenset(("succeeded", "abandoned"))


@dataclass(frozen=True, slots=True)
class WebhookSubscriptionRecord:
    """Backend-neutral webhook subscription returned by repository reads."""

    id: str
    url: str
    events: tuple[str, ...]
    description: str | None
    owner_id: str
    namespace: str
    created: datetime
    revoked: bool
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryRecord:
    """One canonical row-per-attempt webhook delivery audit record."""

    id: str
    subscription_id: str
    event_type: str
    payload: str
    payload_hash: str
    attempt_num: int
    status: WebhookDeliveryStatus
    response_status: int | None
    response_body: str | None
    error: str | None
    scheduled_at: datetime
    delivered_at: datetime | None
    created: datetime
    status_updated_at: datetime
    superseded: bool
    lease_token: str | None
    lease_expires_at: datetime | None
    writer_revision: int


@dataclass(frozen=True, slots=True)
class WebhookDeliveryClaim:
    """A claimed attempt plus the subscription data required for one HTTP send."""

    delivery: WebhookDeliveryRecord
    lease_token: str
    lease_expires_at: datetime
    claim_db_now: datetime
    url: str
    secret: str
    subscription_revoked: bool
    owner_id: str
    namespace: str


@dataclass(frozen=True, slots=True)
class WebhookDeliveryOutcome:
    """Bounded HTTP result handed to atomic delivery finalization."""

    succeeded: bool
    response_status: int | None = None
    response_body: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryIntent:
    """Per-delivery outbox row, returned from ``dispatch_event``.

    Carries the routing fields ``webhooks/dispatcher`` and
    ``webhooks/outbox`` used to publish post-commit NATS nudges — the
    delivery row itself is in the durable outbox table; this value
    object just lets the dispatcher avoid a second read in the same
    call to assemble the queued-delivery payload.

    NOTE: this is an in-process dataclass, not a persisted row. Adding
    fields here changes the ABC return contract on all five backends.
    """

    delivery_id: str
    subscription_id: str
    url: str
    namespace: str
    owner_id: str


@dataclass(frozen=True, slots=True)
class WebhookFinalizationResult:
    """Result of an ownership-fenced delivery finalization attempt."""

    applied: bool
    status: WebhookDeliveryStatus | None = None
    successor_delivery_id: str | None = None


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

    # Declarative description of the raw vector-score column that this
    # backend's semantic_search() emits, so the route can convert it to a
    # normalized cosine similarity (0..1) without backend-specific code or
    # brittle class-name sniffing. (semantic_score_column, metric) where
    # metric is one of mnemos.domain.models.METRIC_*. Subclasses override.
    # Default = oracle/mysql convention (COSINE distance under rank_score).
    SEMANTIC_SCORE_COLUMN: str = "rank_score"
    SEMANTIC_SCORE_METRIC: str = "cosine_distance"

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
        embedding: Sequence[float] | None = None,
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
        exclude_superseded: bool = False,
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

    async def backfill_missing_content_hashes(
        self,
        tx: Transaction,
        *,
        batch_size: int = 500,
        apply: bool = False,
    ) -> int:
        """Count or backfill NULL content_hash values.

        Dry-run (``apply=False``) returns the count of currently NULL hashes
        without mutating rows. Apply mode updates at most ``batch_size`` rows,
        computing the same newline-normalized SHA-256 digest used on creation.
        The operation is idempotent and only updates rows whose hash is still
        NULL.
        """
        raise NotImplementedError("content_hash backfill is not implemented for this backend")

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
        """Reversibly mark a memory deleted by setting deleted_at.

        Backends may override for efficient atomic UPDATE..RETURNING. The
        default delegates to delete_memory for legacy soft-delete backends.
        """
        return await self.delete_memory(
            tx,
            memory_id,
            visibility=visibility,
            requested_by=requested_by,
            requested_at=requested_at,
            request_kind=request_kind,
            reason=reason,
            source=source,
        )

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
        exclude_superseded: bool = False,
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
        exclude_superseded: bool = False,
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
    """Canonical webhook subscription, outbox, and delivery state contract.

    The canonical model is row-per-attempt. A chain is identified by
    ``(subscription_id, event_type, payload_hash)`` and ``attempt_num`` is
    one-based within that chain. ``pending`` and ``retrying`` are live;
    ``succeeded`` and ``abandoned`` are terminal. ``superseded`` distinguishes
    an abandoned attempt that advanced to a successor from a final failure.
    See ``docs/WEBHOOK_PERSISTENCE_CONTRACT.md`` for the complete invariants.

    New methods in sequence item 2 deliberately provide staged
    ``NotImplementedError`` defaults rather than becoming abstract immediately.
    That records the target interface without making the existing backend
    repository classes uninstantiable before their implementation items land.
    Capability advertising remains the source of truth during the transition;
    method presence alone never means delivery is supported.
    """

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
        """Create a subscription and return it without exposing ``secret``.

        The caller generates ``subscription_id`` and ``secret`` so all backends
        receive identical values. URL and event validation happens above the
        repository boundary. The insert and returned row are part of ``tx``.
        """
        raise NotImplementedError("create_subscription not implemented for this backend")

    async def list_subscriptions(
        self,
        tx: Transaction,
        *,
        owner_id: str | None,
        namespace: str | None,
        include_revoked: bool,
        limit: int,
    ) -> list[WebhookSubscriptionRecord]:
        """List newest subscriptions within an optional owner/namespace scope.

        ``None`` for both scope fields is the root/operator view. Implementations
        must reject a partial scope (exactly one field ``None``) and must not
        return secrets.
        """
        raise NotImplementedError("list_subscriptions not implemented for this backend")

    async def get_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
    ) -> WebhookSubscriptionRecord | None:
        """Return one scoped subscription, or ``None`` when it is not visible.

        Both scope fields must be present for a tenant read or absent for a
        root/operator read; implementations reject a partial scope.
        """
        raise NotImplementedError("get_subscription not implemented for this backend")

    async def revoke_subscription(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
    ) -> bool:
        """Soft-revoke one visible active subscription, idempotently.

        Return ``True`` only when this call changed the row from active to
        revoked. Both scope fields must be present or absent together. Delivery
        history is never deleted.
        """
        raise NotImplementedError("revoke_subscription not implemented for this backend")

    async def list_deliveries(
        self,
        tx: Transaction,
        *,
        subscription_id: str,
        owner_id: str | None,
        namespace: str | None,
        limit: int,
    ) -> list[WebhookDeliveryRecord]:
        """List newest attempts for one visible subscription, without secrets.

        Both scope fields must be present or absent together.
        """
        raise NotImplementedError("list_deliveries not implemented for this backend")

    @abstractmethod
    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[WebhookDeliveryIntent]:
        """Append one first-attempt row per matching active subscription.

        The rows must commit atomically with the triggering data write. Each
        row starts at ``attempt_num=1``, ``status='pending'``, is not
        superseded, has no lease, and records the canonical serialized payload
        plus its SHA-256 hex digest. This method performs no HTTP or NATS I/O;
        nudges are post-commit orchestration. The returned intents carry the
        delivery id and the per-subscription URL / ownership fields the
        post-commit NATS path needs to publish a queued-delivery nudge without
        a second database round-trip.
        """
        ...

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
        """Atomically lease one due, live attempt for an HTTP send.

        A claim is allowed only for the requested writer revision, with no
        unexpired competing lease, no succeeded chain peer, and no live newer
        attempt. Expired leases are reclaimable. The returned timestamps are
        database-clock values and must be timezone-aware UTC values.
        """
        raise NotImplementedError("claim_delivery not implemented for this backend")

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
        """Atomically claim up to ``limit`` due attempts for crash recovery.

        Competing workers must not claim the same attempt. Backends with
        ``SKIP LOCKED`` should use it; single-writer backends may serialize the
        claim transaction. Ordering is oldest ``scheduled_at`` first.
        """
        raise NotImplementedError("claim_due_deliveries not implemented for this backend")

    async def guard_delivery_claim(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
    ) -> bool:
        """Fence a preclaimed attempt immediately before its HTTP POST.

        Return ``True`` only while the token still owns an unexpired live row
        whose chain has neither succeeded nor advanced. Otherwise converge or
        release the row as appropriate and return ``False``.
        """
        raise NotImplementedError("guard_delivery_claim not implemented for this backend")

    async def release_delivery_claim(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        lease_token: str,
    ) -> bool:
        """Release an owned lease when no HTTP POST began.

        This must not consume an attempt or schedule a successor. Return
        ``True`` only when the resulting row remains live and reclaimable.
        """
        raise NotImplementedError("release_delivery_claim not implemented for this backend")

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
        """Atomically persist an HTTP result and converge its retry chain.

        A 2xx success becomes the chain's sole terminal ``succeeded`` row and
        abandons free successors. A retryable failure abandons/supersedes the
        owned attempt and inserts at most one ``attempt_num + 1`` successor at
        the configured exponential-backoff time. Exhaustion or revocation ends
        at ``abandoned`` without a successor. Failure finalization requires an
        unexpired owned lease; success may commit after expiry when the fencing
        token still matches, preserving a received 2xx while still converging
        races. A stale or losing writer returns ``applied=False``.

        ``max_attempts`` must be positive. The schedule must contain at least
        ``max_attempts - 1`` positive delays; implementations validate these
        policy inputs before mutating the chain.
        """
        raise NotImplementedError("finalize_delivery not implemented for this backend")

    async def store_delivery_response_body(
        self,
        tx: Transaction,
        *,
        delivery_id: str,
        response_body: str,
    ) -> bool:
        """Attach bounded post-finalization response audit text.

        Body capture intentionally follows durable status finalization so a
        slow body cannot hold a lease. This audit-only update must never alter
        status, retry, superseded, or lease fields.
        """
        raise NotImplementedError("store_delivery_response_body not implemented for this backend")

    async def repair_delivery_chains(self, tx: Transaction) -> int:
        """Terminalize unleased live attempts made obsolete by chain peers.

        The idempotent sweep abandons/supersedes a live row when a newer attempt
        exists or any chain peer succeeded. It must not steal unexpired leases.
        Return the number of rows changed.
        """
        raise NotImplementedError("repair_delivery_chains not implemented for this backend")


class NatsDispatchLogRepository(ABC):
    """Idempotent NATS dispatch-dedupe log, backend-neutral (item 9/12).

    NATS delivers at-least-once. Both the v0.3 webhook outbox consumer
    (``mnemos/workers/webhooks_dispatch_nats_consumer.py``) and the v0.3
    federation memory upsert consumer
    (``mnemos/workers/federation_memory_nats_consumer.py``) record a
    ``(event_id, subject)`` row before applying side effects, so that a
    redelivery is acknowledged as a duplicate without a second side effect.

    The dedupe MUST be atomic against the side-effect write when both live in
    the same outer transaction (the federation consumer uses ``INSERT ...
    nats_dispatch_log`` then ``INSERT ... memories`` in one transaction).
    That is the contract of ``record_if_new`` -- it must perform the
    check-and-insert under the caller's transaction, so the side effect can
    rollback the dedupe row on failure.

    Table shape (all backends must implement this exactly -- see
    ``mnemos/db_migrations/migrations_v5_2_0_nats_outbox_idempotency.sql``
    and its SQLite / MySQL / MariaDB / Oracle / Db2 mirrors):

        event_id      TEXT NOT NULL,
        subject       TEXT NOT NULL,
        dispatched_at <TIMESTAMPTZ-or-equivalent> NOT NULL DEFAULT <now>,
        PRIMARY KEY (event_id, subject)

    The legacy Oracle/DB2 ``(id, subject, payload, published_at, acked_at)``
    shape is incompatible with the dedupe contract; item 9 retconned those
    tables to match the canonical Postgres/SQLite shape.
    """

    @abstractmethod
    async def record_if_new(
        self,
        tx: Transaction,
        event_id: str,
        subject: str,
    ) -> bool:
        """Atomically record ``(event_id, subject)`` and return ``True``.

        Return ``True`` when this call inserted a new dedupe row -- the
        caller is now responsible for applying the side effect.

        Return ``False`` when the ``(event_id, subject)`` pair already
        exists -- the caller MUST treat the delivery as a duplicate and
        skip the side effect.

        Implementations must:

        * run the check-and-insert under the caller's ``tx`` so the side
          effect and dedupe row commit/rollback together;
        * rely on the canonical ``(event_id, subject)`` unique constraint,
          not on a SELECT-then-INSERT pair, so concurrent redeliveries
          cannot both insert;
        * not raise on duplicate-row conflict; the semantic is a clean
          ``False`` return.
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

    async def mark_models_unavailable(self, tx: Transaction, provider: str, seen_model_ids: Sequence[str]) -> int:
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

    # ── model-registry PRICING (KNEMON Step 2: llm_provider_registry.json ingest) ─

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
        """Upsert price_in/price_out/price_cached/price_updated_at into model_registry.

        Returns (rows_updated, old_prices_dict_or_None). old_prices_dict is None
        when the pricing did not change or the model row was not found.
        """
        raise NotImplementedError("upsert_model_pricing not implemented for this backend")

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
        """Write a price_history row for audit trail.

        Called after upsert_model_pricing returns old_prices (prices changed).
        """
        raise NotImplementedError("write_price_history not implemented for this backend")


class OAuthRepository(ABC):
    """OAuth provider, identity, and browser-session persistence."""

    @abstractmethod
    async def mcp_get_signing_key(self, tx: Transaction) -> str | None: ...

    @abstractmethod
    async def mcp_save_signing_key(self, tx: Transaction, *, key_id: str, signing_key: str) -> None:
        """Insert a signing key if absent; concurrent first boots retain the first writer."""

    @abstractmethod
    async def mcp_save_client(self, tx: Transaction, row: Row) -> None: ...

    @abstractmethod
    async def mcp_get_client(self, tx: Transaction, client_id: str) -> Row | None: ...

    @abstractmethod
    async def mcp_save_code(self, tx: Transaction, row: Row) -> None: ...

    @abstractmethod
    async def mcp_consume_code(self, tx: Transaction, code: str) -> Row | None:
        """Atomically consume an unexpired, unused PKCE authorization code."""

    @abstractmethod
    async def mcp_save_token(self, tx: Transaction, row: Row) -> None: ...

    @abstractmethod
    async def mcp_rotate_refresh(
        self, tx: Transaction, token_hash: str, client_id: str, successor: Row
    ) -> str:
        """Return rotated/invalid/reused, serializing the entire refresh family.

        Reuse revokes all active descendants. Revocation and successor insertion
        share the caller's transaction, so insertion failure rolls back both.
        """

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

    @abstractmethod
    async def lookup_api_key(
        self, tx: Transaction, key_hash: str
    ) -> Row | None:
        """Resolve an API key to its user context (role/namespace/groups).

        Returns a backend-neutral Row carrying ``id``, ``user_id``,
        ``revoked``, ``role``, ``namespace``, and ``group_ids`` (list of
        group ids). Returns ``None`` when no key matches the hash or the
        key has been revoked. Implementations must NOT raise on missing
        rows — the auth path turns ``None`` into a 401.
        """

    @abstractmethod
    async def touch_api_key(self, tx: Transaction, key_id: Any) -> None:
        """Bump the ``last_used`` timestamp on the given api_keys row."""

    @abstractmethod
    async def resolve_active_session(
        self, tx: Transaction, session_id: str, *, now: Any
    ) -> Row | None:
        """Resolve an oauth_sessions row, validate it is still active.

        Returns the row carrying ``user_id`` and ``identity_id`` for
        valid, non-revoked, non-expired sessions, and updates
        ``last_used_at`` as a side effect (matching the previous
        asyncpg path's behaviour). Returns ``None`` for unknown,
        revoked, or expired sessions. ``now`` is the backend's
        "current timestamp" sentinel — implementations substitute their
        native ``NOW()`` / ``CURRENT_TIMESTAMP`` / sysdate literal so
        the same caller code works on every backend.
        """

    @abstractmethod
    async def gc_expired_sessions(
        self,
        tx: Transaction,
        *,
        now: Any,
        expired_grace: timedelta,
        revoked_grace: timedelta,
    ) -> int:
        """Garbage-collect stale ``oauth_sessions`` rows.

        Deletes rows whose ``expires_at`` is older than ``now - expired_grace``
        or whose ``revoked`` flag is set AND ``revoked_at`` is older than
        ``now - revoked_grace`` (NULL ``revoked_at`` on a revoked row counts as
        very old, so a row revoked without a timestamp is always eligible).
        Returns the count of rows deleted. ``now`` is the backend's "current
        timestamp" sentinel so implementations substitute their native
        ``NOW()`` / ``CURRENT_TIMESTAMP`` / ``SYSTIMESTAMP`` literal and the
        same caller code works on every backend.
        """


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

    @abstractmethod
    async def fetch_consultation_full(
        self,
        tx: Transaction,
        consultation_id: str,
        *,
        root: bool = False,
        user_id: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        """Assemble a verbatim classified view of one GRAEAE consultation.

        Owner-scoped: non-root callers only resolve their own consultations
        (``owner_id == user_id``); an invisible/unknown id returns ``None``.

        Reads the single ``graeae_consultations`` row plus every
        ``graeae_audit_log`` row for that consultation (ordered by
        ``sequence_num``) and materialises them into a structured dict:

            {
              "consultation_id": str,
              "source":   {prompt, context, task_type, mode, created},
              "quorum":   {consensus_score, winning_muse, cost, latency_ms,
                           model_variants, muses: [{provider, model,
                           quality_score, latency_ms}]},
              "synthesis": {text},
              "muses":    [{provider, model, response_text}],
            }

        Returns ``None`` when the consultation id is unknown. CLOB /
        large-text columns are materialised to ``str`` so callers can
        JSON-serialise the result without further unwrapping.
        """
        ...


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

    async def fetch_federated_memory_markers(
        self,
        tx: Transaction,
        local_ids: Sequence[str],
    ) -> dict[str, Row]:
        """Fetch page markers, with a compatibility fallback for backends.

        Backends with a native set-membership query should override this.
        """
        markers: dict[str, Row] = {}
        for local_id in local_ids:
            row = await self.fetch_federated_memory_marker(tx, local_id)
            if row is not None:
                markers[local_id] = row
        return markers

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


class AclRepository(ABC):
    """Per-principal memory ACL escape hatch — the ``memory_acl`` table.

    A grant widens *read* visibility on top of a memory's own UNIX mode
    bits: it lets a second group or a named user see a memory they would
    not otherwise reach. The read predicate (see
    ``mnemos.core.visibility`` / the per-backend renderers) honors these
    rows via an EXISTS disjunct on every multi-user backend. This
    repository is the *management* surface for those rows.

    ``principal`` is a typed string ``'user:<id>'`` or ``'group:<id>'``;
    ``perm`` is a Unix-style bitmask (read=4, write=2). Only multi-user
    backends advertise ``ACL_CAPABILITY`` — SQLite (single-user laptop
    tier) omits it and the route degrades to 503.

    Authorization (who may grant/revoke) is enforced at the route layer,
    not here: the SQL contract is principal-agnostic so callers stay
    responsible for the owner/root/group-admin gate.
    """

    @abstractmethod
    async def grant_acl(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        principal: str,
        perm: int,
        granted_by: str | None,
    ) -> Row:
        """Insert or update a grant. Upserts on (memory_id, principal).

        Returns the resulting row. Implementations MUST treat a repeat
        grant to the same principal as an idempotent update of ``perm``
        / ``granted_by`` (ON CONFLICT / MERGE), never a duplicate-key
        error.
        """
        ...

    @abstractmethod
    async def revoke_acl(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        principal: str,
    ) -> bool:
        """Delete a grant. Returns True if a row was removed, else False
        (so the route can 404 a revoke of a non-existent grant)."""
        ...

    @abstractmethod
    async def list_acl(self, tx: Transaction, memory_id: str) -> list[Row]:
        """Return all grants for ``memory_id`` ordered by principal.

        Rows carry ``principal``, ``perm``, ``granted_by``, and the
        creation timestamp. Returns ``[]`` when the memory has no
        grants (the common case)."""
        ...

    @abstractmethod
    async def is_group_admin(
        self,
        tx: Transaction,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """True if ``user_id`` is a delegated admin of ``group_id``.

        Backs the group-admin tier of the ACL-management authz gate:
        a non-owner who is ``is_admin`` of the memory's ``group_id`` may
        grant/revoke on memories in that group. Reads
        ``user_groups.is_admin`` for the (user, group) edge; returns
        ``False`` when no membership row exists."""
        ...


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

    @abstractmethod
    async def get_queue_stats(self, tx: Transaction) -> dict[str, int]:
        """Return the v3.5 stats snapshot the distillation worker logs.

        Returns a backend-neutral dict with exactly these keys
        (callers / log format depend on the exact names — don't drop
        or rename any):

        * ``total`` — total rows in ``memory_compression_queue``.
        * ``pending`` — rows whose ``status = 'pending'``.
        * ``running`` — rows whose ``status = 'running'``.
        * ``done`` — rows whose ``status = 'done'``.
        * ``failed`` — rows whose ``status = 'failed'``.
        * ``variants`` — total rows in ``memory_compressed_variants``.

        Implementations MUST compute the status counts and the variant
        count in a single round-trip pair (or one query that joins
        both counts) — the distillation worker calls this every
        ``CHECK_INTERVAL`` (30s) and the prior raw-SQL path issued
        two round-trips per call. Backends without ``FOR UPDATE`` /
        row-locking concerns (SQLite) may compute both counts in a
        single SELECT against two tables; backends that need a
        transaction wrapper for atomicity (Postgres, Oracle, MySQL)
        can issue two SELECTs inside the supplied ``tx``.
        """
        ...


class MorpheusRepository(ABC):
    """v3.3 MORPHEUS run-lifecycle CRUD — backend-agnostic.

    Item 11a of the 12-item ABC migration: the run-lifecycle functions
    (``begin_run`` / ``set_phase`` / ``update_counters`` /
    ``increment_extract_counters`` / ``finish_run`` / ``fail_run`` /
    ``sweep_orphan_runs`` / ``rollback_run``) used to live in
    ``mnemos/domain/morpheus/runner.py`` as raw ``asyncpg.Pool``-typed
    module-level functions. Every hive backend (Postgres, SQLite, MySQL,
    MariaDB, Oracle, Db2) was drifting on its ``morpheus_runs`` schema —
    Postgres's canonical shape (split across migrations_v3_3_morpheus.sql
    + namespace + consolidate + extract) had 19 columns; the other
    backends were stubs or early-iteration shapes (``run_type`` /
    ``metrics`` on Oracle/DB2; the SQLite stub even used ``status
    DEFAULT 'pending'`` which isn't a valid value in the Postgres
    CHECK constraint).

    This ABC moves every run-lifecycle SQL operation behind the
    persistence surface so the runner calls ``backend.morpheus.<method>
    (tx, ...)`` instead of raw ``pool.acquire()`` and the schema is
    canonical on every backend.

    Schema reference (canonical): ``db/migrations_v3_3_morpheus.sql``
    + ``migrations_v3_3_morpheus_namespace.sql`` +
    ``migrations_v4_2_morpheus_consolidate.sql`` +
    ``migrations_v4_2_morpheus_extract.sql`` (Postgres). The other
    backends were retconned in item 11a's
    ``0061c_morpheus_runs_parity.sql`` (Oracle/DB2) /
    ``migrations_v6_3_morpheus_runs_parity_sqlite.sql`` /
    ``0061c_morpheus_runs_parity_mysql.sql`` /
    ``0061c_morpheus_runs_parity_mariadb.sql`` files.

    Concurrency contract: ``sweep_orphan_runs`` and ``rollback_run``
    open the supplied ``tx`` and the SQL inside MUST keep the entire
    rollback in a single transaction — partial rollback (some
    memories deleted, some triples still pointing at the run) would
    leave the corpus inconsistent. The Postgres multi-CTE pattern is
    decomposed into portable sequential statements on backends that
    don't support writable CTEs feeding an UPDATE.

    JSON-operator contract: ``rollback_run`` touches ``memories.metadata``
    to delete the ``pre_consolidate_permission_mode`` key Postgres
    wrote during the CONSOLIDATE phase. Postgres uses JSONB operators
    (``metadata->>$1``, ``? $1``, ``COALESCE(metadata, '{}'::jsonb) - $1``);
    each backend translates these to its own JSON dialect
    (MySQL/MariaDB: ``JSON_EXTRACT`` / ``JSON_CONTAINS_PATH`` /
    ``JSON_REMOVE``; SQLite: ``json_extract`` + ``json_remove``;
    Oracle 23ai: ``JSON_VALUE`` / ``JSON_EXISTS`` +
    ``JSON_TRANSFORM``/read-modify-write; Db2: read-modify-write
    because Db2 has no native JSON update function in ORA-compat
    mode). Read-modify-write is acceptable on the read-modify-write
    fallbacks because ``rollback_run`` is an admin path, not a hot
    loop.
    """

    @abstractmethod
    async def begin_run(
        self,
        tx: Transaction,
        *,
        triggered_by: str,
        window_hours: int,
        cluster_min_size: int,
        config: dict | None,
        namespace: str | None,
    ) -> str:
        """Open a new ``morpheus_runs`` row and return its id as a string.

        Caller is responsible for advancing the row through phases via
        :meth:`set_phase` and finalising via :meth:`finish_run` (or
        :meth:`fail_run` on exception). The row is created with
        ``status='running'`` so an inspector polling ``/v1/morpheus/runs``
        sees the dream in flight. ``namespace`` set scopes the run to
        memories with that ``namespace`` value; NULL means "all namespaces"
        (the default).
        """
        ...

    @abstractmethod
    async def set_phase(self, tx: Transaction, run_id: str, phase: str) -> None:
        """Stamp ``morpheus_runs.phase`` with the current phase name."""
        ...

    @abstractmethod
    async def update_counters(
        self,
        tx: Transaction,
        run_id: str,
        *,
        memories_scanned: int | None = None,
        clusters_found: int | None = None,
        summaries_created: int | None = None,
        memories_consolidated: int | None = None,
        clusters_consolidated: int | None = None,
        triples_extracted: int | None = None,
        memories_processed_for_extraction: int | None = None,
    ) -> None:
        """Bump a subset of counters on ``morpheus_runs``.

        Only the kwargs explicitly passed are written — the partial-update
        semantic matches the original ``runner.update_counters`` shape so
        phase functions can update one counter at a time without
        overwriting the rest.
        """
        ...

    @abstractmethod
    async def increment_extract_counters(
        self,
        tx: Transaction,
        run_id: str,
        *,
        triples_extracted: int,
        memories_processed: int,
    ) -> None:
        """Increment the extract counters by the per-memory deltas.

        Used by the EXTRACT phase as each source memory commits — the
        counter accumulates rather than replacing, matching the prior
        runner-side ``COALESCE(..., 0) + $n`` pattern.
        """
        ...

    @abstractmethod
    async def finish_run(self, tx: Transaction, run_id: str) -> None:
        """Mark the run ``status='success'`` and stamp ``finished_at``."""
        ...

    @abstractmethod
    async def fail_run(self, tx: Transaction, run_id: str, error: str) -> None:
        """Mark the run ``status='failed'`` and stamp ``finished_at + error``."""
        ...

    @abstractmethod
    async def sweep_orphan_runs(
        self,
        tx: Transaction,
        *,
        threshold_hours: float,
    ) -> list[Row]:
        """Fail ``morpheus_runs`` rows stranded in ``status='running'``.

        Marks every ``running`` row whose ``started_at`` is older than
        ``threshold_hours`` ago as ``failed`` with a synthetic
        ``orphan_timeout_sweep`` error. Returns the list of reclaimed
        rows with ``id`` and ``started_at`` so callers can log each
        reclaimed run's timestamp. Mirrors the compression worker
        contest stale-running sweep — best-effort reclaim path for
        workers/API triggers that crashed after opening a run row but
        before any terminal status.
        """
        ...

    @abstractmethod
    async def rollback_run(
        self,
        tx: Transaction,
        run_id: str,
        *,
        requested_by: str,
    ) -> tuple[int, int]:
        """Undo every memory mutation tagged with this run.

        Returns ``(memories_deleted, run_rows_updated)``. Synthesised
        rows (those with ``provenance='morpheus_local'``) are deleted;
        consolidated originals are restored in place from the metadata
        audit key (``pre_consolidate_permission_mode``) the CONSOLIDATE
        phase wrote; KG triples tagged with the run are removed; the
        ``memories.triples_extracted_at`` mark is cleared on affected
        memories; and the ``morpheus_runs`` row is flipped to
        ``status='rolled_back'``. The full sequence runs inside the
        supplied ``tx`` so a partial rollback cannot leak.

        ``memories.metadata`` is touched to delete the
        ``pre_consolidate_permission_mode`` key — each backend
        translates the Postgres JSONB operators to its own dialect
        (MySQL ``JSON_REMOVE``, SQLite ``json_remove``, Oracle
        ``JSON_TRANSFORM``/read-modify-write, Db2 read-modify-write).
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
    "acl",
]


CORE_CAPABILITY: CapabilityName = "core"
OAUTH_CAPABILITY: CapabilityName = "oauth"
SESSIONS_CAPABILITY: CapabilityName = "sessions"
CONSULTATIONS_CAPABILITY: CapabilityName = "consultations"
FEDERATION_CAPABILITY: CapabilityName = "federation"
AUDIT_CAPABILITY: CapabilityName = "audit"
STATE_CAPABILITY: CapabilityName = "state"
ACL_CAPABILITY: CapabilityName = "acl"
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
        COMPRESSION_QUEUE_CAPABILITY,
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
        KG_CAPABILITY,
        VERSIONS_CAPABILITY,
        BRANCHES_CAPABILITY,
        COMPRESSION_CAPABILITY,
        FEDERATION_DETAIL_CAPABILITY,
        STATE_DETAIL_CAPABILITY,
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

    async def insert_pantheon_routing_audit(
        self,
        tx: Transaction,
        record: Mapping[str, Any],
    ) -> None:
        """Insert one PANTHEON routing audit row using this backend's SQL dialect."""
        raise NotImplementedError("pantheon routing audit is not implemented")

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
    def morpheus(self) -> MorpheusRepository:
        """v3.3 MORPHEUS run-lifecycle CRUD repository.

        Item 11a of the 12-item ABC migration: the runner now routes
        every ``begin_run`` / ``set_phase`` / ``update_counters`` /
        ``increment_extract_counters`` / ``finish_run`` / ``fail_run``
        / ``sweep_orphan_runs`` / ``rollback_run`` call through this
        property so the same caller code works on Postgres, SQLite,
        MySQL/MariaDB, Oracle, and Db2.

        Default :class:`NotImplementedError` so a backend that hasn't
        shipped this ABC yet fails loudly at first call rather than
        silently pretending to support MORPHEUS — concrete backends
        override this property when the implementation lands.
        """
        raise NotImplementedError("morpheus run-lifecycle repository is not implemented")

    @property
    def webhooks(self) -> WebhookRepository: ...

    @property
    def nats_dispatch_log(self) -> NatsDispatchLogRepository: ...

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


@runtime_checkable
class AclPersistence(PersistenceCapabilityBase, Protocol):
    """Per-principal memory ACL grant/revoke/list persistence.

    Advertised only by multi-user backends (Postgres/Oracle/Db2);
    single-user SQLite omits ``ACL_CAPABILITY`` so the management
    routes degrade to 503 there.
    """

    _supports_acl_persistence: Literal[True]

    @property
    def acl(self) -> AclRepository: ...


PersistenceBackend: TypeAlias = Union[
    CorePersistence,
    OAuthPersistence,
    SessionsPersistence,
    ConsultationsPersistence,
    FederationPersistence,
    AuditPersistence,
    StatePersistence,
    AclPersistence,
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
        out = set(details)
        # ``webhooks`` means end-to-end delivery, not merely that the
        # backend can append an outbox row.  Until a backend has a compatible
        # claim/send/finalize worker it must not advertise this capability:
        # doing so makes health/doctor report a feature whose rows will never
        # leave the database.
        if not getattr(backend, "supports_webhooks", False):
            out.discard(WEBHOOKS_CAPABILITY)
        return out

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
                COMPRESSION_QUEUE_CAPABILITY,
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
    if not getattr(backend, "supports_webhooks", False):
        out.discard(WEBHOOKS_CAPABILITY)
    return out


def require_capability(backend: object, capability: str) -> None:
    if not has_capability(backend, capability):
        raise BackendCapabilityMissing(capability, type(backend).__name__)


@asynccontextmanager
async def null_transaction(tx: Transaction) -> AsyncIterator[Transaction]:
    """Yield an existing transaction without managing its lifecycle."""
    yield tx
