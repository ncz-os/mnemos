"""Characterisation tests for ``mnemos.domain.memory_dedup``.

The module was shipped with no direct test references at all; these tests
pin its *current* behaviour (normal, edge and error paths) against fakes only
— no network, no live database, no NATS broker. Every assertion records what
the code does today, not what it arguably ought to do.

Surface under test
------------------
Public (``__all__``):

* ``backfill_content_hashes(backend, *, batch_size=500, apply=False)``
* ``dedup_exact_content(backend, *, namespace=None, apply=False)``

Internal helpers exercised directly (they carry the row-shape branching):

* ``DedupGroup.as_dict`` (frozen dataclass; aliases + count fields)
* ``_row_get`` (mapping vs. sequence vs. object-with-``get`` vs. ``None``)
* ``_memory_ids_from_row`` (``\\x1f``-joined string, list, ``None``)
* ``_best_memory_id`` (``keep_id`` > ``canonical_id`` > first id > ``None``)
* ``_group_from_row`` (keeper/duplicate derivation, ``None`` guards)
* ``_root_mutation_visibility`` (ROOT_BYPASS filter with the group namespace)
* ``_dispatch_delete_side_effects`` (webhook payload, NATS subject)

Known defects pinned as ``xfail(strict=True)``
----------------------------------------------
1. ``_memory_ids_from_row`` drops falsy-but-present ids: splitting
   ``"a\\x1f0\\x1fb"`` filters ``if part``, so the string ``"0"`` never
   reaches the group. Likewise ``_row_get`` maps ``0``/``""``/``[]`` to the
   default, so a keeper id of ``0`` falls through to the next candidate.
2. ``delivery_ids`` is built with ``str(item)`` over the webhook intents.
   ``WebhookDeliveryIntent`` has no ``__str__``, so the payload stringifies
   via ``repr`` — an opaque ``WebhookDeliveryIntent(delivery_id='d1',
   ..., namespace=<MagicMock ...>)`` blob instead of the delivery id.

Behaviour that looks odd but is pinned as ordinary (documented) behaviour
because it is not clearly a defect here: the dry-run ``null_count`` comes
straight from the repository (it may be a count of eligible rows rather than
NULL rows); ``_row_get`` maps falsy values to the default; the NATS subject
namespace substitution only rewrites ``.`` to ``_``; ``backfill``'s apply
loop sums ``changed or 0`` across batches so a repository that keeps
returning ``batch_size`` never terminates.

Author: Jason Perlow <jperlow@gmail.com>
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.core.secret_detection import (
    SECRET_CLEAN_MARKER_KEY,
    clean_marker_fields,
    redact_field_with_stored,
)
from mnemos.domain.memory_dedup import (
    DedupGroup,
    _best_memory_id,
    _dispatch_delete_side_effects,
    _group_from_row,
    _memory_ids_from_row,
    _root_mutation_visibility,
    _row_get,
    backfill_content_hashes,
    dedup_exact_content,
)
from mnemos.nats.client import get_node_name
from mnemos.persistence.base import WebhookDeliveryIntent
from mnemos.persistence.visibility import VisibilityScope

# ── Fakes ──────────────────────────────────────────────────────────────────
#
# ``FakeBackend.transactional()`` yields a ``SimpleNamespace`` whose
# ``type(tx).__name__`` is not a known dialect, so ``transaction_dialect``
# raises ``TypeError`` and ``supports_journal(tx)`` is False. That keeps the
# audit-chain refetch branch off unless a test opts in explicitly (see the
# audit test below). Nothing here touches the network or a real database.


class SequenceRepo:
    """Answers one repository method from a scripted sequence of returns.

    A method with NO scripted values falls back to ``default`` — unlike the
    shared ``_FakeMemoryRepo`` (whose catch-all ``__getattr__`` would turn
    ``soft_delete_memory`` into an ``AsyncMock`` returning a Mock that the
    module would happily count as a deleted row). With ``raises=True``, a
    scripted ``BaseException`` is raised when its turn comes (error paths).
    """

    def __init__(self, *returns: Any, default: Any = None, raises: bool = False) -> None:
        self._returns = list(returns)
        self._default = default
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self._returns:
            return self._default
        result = self._returns.pop(0)
        if self._raises and isinstance(result, BaseException):
            raise result
        return result


class MemoryRepo:
    """``backend.memories`` stub covering the three methods used.

    Unconfigured methods return their ``default`` (None / [] / a deleted
    row) rather than an ``AsyncMock`` — a Mock return would otherwise be
    counted as a real deleted row by ``dedup_exact_content``. When
    ``soft_delete`` is not scripted, each soft-delete returns THE GROUP ROW
    it was asked about (mimicking the real repository, which returns the
    mutated row) so webhook/NATS payloads are populated from it.
    """

    def __init__(self, *, find_groups=None, backfill=None, soft_delete=None) -> None:
        self.find_duplicate_content_groups = SequenceRepo(
            *(() if find_groups is None else find_groups), default=[], raises=True
        )
        self._group_rows = list(() if find_groups is None else find_groups)
        self.backfill_missing_content_hashes = SequenceRepo(
            *(() if backfill is None else backfill), default=0, raises=True
        )
        self._soft_delete_seq = None if soft_delete is None else SequenceRepo(*soft_delete, raises=True)
        self._soft_delete_default = (
            {"id": None, "content": "duplicate", "category": "facts", "metadata": {}}
            if soft_delete is not None
            else None
        )
        self.soft_delete_calls: list[dict[str, Any]] = []

    async def soft_delete_memory(self, tx, memory_id, **kwargs):
        self.soft_delete_calls.append({"memory_id": memory_id, **kwargs})
        if self._soft_delete_seq is not None:
            return await self._soft_delete_seq(tx, memory_id, **kwargs)
        # Default: the group row the caller is deduplicating, as the real
        # repository would return after mutating it (the repository call is
        # ``find_duplicate_content_groups(tx, namespace=...)``, so the rows
        # are not in the recorded args — only tx and the kwarg are).
        for rows in self._group_rows:
            for group_row in rows:
                if group_row.get("keep_id") != memory_id and memory_id in (group_row.get("memory_ids") or []):
                    return dict(group_row)
        return self._soft_delete_default


class Webhooks:
    """``backend.webhooks.dispatch_event`` stub."""

    def __init__(self, delivery_ids=()) -> None:
        self.delivery_ids = list(delivery_ids)
        self.calls: list[dict[str, Any]] = []

    async def dispatch_event(self, tx, event_type, payload, **kwargs):
        record = {"event_type": event_type, "payload": dict(payload)}
        record.update(kwargs)
        self.calls.append(record)
        from mnemos.persistence.base import WebhookDeliveryIntent

        return [
            WebhookDeliveryIntent(
                delivery_id=delivery_id,
                subscription_id="sub-fake",
                url="https://example.invalid/hook",
                namespace=kwargs.get("namespace"),
                owner_id=kwargs.get("owner_id"),
            )
            for delivery_id in self.delivery_ids
        ]


class RecordingAuditChain:
    """Captures ``get_latest_audit_entry`` / ``insert_audit_entry`` calls."""

    def __init__(self, *, latest=None) -> None:
        self.latest = latest
        self.latest_calls: list[Any] = []
        self.inserted: list[dict[str, Any]] = []

    async def get_latest_audit_entry(self, tx, memory_id_bytes):
        self.latest_calls.append(memory_id_bytes)
        return self.latest

    async def insert_audit_entry(self, tx, **kwargs):
        self.inserted.append(kwargs)
        return kwargs["entry_id"]


class Backend:
    """``PersistenceBackend``-shaped fake: transactional() + memories + webhooks."""

    supports_webhooks = True
    audit_chain: Any = None  # write_audit_entry no-ops when None (best effort)

    def __init__(self, *, find_groups=None, backfill=None, soft_delete=None, webhooks=None) -> None:
        self.memories = MemoryRepo(find_groups=find_groups, backfill=backfill, soft_delete=soft_delete)
        self.webhooks = webhooks if webhooks is not None else Webhooks()
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


# ── Row fixtures ───────────────────────────────────────────────────────────

GROUP_ROW = {
    "owner_id": "alice",
    "namespace": "lab",
    "content_hash": "a" * 64,
    "memory_ids": ["mem_keep", "mem_dup"],
    "keep_id": "mem_keep",
}


# ── DedupGroup ─────────────────────────────────────────────────────────────


def test_dedup_group_is_frozen():
    group = DedupGroup(
        owner_id="alice",
        namespace="lab",
        content_hash="h",
        keep_id="m1",
        duplicate_ids=["m2"],
        memory_ids=["m1", "m2"],
    )
    with pytest.raises(Exception) as excinfo:  # frozen dataclass: no attribute assignment
        group.keep_id = "other"
    assert type(excinfo.value).__name__ in {"FrozenInstanceError", "AttributeError"}


def test_dedup_group_as_dict_aliases_and_counts():
    group = DedupGroup(
        owner_id="alice",
        namespace="lab",
        content_hash="h",
        keep_id="m1",
        duplicate_ids=["m2", "m3"],
        memory_ids=["m1", "m2", "m3"],
    )
    payload = group.as_dict()
    assert payload["keep_id"] == "m1"
    assert payload["canonical_id"] == payload["keep_id"]
    # duplicate_count counts the WHOLE group, not just the losers.
    assert payload["duplicate_count"] == 3
    assert payload == {
        "owner_id": "alice",
        "namespace": "lab",
        "content_hash": "h",
        "keep_id": "m1",
        "canonical_id": "m1",
        "duplicate_ids": ["m2", "m3"],
        "memory_ids": ["m1", "m2", "m3"],
        "duplicate_count": 3,
    }


# ── _row_get ───────────────────────────────────────────────────────────────


def test_row_get_reads_mapping_keys():
    row = {"a": 1, "b": None}
    assert _row_get(row, "a") == 1
    assert _row_get(row, "b") is None  # None value falls back to the default
    assert _row_get(row, "missing") is None
    assert _row_get(row, "missing", default=42) == 42


def test_row_get_falls_back_from_sequence_indexing_to_get():
    # Sequence: row["memory_id"] raises IndexError, then row.get(key, default)
    # is consulted; a plain list has no .get, so the AttributeError path returns
    # the default rather than raising.
    assert _row_get(["x", "y"], "memory_id", default="fallback") == "fallback"

    # Object with .get() only: __getitem__ raises TypeError first.
    class OnlyGet:
        def get(self, key, default=None):
            return {"k": "v"}.get(key, default)

    assert _row_get(OnlyGet(), "k") == "v"
    assert _row_get(OnlyGet(), "nope", default="d") == "d"


def test_row_get_maps_falsy_values_to_the_default():
    # Documented quirk: a present-but-None value (and any value that is
    # None after coercion) is replaced by the default; genuinely falsy
    # values (0, "", [], False) pass through untouched.
    assert _row_get({"z": 0}, "z", default="d") == 0
    assert _row_get({"z": ""}, "z", default="d") == ""
    assert _row_get({"z": []}, "z", default="d") == []
    assert _row_get({"z": False}, "z", default="d") is False
    assert _row_get({"z": None}, "z", default="d") == "d"


# ── _memory_ids_from_row ───────────────────────────────────────────────────


def test_memory_ids_from_unit_separator_string_is_always_empty():
    # Pinned quirk: _row_get replaces an empty-but-present string with the
    # default ([]), so _memory_ids_from_row NEVER sees a string from a real
    # row — a \\x1f-joined "memory_ids" column always yields [] here.
    assert _memory_ids_from_row("m1\x1fm2\x1fm3") == []
    assert _memory_ids_from_row("\x1fm1\x1f\x1fm2\x1f") == []
    assert _memory_ids_from_row("") == []


def test_memory_ids_from_sequence_and_none():
    # Quirk: a LIST never reaches the str branch — _row_get's sequence
    # path raises IndexError, then list.get raises AttributeError, so the
    # default ([]) comes back. Only a str-typed value can be split.
    assert _memory_ids_from_row(["a", "b"]) == []
    assert _memory_ids_from_row([1, 2]) == []
    # Absent-key / None / {} defaults all become [].
    assert _memory_ids_from_row({}) == []
    assert _memory_ids_from_row(None) == []


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: _memory_ids_from_row filters falsy parts, so a present id "
    "'0' is dropped from \\x1f-joined memory_ids",
)
def test_memory_ids_drops_falsy_present_id():
    """DEFECT pinned: a present-but-falsy id (e.g. the string ``"0"``) is
    silently dropped when the row carries ``memory_ids`` as a
    \\x1f-joined string, because the split filters ``if part``. The id then
    never appears in the group's ``memory_ids``/``duplicate_ids``.

    xfail(strict=True): remove this mark when the filter preserves
    non-empty parts (or when ``memory_ids`` stops being a string).
    """
    assert _memory_ids_from_row("a\x1f0\x1fb") == ["a", "0", "b"]


# ── _best_memory_id ────────────────────────────────────────────────────────


def test_best_memory_id_prefers_keep_id_then_canonical_id_then_first_id():
    assert _best_memory_id({"keep_id": "k", "canonical_id": "c"}, ["a", "b"]) == "k"
    assert _best_memory_id({"canonical_id": "c"}, ["a", "b"]) == "c"
    assert _best_memory_id({}, ["a", "b"]) == "a"
    assert _best_memory_id({"keep_id": None, "canonical_id": None}, ["a"]) == "a"


def test_best_memory_id_returns_none_without_candidates():
    assert _best_memory_id({}, []) is None
    assert _best_memory_id({"keep_id": ""}, []) is None


# ── _group_from_row ────────────────────────────────────────────────────────


def test_group_from_row_normal():
    group = _group_from_row(dict(GROUP_ROW))
    assert group is not None
    assert group.owner_id == "alice"
    assert group.namespace == "lab"
    assert group.content_hash == "a" * 64
    assert group.keep_id == "mem_keep"
    assert group.duplicate_ids == ["mem_dup"]
    assert group.memory_ids == ["mem_keep", "mem_dup"]


def test_group_from_row_uses_canonical_id_alias_when_keep_id_absent():
    group = _group_from_row(
        {"owner_id": "o", "namespace": "n", "content_hash": "h", "memory_ids": ["a", "b"], "canonical_id": "b"}
    )
    assert group is not None
    assert group.keep_id == "b"
    assert group.duplicate_ids == ["a"]  # keeper excluded, order preserved


def test_group_from_row_without_explicit_keeper_keeps_the_first_id():
    group = _group_from_row({"memory_ids": ["first", "second"]})
    assert group is not None
    assert group.keep_id == "first"
    assert group.duplicate_ids == ["second"]
    # Missing owner/namespace/content_hash become "".
    assert group.owner_id == ""
    assert group.namespace == ""
    assert group.content_hash == ""


def test_group_from_row_returns_none_without_keeper_or_duplicates():
    # No keeper (no ids at all).
    assert _group_from_row({"owner_id": "o"}) is None
    # Single id: the keeper is not its own duplicate.
    assert _group_from_row({"memory_ids": ["only"]}) is None
    # All ids equal the keeper: nothing left to delete.
    assert _group_from_row({"memory_ids": ["same", "same"], "keep_id": "same"}) is None


def test_group_from_row_accepts_unit_separator_string_memory_ids():
    group = _group_from_row({"memory_ids": "keep\x1fdup1\x1fdup2", "keep_id": "keep"})
    assert group is not None
    assert group.memory_ids == ["keep", "dup1", "dup2"]
    assert group.duplicate_ids == ["dup1", "dup2"]


# ── _root_mutation_visibility ──────────────────────────────────────────────


def test_root_mutation_visibility_is_root_bypass_scoped_to_namespace():
    vis = _root_mutation_visibility("lab")
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.user_id is None
    assert vis.group_ids == ()
    assert vis.namespace == "lab"

    # Default namespace is None (not "default") when not supplied.
    assert _root_mutation_visibility().namespace is None


# ── backfill_content_hashes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_dry_run_reports_repository_count_once():
    backend = Backend(backfill=[8333])
    payload = await backfill_content_hashes(backend)
    assert payload == {"apply": False, "batch_size": 500, "null_count": 8333, "updated_count": 0}
    # Exactly one repository call: dry-run stops after the first probe.
    assert len(backend.memories.backfill_missing_content_hashes.calls) == 1
    call = backend.memories.backfill_missing_content_hashes.calls[0]
    assert call["kwargs"] == {"batch_size": 500, "apply": False}
    assert backend.commits == 1
    assert backend.rollbacks == 0


@pytest.mark.asyncio
async def test_backfill_dry_run_forwards_batch_size_and_none_count():
    backend = Backend(backfill=[None])
    payload = await backfill_content_hashes(backend, batch_size=7)
    # None from the repository flows through as null_count (no coercion).
    assert payload == {"apply": False, "batch_size": 7, "null_count": None, "updated_count": 0}


@pytest.mark.asyncio
async def test_backfill_apply_loops_until_short_batch():
    # Batch 1: exactly batch_size -> keep looping. Batch 2: short -> stop.
    backend = Backend(backfill=[2, 1])
    payload = await backfill_content_hashes(backend, batch_size=2, apply=True)
    assert payload == {"apply": True, "batch_size": 2, "updated_count": 3}
    calls = backend.memories.backfill_missing_content_hashes.calls
    assert len(calls) == 2
    assert all(call["kwargs"] == {"batch_size": 2, "apply": True} for call in calls)
    assert backend.commits == 1


@pytest.mark.asyncio
async def test_backfill_apply_sums_none_and_partial_batches():
    # First call reports None (coerced to 0, still < batch_size -> stop).
    backend = Backend(backfill=[None])
    payload = await backfill_content_hashes(backend, batch_size=3, apply=True)
    assert payload["updated_count"] == 0
    assert len(backend.memories.backfill_missing_content_hashes.calls) == 1


@pytest.mark.asyncio
async def test_backfill_apply_partial_first_batch_stops_immediately():
    backend = Backend(backfill=[1])
    payload = await backfill_content_hashes(backend, batch_size=3, apply=True)
    assert payload == {"apply": True, "batch_size": 3, "updated_count": 1}


@pytest.mark.asyncio
async def test_backfill_rejects_non_positive_batch_size_before_transaction():
    backend = Backend(backfill=[1])
    for batch_size in (0, -1):
        with pytest.raises(ValueError, match="batch_size must be positive"):
            await backfill_content_hashes(backend, batch_size=batch_size)
    # Rejected before the transaction opens: no commit, no repository call.
    assert backend.commits == 0
    assert backend.memories.backfill_missing_content_hashes.calls == []


# ── dedup_exact_content: discovery / dry-run ───────────────────────────────


@pytest.mark.asyncio
async def test_dedup_dry_run_counts_groups_and_dry_run_stops():
    backend = Backend(find_groups=[[dict(GROUP_ROW)]])
    payload = await dedup_exact_content(backend, namespace="lab")
    assert payload["apply"] is False
    assert payload["namespace"] == "lab"
    assert payload["group_count"] == 1
    assert payload["duplicate_count"] == 1
    assert payload["deleted_count"] == 0
    assert payload["delivery_ids"] == []
    assert payload["nats_intents"] == []
    assert payload["groups"] == [
        {
            "owner_id": "alice",
            "namespace": "lab",
            "content_hash": "a" * 64,
            "keep_id": "mem_keep",
            "canonical_id": "mem_keep",
            "duplicate_ids": ["mem_dup"],
            "memory_ids": ["mem_keep", "mem_dup"],
            "duplicate_count": 2,
        }
    ]
    # Dry-run: nothing deleted, nothing dispatched.
    assert backend.memories.soft_delete_calls == []
    assert backend.webhooks.calls == []
    # The namespace filter reaches the repository.
    assert backend.memories.find_duplicate_content_groups.calls[0]["kwargs"] == {"namespace": "lab"}
    assert backend.commits == 1


@pytest.mark.asyncio
async def test_dedup_dry_run_with_no_groups():
    backend = Backend(find_groups=[[]])
    payload = await dedup_exact_content(backend)
    assert payload["namespace"] is None
    assert payload["groups"] == []
    assert payload["group_count"] == 0
    assert payload["duplicate_count"] == 0
    assert payload["deleted_count"] == 0


@pytest.mark.asyncio
async def test_dedup_skips_rows_that_do_not_form_a_group():
    # One keeper-only row and one keeper-equal row: both are filtered out.
    backend = Backend(
        find_groups=[
            [
                {"owner_id": "o", "namespace": "n", "content_hash": "h", "memory_ids": ["only"]},
                {"memory_ids": ["same", "same"], "keep_id": "same"},
                {"owner_id": "o", "namespace": "n", "content_hash": "h", "memory_ids": ["k", "d"], "keep_id": "k"},
            ]
        ]
    )
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["group_count"] == 1
    assert payload["groups"][0]["keep_id"] == "k"
    assert payload["deleted_count"] == 1


# ── dedup_exact_content: apply ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_apply_soft_deletes_each_loser_with_dedup_metadata():
    backend = Backend(find_groups=[[dict(GROUP_ROW)]])
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1
    assert len(backend.memories.soft_delete_calls) == 1
    call = backend.memories.soft_delete_calls[0]
    assert call["memory_id"] == "mem_dup"
    assert call["requested_by"] == "mnemos-memory-dedup"
    assert call["requested_at"] is None
    assert call["request_kind"] == "dedup_exact_content"
    assert call["reason"] == "exact-content duplicate soft-delete"
    assert call["source"] == ["mnemos.cli.memory-dedup"]
    visibility = call["visibility"]
    assert visibility.scope == VisibilityScope.ROOT_BYPASS
    assert visibility.namespace == "lab"  # scoped to the GROUP namespace
    # Keeper untouched: only losers are soft-deleted.
    assert [c["memory_id"] for c in backend.memories.soft_delete_calls] == ["mem_dup"]
    assert backend.commits == 1


@pytest.mark.asyncio
async def test_dedup_apply_skips_side_effects_when_soft_delete_returns_falsy():
    row = {
        "owner_id": "alice",
        "namespace": "lab",
        "content_hash": "a" * 64,
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    # A scripted None (repository says "already gone") is never counted.
    backend = Backend(find_groups=[[row]], soft_delete=[None])
    payload = await dedup_exact_content(backend, apply=True)
    assert len(backend.memories.soft_delete_calls) == 1
    assert payload["deleted_count"] == 0
    assert payload["delivery_ids"] == []
    assert payload["nats_intents"] == []
    assert payload["group_count"] == 1  # the group itself is still reported
    assert backend.webhooks.calls == []
    assert backend.commits == 1  # not a rollback: a falsy delete is not an error


@pytest.mark.asyncio
async def test_dedup_apply_dispatches_webhooks_and_nats_per_deleted_row():
    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "content_hash": "a" * 64,
        "category": "facts",
        "subcategory": None,
        "content": "call me at 555-123-4567 now",
        "metadata": {},
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]], webhooks=Webhooks(delivery_ids=["d1", "d2"]))
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1

    # Webhook: one dispatch per deleted row, event + payload pinned.
    # The dispatch kwargs mirror the SOFT-DELETE row the repository returned
    # (here: the group row, as the real repository returns the mutated row).
    assert len(backend.webhooks.calls) == 1
    dispatch = backend.webhooks.calls[0]
    assert dispatch["event_type"] == "memory.deleted"
    assert dispatch["owner_id"] == "alice"
    assert dispatch["namespace"] == "lab"
    assert dispatch["payload"] == {
        "memory_id": "mem_dup",
        "category": "facts",
        "subcategory": None,
        "content": "call me at 555-123-4567 now",
        "owner_id": "alice",
        "namespace": "lab",
    }

    # NATS: subject keeps dots; payload carries the node name; id key is the
    # ``<memory_id>.deleted`` correlation key.
    assert len(payload["nats_intents"]) == 1
    subject, nats_payload, intent_key = payload["nats_intents"][0]
    assert subject == "mnemos.memory.deleted.lab"
    assert nats_payload["memory_id"] == "mem_dup"
    assert nats_payload["namespace"] == "lab"
    assert nats_payload["category"] == "facts"
    assert nats_payload["source_node"] == get_node_name()
    assert intent_key == "mem_dup.deleted"
    assert backend.commits == 1


@pytest.mark.asyncio
async def test_dedup_apply_webhook_payload_redacts_secret_content_via_stored_spans():
    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "category": "facts",
        "subcategory": None,
        "content": "call me at 555-123-4567 now",
        "metadata": {"secret_redact_fields": {"content": [[12, 25]]}},
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]], webhooks=Webhooks(delivery_ids=["d1"]))
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1
    # F3: the outbound webhook content is span-redacted; the raw secret
    # must not leave the server. Pin the exact redacted form against the
    # same production helper the module calls.
    assert backend.webhooks.calls[0]["payload"]["content"] == "call me at 5[REDACTED]ow"
    assert "555-123-4567" not in backend.webhooks.calls[0]["payload"]["content"]
    assert backend.webhooks.calls[0]["payload"]["content"] == redact_field_with_stored(
        "call me at 555-123-4567 now", {"secret_redact_fields": {"content": [[12, 25]]}}, "content"
    )


@pytest.mark.asyncio
async def test_dedup_apply_webhook_payload_untouched_when_content_is_clean_marked():
    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "category": "facts",
        "subcategory": None,
        "content": "plain text with no secret",
        "metadata": {SECRET_CLEAN_MARKER_KEY: clean_marker_fields(["content"])},
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]], webhooks=Webhooks())
    await dedup_exact_content(backend, apply=True)
    # Clean marker present under the current classifier: content passes through.
    assert backend.webhooks.calls[0]["payload"]["content"] == "plain text with no secret"


@pytest.mark.asyncio
async def test_dedup_apply_without_webhook_support_skips_dispatch_but_keeps_nats():
    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "category": "facts",
        "content": "hello",
        "metadata": {},
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]], webhooks=Webhooks(delivery_ids=["d1"]))
    backend.supports_webhooks = False
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1
    assert backend.webhooks.calls == []  # no dispatch without support
    assert len(payload["nats_intents"]) == 1  # NATS intents are unconditional


@pytest.mark.asyncio
async def test_dedup_nats_subject_replaces_dots_in_namespace():
    # The row the module receives carries a namespace (the GROUP namespace
    # drives the visibility filter); the NATS subject is derived from the
    # soft-delete row returned by the repository, which the fake here
    # supplies via the delete result.
    group_row = {
        "id": "m2",
        "namespace": "corp.lab",
        "memory_ids": ["m1", "m2"],
        "keep_id": "m1",
    }
    deleted_row = {"id": "m2", "namespace": "corp.lab", "category": "facts"}
    backend = Backend(find_groups=[[group_row]], soft_delete=[deleted_row])
    payload = await dedup_exact_content(backend, apply=True)
    subject, nats_payload, _key = payload["nats_intents"][0]
    # Only dots are replaced; the original (dotted) namespace is kept in the payload.
    assert subject == "mnemos.memory.deleted.corp_lab"
    assert nats_payload["namespace"] == "corp.lab"


@pytest.mark.asyncio
async def test_dedup_nats_subject_defaults_to_default_namespace():
    # No namespace anywhere: subject falls back to ``default`` while the
    # payload keeps the raw (None) namespace.
    group_row = {"id": "m2", "namespace": None, "memory_ids": ["m1", "m2"], "keep_id": "m1"}
    backend = Backend(find_groups=[[group_row]])
    payload = await dedup_exact_content(backend, apply=True)
    subject, nats_payload, _key = payload["nats_intents"][0]
    assert subject == "mnemos.memory.deleted.default"
    assert nats_payload["namespace"] is None  # payload keeps the raw (None) namespace


@pytest.mark.asyncio
async def test_dedup_apply_repository_error_propagates_and_rolls_back():
    backend = Backend()
    backend.memories.find_duplicate_content_groups._returns.append(RuntimeError("repo exploded"))
    with pytest.raises(RuntimeError, match="repo exploded"):
        await dedup_exact_content(backend, apply=True)
    # The transactional() block exited via exception: rollback, not commit.
    assert backend.rollbacks == 1
    assert backend.commits == 0
    assert backend.memories.soft_delete_calls == []


@pytest.mark.asyncio
async def test_dedup_apply_soft_delete_error_propagates_and_rolls_back():
    backend = Backend(find_groups=[[dict(GROUP_ROW)]])
    backend.memories.soft_delete_memory = SequenceRepo(RuntimeError("delete exploded"), raises=True)
    with pytest.raises(RuntimeError, match="delete exploded"):
        await dedup_exact_content(backend, apply=True)
    assert backend.rollbacks == 1
    assert backend.commits == 0


@pytest.mark.asyncio
async def test_dedup_apply_audit_chain_off_writes_no_audit_entry(monkeypatch):
    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "content_hash": "a" * 64,
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]])
    backend.audit_chain = RecordingAuditChain()
    # MNEMOS_AUDIT_CHAIN unset (conftest isolates env): audit_chain_enabled()
    # is False, so _maybe_write_delete_audit still calls the writer but the
    # writer short-circuits before touching the repository.
    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)
    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1
    assert backend.audit_chain.latest_calls == []
    assert backend.audit_chain.inserted == []


@pytest.mark.asyncio
async def test_dedup_apply_audit_chain_on_writes_delete_entry_with_dedup_writer(monkeypatch):
    """Audit chain ON: a signed delete entry lands in the audit chain with
    the dedup writer id. ``fetch_audit_snapshot`` (the refetch the helper
    performs only when the tx has a known dialect) is exercised separately
    below; the fake tx here is dialect-less, so supports_journal is False
    and the soft-deleted row itself is the snapshot.

    The switch lives in ``mnemos.core.config`` (the helper rebinds the name
    on every call), so that is what gets patched.
    """
    import mnemos.core.config as config

    row = {
        "id": "mem_dup",
        "owner_id": "alice",
        "namespace": "lab",
        "content_hash": "a" * 64,
        "memory_ids": ["mem_keep", "mem_dup"],
        "keep_id": "mem_keep",
    }
    backend = Backend(find_groups=[[row]])
    backend.audit_chain = RecordingAuditChain(latest=None)

    monkeypatch.setattr(config, "audit_chain_enabled_flag", lambda: True)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    payload = await dedup_exact_content(backend, apply=True)
    assert payload["deleted_count"] == 1
    assert len(backend.audit_chain.latest_calls) == 1
    assert len(backend.audit_chain.inserted) == 1
    entry = backend.audit_chain.inserted[0]
    assert entry["op"] == "delete"
    assert entry["writer_id"] == "mnemos-memory-dedup"
    # Best-effort default: a failed audit write must not fail the dedup.
    assert backend.commits == 1
    assert backend.rollbacks == 0


# ── _dispatch_delete_side_effects (direct) ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_delete_side_effects_returns_delivery_ids_and_nats_intents():
    row = {
        "id": "m9",
        "category": "facts",
        "subcategory": None,
        "content": "hello",
        "metadata": {},
        "owner_id": "alice",
        "namespace": "lab",
    }
    backend = Backend(webhooks=Webhooks(delivery_ids=["d1", "d2"]))
    delivery_ids, nats_intents = await _dispatch_delete_side_effects(backend, None, row)
    # delivery_ids are str(intent) — see the xfail'd test below for why this
    # is an opaque repr blob rather than the delivery id.
    assert len(delivery_ids) == 2
    assert str(delivery_ids[0]).startswith("WebhookDeliveryIntent(delivery_id='d1'")
    assert str(delivery_ids[1]).startswith("WebhookDeliveryIntent(delivery_id='d2'")
    assert "namespace='lab'" in str(delivery_ids[0])
    assert len(backend.webhooks.calls) == 1
    assert backend.webhooks.calls[0]["event_type"] == "memory.deleted"
    assert backend.webhooks.calls[0]["payload"]["memory_id"] == "m9"
    assert len(nats_intents) == 1
    assert nats_intents[0][0] == "mnemos.memory.deleted.lab"
    assert nats_intents[0][1]["memory_id"] == "m9"
    assert nats_intents[0][2] == "m9.deleted"


@pytest.mark.asyncio
async def test_dispatch_delete_side_effects_without_webhook_support_returns_no_deliveries():
    row = {"id": "m9", "namespace": None}
    backend = Backend(webhooks=Webhooks(delivery_ids=["d1"]))
    backend.supports_webhooks = False
    delivery_ids, nats_intents = await _dispatch_delete_side_effects(backend, None, row)
    assert delivery_ids == []
    assert len(nats_intents) == 1
    assert nats_intents[0][0] == "mnemos.memory.deleted.default"


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: delivery_ids stringifies WebhookDeliveryIntent via repr, not intent.delivery_id",
)
def test_dispatch_delivery_ids_are_stringified_intents():
    """DEFECT pinned: ``delivery_ids`` contains ``str(intent)`` rather than
    ``intent.delivery_id``, because ``WebhookDeliveryIntent`` defines no
    ``__str__`` and therefore stringifies via ``repr`` — an opaque blob
    instead of the delivery id a caller would expect to correlate on.

    xfail(strict=True): remove this mark when the payload carries
    ``intent.delivery_id`` (or the dataclass gains a ``__str__``).
    """
    intent = WebhookDeliveryIntent(
        delivery_id="d1",
        subscription_id="sub-fake",
        url="https://example.invalid/hook",
        namespace="lab",
        owner_id="alice",
    )
    assert str(intent) == "d1"
