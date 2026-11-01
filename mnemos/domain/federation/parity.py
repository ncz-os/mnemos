"""F-15 replica parity and reconcile.

A replica's ``fed:<peer>:*`` population is compared with the source's
exportable population by ``id`` (identity), stored ``content_hash``
(drift), and embedding presence. Both sides are read through explicit
keyset pages -- ``fetch_source_export_keyset_page`` on the source and
``fetch_parity_page`` on the replica -- so a comparison over a 25k-row
replica costs bounded memory, never a full-table load.

Classification is per id:

* ``missing``       -- exportable on the source, absent from the replica
* ``extra``         -- ``fed:<peer>:*`` on the replica, no longer exportable upstream
* ``stale``         -- present on both sides with a different content hash
* ``null_embedding``-- present on the replica without a stored vector
                       (the field problem: a replica whose column dimension
                       disagreed with the fleet standard stored content and
                       no vector, silently)

Reconcile is dry-run first. ``apply=True`` re-pulls only the ids the report
classified as ``missing``; it never deletes or overwrites a replica row.
Re-pull goes through the source's own journal (``redispatch_federation_journal``)
so the receiver's sequence fence sees a NEW event, then through the normal
sync path, which is what keeps the receiver's bookkeeping (receive state,
sync log, cursor) exactly as consistent as an ordinary pull.

Finding the source's coordinates for a peer whose configured
``base_url`` is not reachable from the node running the report is the
caller's job: pass ``base_url``/``auth_token`` explicitly, as
``mnemos federation parity --source-url`` does from the source host.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 500
DEFAULT_SAMPLE = 50

# Two populations, deliberately separate: the 0.7% of exportable rows never
# pulled ("missing") and the rows that could never be pulled because the
# source's own by-id path rejects them ("unreachable") are different bugs
# with different fixes, and merging them would hide the second.
POPULATIONS = ("missing", "extra", "stale", "null_embedding", "unreachable")


def _bucket(ids: list[str], page_size: int) -> dict[str, Any]:
    """Bounded id sample per population, plus an honest truncation flag."""
    sample_cap = max(0, min(DEFAULT_SAMPLE, page_size))
    return {
        "count": len(ids),
        "sample": sorted(ids)[:sample_cap],
        "sample_truncated": len(ids) > sample_cap,
    }


async def _source_keyset(backend, *, namespaces, categories, page_size: int) -> dict[str, str | None]:
    """Every exportable ``id -> content_hash`` on this node, by keyset page."""
    seen: dict[str, str | None] = {}
    after: str | None = None
    repo = backend.federation
    while True:
        async with backend.transactional() as tx:
            rows = await repo.fetch_source_export_keyset_page(
                tx,
                namespaces=namespaces,
                categories=categories,
                after_id=after,
                limit=page_size,
            )
        if not rows:
            return seen
        for row in rows:
            seen[str(row["id"])] = row.get("content_hash")
            after = str(row["id"])
        if len(rows) < page_size:
            return seen


async def _replica_keyset(backend, peer_name: str, *, page_size: int) -> dict[str, dict[str, Any]]:
    """This node's ``fed:<peer_name>:*`` rows, by keyset page on ``local_id``."""
    seen: dict[str, dict[str, Any]] = {}
    after: str | None = None
    repo = backend.federation
    while True:
        async with backend.transactional() as tx:
            rows = await repo.fetch_parity_page(tx, after_id=after, limit=page_size)
        if not rows:
            return seen
        for row in rows:
            local_id = str(row["local_id"])
            after = local_id
            if str(row.get("peer_name") or "") != peer_name:
                continue
            seen[str(row.get("remote_id") or local_id)] = {
                "local_id": local_id,
                "content_hash": row.get("content_hash"),
                "has_embedding": bool(row.get("has_embedding")),
            }
        if len(rows) < page_size:
            return seen


