"""Safe, reversible exact memory-content deduplication maintenance.

This module is intentionally backend-neutral at orchestration time: it uses
repository methods for hash backfill, duplicate discovery, and soft deletion so
normal audit/webhook/federation hooks can remain in the app layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mnemos.audit.route_helper import write_audit_entry
from mnemos.core.config import get_settings
from mnemos.nats.client import get_node_name as _nats_get_node_name
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.workers.audit_sealer import audit_chain_enabled

_DEDUP_USER_ID = "mnemos-memory-dedup"
_DEDUP_REASON = "exact-content duplicate soft-delete"


@dataclass(frozen=True)
class DedupGroup:
    owner_id: str
    namespace: str
    content_hash: str
    keep_id: str
    duplicate_ids: list[str]
    memory_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "namespace": self.namespace,
            "content_hash": self.content_hash,
            "keep_id": self.keep_id,
            "canonical_id": self.keep_id,
            "duplicate_ids": self.duplicate_ids,
            "memory_ids": self.memory_ids,
            "duplicate_count": len(self.memory_ids),
        }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        try:
            value = row.get(key, default)
        except AttributeError:
            return default
    return default if value is None else value


def _memory_ids_from_row(row: Any) -> list[str]:
    raw = _row_get(row, "memory_ids", [])
    if isinstance(raw, str):
        return [part for part in raw.split("\x1f") if part]
    return [str(value) for value in (raw or [])]


def _best_memory_id(row: Any, memory_ids: Sequence[str]) -> str | None:
    keep_id = _row_get(row, "keep_id") or _row_get(row, "canonical_id")
    return str(keep_id) if keep_id else (str(memory_ids[0]) if memory_ids else None)


def _group_from_row(row: Any) -> DedupGroup | None:
    memory_ids = _memory_ids_from_row(row)
    keep_id = _best_memory_id(row, memory_ids)
    if keep_id is None:
        return None
    duplicate_ids = [memory_id for memory_id in memory_ids if memory_id != keep_id]
    if not duplicate_ids:
        return None
    return DedupGroup(
        owner_id=str(_row_get(row, "owner_id", "")),
        namespace=str(_row_get(row, "namespace", "")),
        content_hash=str(_row_get(row, "content_hash", "")),
        keep_id=keep_id,
        duplicate_ids=duplicate_ids,
        memory_ids=memory_ids,
    )


def _root_mutation_visibility(namespace: str | None = None) -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=namespace,
    )


async def _maybe_write_delete_audit(backend: Any, tx: Any, row: Any, memory_id: str) -> None:
    if not audit_chain_enabled():
        return
    session_secret = (getattr(get_settings().server, "session_secret", "") or "").encode("utf-8")
    if not session_secret:
        return
    await write_audit_entry(
        backend,
        tx,
        op="delete",
        memory_id_str=memory_id,
        content=_row_get(row, "content", ""),
        category=_row_get(row, "category"),
        subcategory=_row_get(row, "subcategory"),
        metadata=None,
        embedding=None,
        writer_id=_DEDUP_USER_ID,
        session_secret=session_secret,
    )


async def _dispatch_delete_side_effects(backend: Any, tx: Any, row: Any) -> tuple[list[str], list[tuple[str, dict[str, Any], str]]]:
    delivery_ids: list[str] = []
    if getattr(backend, "supports_webhooks", True):
        delivery_ids = await backend.webhooks.dispatch_event(
            tx,
            "memory.deleted",
            {
                "memory_id": _row_get(row, "id"),
                "category": _row_get(row, "category"),
                "subcategory": _row_get(row, "subcategory"),
                "content": _row_get(row, "content"),
                "owner_id": _row_get(row, "owner_id"),
                "namespace": _row_get(row, "namespace"),
            },
            owner_id=_row_get(row, "owner_id"),
            namespace=_row_get(row, "namespace"),
        )

    namespace = _row_get(row, "namespace")
    safe_ns = (namespace or "default").replace(".", "_")
    nats_intents = [
        (
            f"mnemos.memory.deleted.{safe_ns}",
            {
                "memory_id": _row_get(row, "id"),
                "namespace": namespace,
                "category": _row_get(row, "category"),
                "source_node": _nats_get_node_name(),
            },
            f"{_row_get(row, 'id')}.deleted",
        )
    ]
    return delivery_ids, nats_intents


async def backfill_content_hashes(backend: Any, *, batch_size: int = 500, apply: bool = False) -> dict[str, Any]:
    """Backfill NULL ``content_hash`` rows in bounded batches.

    Dry-run is the default and reports the currently eligible NULL count. Apply
    mode repeatedly asks the repository to update at most ``batch_size`` rows,
    making the command idempotent and safe to re-run.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    total_updated = 0
    async with backend.transactional() as tx:
        while True:
            changed = await backend.memories.backfill_missing_content_hashes(
                tx,
                batch_size=batch_size,
                apply=apply,
            )
            if not apply:
                return {"apply": False, "batch_size": batch_size, "null_count": changed, "updated_count": 0}
            total_updated += int(changed or 0)
            if int(changed or 0) < batch_size:
                break
    return {"apply": True, "batch_size": batch_size, "updated_count": total_updated}


async def dedup_exact_content(backend: Any, *, namespace: str | None = None, apply: bool = False) -> dict[str, Any]:
    """Count or soft-delete exact content duplicates.

    Groups are active rows keyed by ``(content_hash, owner_id, namespace)``.
    The repository orders each group so the keeper is newest ``created`` with a
    higher ``quality_rating`` tie-break. Apply mode uses ``delete_memory`` per
    loser row rather than raw SQL so the mutation remains reversible and app
    side effects can be emitted.
    """
    delivery_ids: list[str] = []
    nats_intents: list[tuple[str, dict[str, Any], str]] = []
    deleted_count = 0
    async with backend.transactional() as tx:
        rows = await backend.memories.find_duplicate_content_groups(tx, namespace=namespace)
        groups = [group for row in rows if (group := _group_from_row(row)) is not None]
        if apply:
            for group in groups:
                visibility = _root_mutation_visibility(group.namespace)
                for duplicate_id in group.duplicate_ids:
                    row = await backend.memories.soft_delete_memory(
                        tx,
                        duplicate_id,
                        visibility=visibility,
                        requested_by=_DEDUP_USER_ID,
                        requested_at=None,
                        request_kind="dedup_exact_content",
                        reason=_DEDUP_REASON,
                        source=["mnemos.cli.memory-dedup"],
                    )
                    if not row:
                        continue
                    deleted_count += 1
                    await _maybe_write_delete_audit(backend, tx, row, duplicate_id)
                    item_delivery_ids, item_nats_intents = await _dispatch_delete_side_effects(backend, tx, row)
                    delivery_ids.extend(str(item) for item in item_delivery_ids)
                    nats_intents.extend(item_nats_intents)
    return {
        "apply": apply,
        "namespace": namespace,
        "groups": [group.as_dict() for group in groups],
        "group_count": len(groups),
        "duplicate_count": sum(len(group.duplicate_ids) for group in groups),
        "deleted_count": deleted_count,
        "delivery_ids": delivery_ids,
        "nats_intents": nats_intents,
    }


__all__ = [
    "backfill_content_hashes",
    "dedup_exact_content",
]
