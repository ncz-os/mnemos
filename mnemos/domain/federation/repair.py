"""MN-24 / D-MN5: repair rows fenced before the replica fence existed.

A replica row damaged by a LOCAL lifecycle sweep (soft-delete, archival, a
local consolidation) *before* the replica fence shipped can never heal: the
receiver's durable sequence fence (``federation_receive_state``) drops the
source's replay before any write, so the row stays wrong forever and the
only fallback is a full re-seed of the peer.

``repair_fenced_replicas`` is the one-off cleanup, dry run by default. The
operator reads the dry-run list and applies it once. Apply, per row:

1. re-read the candidate inside the mutation transaction;
2. skip rows the source actually withdrew (the fence says ``withdrawn``) --
   a revocation must never come back as live data;
3. skip consolidations the PEER itself published (the row's ``metadata``
   carries the ``federation_consolidation`` marker the tombstone path
   writes) -- a peer's own consolidation is not local damage;
4. otherwise: delete the archive blob (the archive table references the row
   with ``ON DELETE RESTRICT``, so the blob goes first), delete the local
   copy, clear the row's receive-state fence, and write an audit delete
   entry when the audit chain is on.

After all rows, the peer's journal cursors are cleared so the next ordinary
sync replays from the source and re-pulls every repaired row through the
normal write path -- no new SQL surface, no source cooperation needed. Per-row
failures are collected, never raised: one bad id must not stop the run, as
``reconcile_peer`` already models.

Storage goes through the ``FederationRepository`` ABC
(``fetch_fenced_damaged_replicas``, ``clear_federation_receive_fence``,
``clear_federation_peer_cursors``, ``delete_federated_archive_blob`` plus the
existing ``delete_federated_memory``) -- never raw ``_lc._pool`` access.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 500

# The tombstone path (``_apply_consolidation_tombstone``) stamps this marker
# into the loser's ``metadata``. A consolidated row that carries it is the
# PEER's own consolidation applied here -- not damage a local sweep did -- so
# the repair must leave it alone. A locally consolidated row has
# ``consolidated_into`` set WITHOUT this marker.
_PEER_CONSOLIDATION_MARKER = "federation_consolidation"


def _is_peer_consolidated(metadata: Any) -> bool:
    if not metadata:
        return False
    if isinstance(metadata, dict):
        return _PEER_CONSOLIDATION_MARKER in metadata
    try:
        return _PEER_CONSOLIDATION_MARKER in json.loads(str(metadata))
    except (ValueError, TypeError):
        return False


def _damage_of(row: Any) -> list[str]:
    def _get(key: str) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return getattr(row, key, None)

    damage = []
    if _get("deleted_at") is not None:
        damage.append("deleted")
    if _get("archived_at") is not None:
        damage.append("archived")
    if _get("consolidated_into") is not None:
        damage.append("consolidated")
    return damage


async def _damaged_for_peer(backend, peer_name: str, *, page_size: int) -> list[dict[str, Any]]:
    """Every locally damaged replica row for one peer, keyset-paged."""
    found: list[dict[str, Any]] = []
    after_id: str | None = None
    repo = backend.federation
    while True:
        async with backend.transactional() as tx:
            rows = await repo.fetch_fenced_damaged_replicas(tx, peer_name=peer_name, after_id=after_id, limit=page_size)
        if not rows:
            return found
        for row in rows:
            try:
                local_id = str(row["local_id"])
                remote_id = str(row["remote_id"])
            except (KeyError, IndexError, TypeError):
                local_id = str(getattr(row, "local_id", ""))
                remote_id = str(getattr(row, "remote_id", ""))
            try:
                withdrawn = row["withdrawn"]
            except (KeyError, IndexError, TypeError):
                withdrawn = getattr(row, "withdrawn", None)
            try:
                metadata = row["metadata"]
            except (KeyError, IndexError, TypeError):
                metadata = getattr(row, "metadata", None)
            found.append(
                {
                    "local_id": local_id,
                    "remote_id": remote_id,
                    "damage": _damage_of(row),
                    "withdrawn": bool(int(withdrawn)) if withdrawn is not None else False,
                    "peer_consolidated": _is_peer_consolidated(metadata),
                }
            )
            after_id = local_id
        if len(rows) < page_size:
            return found


async def repair_fenced_replicas(
    backend,
    peer_id: str | None = None,
    *,
    dry_run: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """List (dry run) or repair (``dry_run=False``) pre-fence-damaged replicas.

    ``peer_id=None`` repairs every peer this node holds replicas from; an id
    narrows the run to that peer. The default changes nothing: it reports
    the candidates the operator would apply.

    Returns ``{peers, candidates, repaired, skipped_withdrawn,
    skipped_consolidated, failed}``. ``candidates`` is the dry-run list the
    operator reviews (``remote_id`` + what is wrong with each row);
    ``failed`` collects per-row errors on an apply run without stopping it.
    """
    page_size = max(1, min(int(page_size), 5000))
    async with backend.transactional() as tx:
        rows = await backend.federation.list_peers(tx)
    by_id = {str(r["id"]): str(r["name"]) for r in rows}
    if peer_id is not None:
        if peer_id not in by_id:
            raise ValueError(f"peer {peer_id!r} not found")
        peers = [by_id[peer_id]]
    else:
        peers = sorted(set(by_id.values()))

    candidates: list[dict[str, Any]] = []
    for name in peers:
        for row in await _damaged_for_peer(backend, name, page_size=page_size):
            candidates.append({"peer": name, **row})

    result: dict[str, Any] = {
        "peers": peers,
        "dry_run": bool(dry_run),
        "candidates": sorted(candidates, key=lambda c: (c["peer"], c["remote_id"])),
        "repaired": 0,
        "skipped_withdrawn": 0,
        "skipped_consolidated": 0,
        "failed": [],
    }
    if dry_run:
        return result

    from mnemos.workers.audit_sealer import audit_chain_enabled

    for cand in result["candidates"]:
        name = cand["peer"]
        remote_id = cand["remote_id"]
        local_id = cand["local_id"]
        try:
            async with backend.transactional() as tx:
                repo = backend.federation
                # Re-read the fence inside the mutation transaction: a
                # withdrawal that landed after the dry run must not be
                # resurrected by a stale candidate list. Keyset-page to the
                # candidate's id -- the first page alone is not enough when
                # the population exceeds one page.
                match = None
                cursor: str | None = None
                while True:
                    fresh = await repo.fetch_fenced_damaged_replicas(
                        tx, peer_name=name, after_id=cursor, limit=page_size
                    )
                    if not fresh:
                        break
                    for row in fresh:
                        try:
                            rid = str(row["remote_id"])
                            meta = row["metadata"]
                            wdn = row["withdrawn"]
                            lid = str(row["local_id"])
                        except (KeyError, IndexError, TypeError):
                            rid = str(getattr(row, "remote_id", ""))
                            meta = getattr(row, "metadata", None)
                            wdn = getattr(row, "withdrawn", None)
                            lid = str(getattr(row, "local_id", ""))
                        if rid == remote_id:
                            match = (wdn, meta)
                            break
                        cursor = lid
                    if match is not None or len(fresh) < page_size:
                        break
                if match is None:
                    # Already gone (a concurrent repair or a re-pull fixed
                    # it): nothing to do, and not a failure.
                    continue
                withdrawn, metadata = match
                if withdrawn is not None and bool(int(withdrawn)):
                    result["skipped_withdrawn"] += 1
                    continue
                if _is_peer_consolidated(metadata):
                    result["skipped_consolidated"] += 1
                    continue
                snapshot = None
                if audit_chain_enabled():
                    from mnemos.audit.route_helper import (
                        fetch_audit_snapshot,
                    )

                    snapshot = await fetch_audit_snapshot(tx, local_id)
                await repo.delete_federated_archive_blob(tx, local_id=local_id)
                await repo.delete_federated_memory(tx, name, remote_id)
                await repo.clear_federation_receive_fence(tx, peer_name=name, remote_id=remote_id)
                if snapshot is not None:
                    from mnemos.audit.route_helper import write_transaction_audit

                    await write_transaction_audit(
                        tx,
                        op="delete",
                        memory_id_str=local_id,
                        snapshot=snapshot,
                        writer_id=f"fed:{name}",
                    )
                result["repaired"] += 1
        except Exception as exc:  # noqa: BLE001 -- collected, never raised
            logger.warning(
                "[federation/repair] peer=%s id=%s repair failed: %s",
                name,
                remote_id,
                exc,
            )
            result["failed"].append({"peer": name, "id": remote_id, "error": str(exc)})

    # The next sync must replay from the source: without a journal cursor for
    # any filter scope it does, and every cleared fence re-pulls as a fresh
    # insert through the normal write path.
    async with backend.transactional() as tx:
        for row in await backend.federation.list_peers(tx):
            if str(row["name"]) in peers:
                await backend.federation.clear_federation_peer_cursors(tx, peer_id=str(row["id"]))
    return result


__all__ = ["DEFAULT_PAGE_SIZE", "repair_fenced_replicas"]