async def _feed_ids(
    backend,
    *,
    namespaces: list[str],
    categories: list[str],
    page_size: int,
) -> set[str]:
    """Every id this node's own live feed serves, read by keyset page."""
    served: set[str] = set()
    after_updated = None
    after_id = None
    while True:
        async with backend.transactional() as tx:
            rows = await backend.federation.feed_query(
                tx,
                since_updated=after_updated,
                since_id=after_id,
                namespaces=namespaces,
                categories=categories,
                limit=page_size,
                prefer_compressed=False,
            )
        if not rows:
            break
        for row in rows:
            served.add(str(row["id"]))
            after_id = str(row["id"])
            after_updated = row.get("updated")
        if len(rows) < page_size:
            break
    return served


async def _populations(
    backend,
    peer_id: str,
    *,
    namespaces: list[str],
    categories: list[str],
    page_size: int,
    source_ids: Mapping[str, str | None] | set[str] | None = None,
) -> dict[str, Any]:
    """Classify every id once; both public entry points read this.

    ``source_ids`` is the peer's exportable id set, when the caller knows it
    (it can only be read on the source host, or supplied out of band). When
    it is ``None`` this node is a replica and its own export predicate says
    nothing about the source: ``extra``/``stale``/``unreachable`` are left
    empty and ``source_available`` is False, so the report never claims
    "everything is extra" just because the replica's own corpus is small.
    """
    async with backend.transactional() as tx:
        peer = await backend.federation.get_sync_peer(tx, peer_id)
    if not peer:
        raise ValueError(f"peer {peer_id} not found")
    peer_name = str(peer["name"])

    replicated = await _replica_keyset(backend, peer_name, page_size=page_size)

    if source_ids is None:
        local = await _source_keyset(backend, namespaces=namespaces, categories=categories, page_size=page_size)
        async with backend.transactional() as tx:
            exportable_count = await backend.federation.count_exportable_rows(
                tx, namespaces=namespaces, categories=categories
            )
        source_available = False
        missing: list[str] = []
        extra: list[str] = []
        stale: list[str] = []
        unreachable: list[str] = []
        # A row the replica holds with no vector is still a real finding; it
        # is read entirely from the replica.
        null_embedding = [mid for mid, row in replicated.items() if not row["has_embedding"]]
        return {
            "peer": peer,
            "peer_name": peer_name,
            "exportable_count": exportable_count,
            "local_exportable": len(local),
            "replicated": replicated,
            "source_available": source_available,
            "missing": missing,
            "unreachable": unreachable,
            "extra": extra,
            "stale": stale,
            "null_embedding": sorted(null_embedding),
        }

    exportable = (
        {str(k): v for k, v in source_ids.items()}
        if isinstance(source_ids, Mapping)
        else {str(mid): None for mid in source_ids}
    )
    exportable_count = len(exportable)
    # "Exportable but unservable" is the F-15 eligibility gap: the journal
    # triggers (0062/0064) only journal rows whose ``federation_source`` is
    # NULL, so a row replicated from another peer is counted by CHARON's
    # export definition but has no publishable event. It is only observable on
    # the node that OWNS the journal, and this branch is the caller-supplied
    # case (the population was read elsewhere), so no gap can be attributed
    # from here: enumerating this node's own federated rows -- none of them
    # publishable -- would mislabel every missing id as unservable. The gap is
    # documented in ``eligible_for_federation_export`` and pinned by
    # ``test_export_definition_is_a_superset_of_the_feed``; closing it needs a
    # trigger change on all six backends and is out of scope here.
    unreachable: list[str] = []

    missing = sorted(mid for mid in exportable if mid not in replicated and mid not in unreachable)
    extra = sorted(mid for mid in replicated if mid not in exportable)
    stale = sorted(
        mid
        for mid, content_hash in exportable.items()
        if content_hash
        and mid in replicated
        and replicated[mid]["content_hash"]
        and replicated[mid]["content_hash"] != content_hash
    )
    null_embedding = sorted(mid for mid, row in replicated.items() if not row["has_embedding"])
    return {
        "peer": peer,
        "peer_name": peer_name,
        "exportable_count": exportable_count,
        "local_exportable": len(exportable),
        "replicated": replicated,
        "source_available": True,
        "missing": missing,
        "unreachable": unreachable,
        "extra": extra,
        "stale": stale,
        "null_embedding": null_embedding,
    }


async def replica_parity(
    backend,
    peer_id: str,
    *,
    namespaces: list[str] | None = None,
    categories: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    source_url: str | None = None,
    auth_token: str | None = None,
    source_ids: Mapping[str, str | None] | set[str] | None = None,
) -> dict[str, Any]:
    """Compare this node's replica of ``peer_id`` with the source's exports.

    Returns counts (plus a bounded id sample) per population. ``in_sync``
    is true only when every population is empty -- "found nothing" and
    "looked and found everything" must never print the same.

    Run this on the SOURCE host to get a full comparison, or pass the
    source's exportable id set as ``source_ids`` (what the source host's own
    ``replica_parity(..., source_ids=...)`` reports). Run on a replica with
    neither, the report is honest about what it cannot see:
    ``source_available`` is false and ``missing``/``extra``/``stale`` are
    zero rather than pretending the replica's own corpus is the source's.
    """
    page_size = max(1, min(int(page_size), 5000))
    namespaces = list(namespaces or [])
    categories = list(categories or [])

    populations = await _populations(
        backend,
        peer_id,
        namespaces=namespaces,
        categories=categories,
        page_size=page_size,
        source_ids=source_ids,
    )
    peer = populations["peer"]
    peer_name = populations["peer_name"]
    exportable_count = populations["exportable_count"]
    replicated = populations["replicated"]
    missing = populations["missing"]
    extra = populations["extra"]
    stale = populations["stale"]
    null_embedding = populations["null_embedding"]
    unreachable = populations["unreachable"]
    source_available = populations["source_available"]

    counts = {
        "missing": _bucket(missing, page_size),
        "extra": _bucket(extra, page_size),
        "stale": _bucket(stale, page_size),
        "null_embedding": _bucket(null_embedding, page_size),
        "unreachable": _bucket(unreachable, page_size),
    }
    counts["null_embedding"]["population"] = "replica"
    in_sync = not (missing or extra or stale or null_embedding)
    hint = None
    if missing:
        hint = (
            f"POST /v1/federation/peers/{peer_id}/reconcile with apply=true re-pulls the missing ids "
            "through the source journal (dry-run when apply is false)"
        )
    return {
        "peer": peer_name,
        "peer_id": peer_id,
        "source_url": source_url or peer.get("base_url"),
        "namespaces": namespaces,
        "categories": categories,
        "page_size": page_size,
        # ``exportable_rows`` is THIS node's own exportable population. On
        # the source host that is the feed population; on a replica it is
        # only what the replica itself would export, which is not the
        # peer's population and must not be printed as if it were. The
        # authoritative source count is obtained by running the same
        # report on the source host (``mnemos federation parity``) -- the
        # report never invents a number it cannot read.
        "source": {
            "exportable_rows": exportable_count if source_available else None,
            "available": source_available,
        },
        "replica": {"rows": len(replicated), "peer": peer_name},
        "missing": counts["missing"],
        "extra": counts["extra"],
        "stale": counts["stale"],
        "null_embedding": counts["null_embedding"],
        "unreachable": counts["unreachable"],
        "in_sync": in_sync,
        "hint": hint,
    }


async def reconcile_peer(
    backend,
    peer_id: str,
    *,
    apply: bool = False,
    namespaces: list[str] | None = None,
    categories: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    limit: int | None = None,
    source_ids: Mapping[str, str | None] | set[str] | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Re-pull the ids the parity report calls ``missing``.

    ``apply=False`` (the default) is a dry run: it reports exactly what it
    would request and touches neither the source nor the replica.

    ``source_ids`` is the peer's exportable id set, read on the source host
    (see ``replica_parity``). Without it the replica cannot know what it is
    missing and the reconcile reports an empty request rather than guessing.
    """

    report = await replica_parity(
        backend,
        peer_id,
        namespaces=namespaces,
        categories=categories,
        page_size=page_size,
        source_ids=source_ids,
    )
    requested = list(report["missing"]["sample"])
    if report["missing"]["count"] > len(requested):
        # ``sample`` is bounded; a reconcile must cover the whole population
        # it reports, not just the part an operator can see.
        async with backend.transactional() as tx:
            peer = await backend.federation.get_sync_peer(tx, peer_id)
        replicated = await _replica_keyset(backend, str(peer["name"]), page_size=page_size)
        all_ids = (
            source_ids
            if source_ids is not None
            else {
                mid
                for mid in await _source_keyset(
                    backend,
                    namespaces=list(namespaces or []),
                    categories=list(categories or []),
                    page_size=page_size,
                )
            }
        )
        requested = sorted(mid for mid in all_ids if mid not in replicated)
    if limit is not None:
        requested = requested[: max(0, int(limit))]

    result = {
        "peer": report["peer"],
        "peer_id": peer_id,
        "applied": bool(apply),
        "requested": requested,
        "restored": 0,
        "failed": [],
        "missing_before": report["missing"]["count"],
        "remaining": report["missing"]["count"],
    }
    if not apply or not requested:
        return result

    # The journal belongs to the SOURCE. A replica cannot publish an event on
    # its peer's behalf, so re-advertisement is an HTTP call to the peer --
    # the same origin the feed and by-id paths already use.
    from mnemos.domain import federation as _fed

    async with backend.transactional() as tx:
        peer_row = await backend.federation.get_sync_peer(tx, peer_id)
    if not peer_row:
        raise ValueError(f"peer {peer_id} not found")
    base_url = source_url or str(peer_row["base_url"])
    redispatched = await _redispatch_on_source(
        base_url.rstrip("/"),
        str(peer_row["auth_token"]),
        requested,
    )
    if redispatched:
        # The receiver's fence only advances on a new sequence; the source
        # has just published one for every id we are about to fetch.
        await _fed.sync_peer(backend, peer_id)

    after = await replica_parity(
        backend,
        peer_id,
        namespaces=namespaces,
        categories=categories,
        page_size=page_size,
        source_ids=source_ids,
    )
    replicated = await _replica_keyset(backend, report["peer"], page_size=page_size)
    for mid in requested:
        if mid in replicated:
            result["restored"] += 1
        else:
            result["failed"].append(
                {
                    "id": mid,
                    "reason": "not served by the peer's by-id path after re-dispatch "
                    "(withdrawn, or exports disabled for this scope)",
                }
            )
    result["remaining"] = after["missing"]["count"]
    if after["missing"]["count"]:
        logger.warning(
            "[federation/parity] peer=%s reconcile restored %d of %d requested ids; %d still missing",
            report["peer"],
            result["restored"],
            len(requested),
            after["missing"]["count"],
        )
    return result


__all__ = ["DEFAULT_PAGE_SIZE", "POPULATIONS", "reconcile_peer", "replica_parity"]


async def _redispatch_on_source(base_url: str, auth_token: str, memory_ids: list[str]) -> int:
    """Ask the source peer to re-advertise ``memory_ids`` on its journal.

    Returns how many ids the source actually re-published -- fewer than
    requested simply means some ids are no longer exportable there, which
    the caller reports as ``failed`` rather than pretending they were served.
    """
    from mnemos.domain import federation as _fed

    url = base_url.rstrip("/") + "/v1/federation/redispatch"
    client, _ = await _fed.make_safe_client(
        url,
        timeout=_fed.FEDERATION_HTTP_TIMEOUT,
        allow_private=_fed._federation_allow_private(),
    )
    try:
        response = await client.post(
            url,
            json={"memory_ids": memory_ids},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        if response.status_code in (404, 405):
            # A peer older than this release has no redispatch route. Nothing
            # was re-published, so reconcile reports every id as failed.
            return 0
        response.raise_for_status()
        body = response.json()
    finally:
        await client.aclose()
    return int(body.get("redispatched") or 0)
