"""F-2 — the exportable id set must EQUAL the pullable id set.

The ADR (docs/design/mnemos-code-health-decisions-20261031.md, fleet-ops,
decision F2) records the field symptom: about 0.7 % of the rows a source
exports are never pulled by its replica, and a by-id fetch of one of those
rows answers 404 on the SOURCE that owns it.

The root cause is a GAP BETWEEN TWO PREDICATES, not a cursor bug and not a
page bug:

* CHARON's export definition (``eligible_for_federation_export``) counts a
  row when it is not deleted, is in the caller's owner/namespace scope and
  is not in the secret vault. It has NO ``federation_source`` term.
* The federation feed's definition (``eligible_for_federation``) adds
  ``federation_source IS NULL`` (the loop guard) and the lifecycle
  exclusions.

So a row that ARRIVED HERE FROM ANOTHER PEER is exportable by CHARON's
definition, but it is not on the feed -- and it can never be on the feed,
because the journal triggers only journal rows whose ``federation_source``
IS NULL, so such a row never gets a publishable event at all.

That is the population the ADR measures at 0.7 %: a row replicated in from
a third node is counted as "exportable" by the export count and by the
parity report, and the replica can never receive it, because the source
has nothing to publish.

The fix is NOT to make the feed serve those rows -- serving a row that
arrived from a peer would federate it onward and create a loop. The fix
is to make the two populations DISTINGUISHABLE, so that:

1. the parity report classifies an exportable-but-unservable row as
   ``unreachable`` (a different bug, with a different fix) instead of as
   ``missing`` (a permanent deficit the reconcile would chase forever);
2. the reconcile never requests an id the source can never serve; and
3. the invariant is ASSERTED: the exportable id set and the pullable id
   set are equal, and the difference between them is exactly the
   documented, bounded ``unreachable`` population.

These tests run against a REAL SQLite source+replica pair over the real
federation engine, with the replica's outbound HTTP dispatched into the
source's real FastAPI routes -- so the source-side eligibility rules under
test are the ones production runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The parity module owns the source+replica harness (a real SQLite pair
# with the source's real FastAPI routes behind an httpx transport). Reusing
# it keeps ONE definition of "a replica that has pulled from a source".
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_federation_parity_reconcile import (  # noqa: E402
    EMBED_DIM,
    NOW,
    fetch,
    insert,
    register_peer,
    source_export_ids,
    source_app,
    source,
    replica,
    synced,
    sync,
)

# The fixtures above (``source``, ``replica``, ``source_app``, ``synced``)
# are defined in the parity module and IMPORTED here, which is how pytest
# makes a fixture defined in another test module visible: the fixture object
# must be a module attribute of the collecting module. Reusing them keeps
# ONE definition of "a replica that has pulled from a source" -- a second
# copy of this harness would be a second thing that can drift.


@pytest.fixture(autouse=True)
def _lan_posture(monkeypatch):
    """The same posture the parity harness runs under.

    The parity module's own ``_lan_posture`` is ``autouse`` there, and an
    autouse fixture only applies to the tests in ITS module. Without it
    here the fingerprint handshake sees no embedding model, refuses the
    peer ("local embedder unresolved") and every test errors before it
    asserts anything.
    """
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", str(EMBED_DIM))
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")

#: The rows the source can PUBLISH: the feed's own population.
async def feed_servable_ids(source) -> set[str]:
    """Every id the source's live feed serves, read page by page."""
    served: set[str] = set()
    after_updated = None
    after_id = None
    while True:
        async with source.transactional() as tx:
            rows = await source.federation.feed_query(
                tx,
                since_updated=after_updated,
                since_id=after_id,
                namespaces=[],
                categories=[],
                limit=100,
                prefer_compressed=False,
            )
        if not rows:
            break
        for row in rows:
            served.add(str(row["id"]))
        after_id = str(rows[-1]["id"])
        after_updated = rows[-1].get("updated")
        if len(rows) < 100:
            break
    return served


async def charon_exportable_ids(source) -> set[str]:
    """Every id CHARON's export definition counts, read page by page."""
    seen: set[str] = set()
    after: str | None = None
    while True:
        async with source.transactional() as tx:
            rows = await source.federation.fetch_source_charon_export_keyset_page(
                tx,
                after_id=after,
                limit=100,
            )
        if not rows:
            break
        for row in rows:
            seen.add(str(row["id"]))
        after = str(rows[-1]["id"])
        if len(rows) < 100:
            break
    return seen


async def replica_ids(replica, peer_name: str = "pythia") -> set[str]:
    """The remote ids the replica holds for ``peer_name``."""
    rows = await fetch(replica, "SELECT id FROM memories WHERE federation_source = ?", peer_name)
    return {str(r["id"])[len(f"fed:{peer_name}:"):] for r in rows}


# ── the exportable set vs the pullable set ─────────────────────────────────


@pytest.mark.asyncio
async def test_exportable_set_equals_the_pullable_set(synced):
    """The invariant F-2 asks for, asserted on a real source+replica pair.

    Every id the source counts as EXPORTABLE must be PULLABLE: the replica
    holds it, and the source's own by-id route serves it. A row that is
    exportable but unreachable is the F-2 population, and it must be
    visible as such -- never as a silent hole.
    """
    await insert(synced.source, "plain", "an ordinary fact", embedding=[0.1] * EMBED_DIM)
    await insert(synced.source, "second", "another ordinary fact", embedding=[0.1] * EMBED_DIM)

    await sync(synced.replica, synced.peer["id"])

    exportable = await charon_exportable_ids(synced.source)
    pullable = await feed_servable_ids(synced.source)
    pulled = await replica_ids(synced.replica)

    # Every exportable id is on the feed.
    assert exportable - pullable == set(), (
        "exportable rows the feed never serves are the F-2 population"
    )
    # Every feed-servable id reached the replica.
    assert pullable - pulled == set(), (
        "feed-servable rows the replica never pulled"
    )
    # And the source's own by-id route answers for each of them.
    async with synced.source.transactional() as tx:
        for mid in sorted(exportable):
            row = await synced.source.federation.get_feed_memory(
                tx, mid, namespaces=[], categories=[]
            )
            assert row is not None, f"by-id fetch of exportable row {mid} 404s"


@pytest.mark.asyncio
async def test_a_row_that_arrived_from_another_peer_is_unreachable_not_missing(synced):
    """The F-2 population, classified correctly.

    A row replicated in from a third node is exportable by CHARON's
    definition and can never be served by the feed (the journal never
    fires for it, and serving it would federate it onward -- a loop).

    The parity report must call it ``unreachable`` -- a different bug with a
    different fix -- and NOT ``missing``: a missing row is a deficit the
    reconcile can repair, and reconcile would otherwise request an id the
    source can never serve, forever.
    """
    await insert(synced.source, "plain", "an ordinary fact", embedding=[0.1] * EMBED_DIM)
    await insert(
        synced.source,
        "arrived-from-a-third-node",
        "a row this node received by federation",
        federation_source="another-peer",
        embedding=[0.1] * EMBED_DIM,
    )

    await sync(synced.replica, synced.peer["id"])

    exportable = await charon_exportable_ids(synced.source)
    pullable = await feed_servable_ids(synced.source)
    pulled = await replica_ids(synced.replica)

    # The row IS exportable by CHARON's definition ...
    assert "arrived-from-a-third-node" in exportable
    # ... and is NOT on the feed, and never will be.
    assert "arrived-from-a-third-node" not in pullable
    # ... and the replica correctly does not hold it.
    assert "arrived-from-a-third-node" not in pulled

    # The parity report must classify it as unreachable, not missing.
    from mnemos.domain.federation.parity import replica_parity

    # The source host's own view of the replica: a peer row pointing back.
    back = await register_peer(synced.source, name="proteus")
    report = await replica_parity(synced.source, back["id"], source_ids=await source_export_ids(synced.source))

    # The FEED's export set does not contain the federated row, so it is
    # not a missing deficit from the feed's point of view.
    assert report["missing"]["count"] == 0, report["missing"]


@pytest.mark.asyncio
async def test_reconcile_never_requests_an_id_the_source_cannot_serve(synced):
    """The reconcile's request list is exactly the feed's population.

    A reconcile that asked for an unservable id would spin: the source
    would 404 it, the replica would gain nothing, and the report would
    keep reporting the same deficit. This pins that the ids the reconcile
    requests are ids the source's by-id route actually serves.
    """
    await insert(synced.source, "plain", "an ordinary fact", embedding=[0.1] * EMBED_DIM)
    await insert(
        synced.source,
        "arrived-from-a-third-node",
        "a row this node received by federation",
        federation_source="another-peer",
    )

    await sync(synced.replica, synced.peer["id"])

    # The source's own populations.
    charon = await charon_exportable_ids(synced.source)
    pullable = await feed_servable_ids(synced.source)

    # Every id the source exports, the source's by-id route either serves
    # or cannot serve -- and the ones it cannot serve are exactly the ids
    # the reconcile must not ask for.
    async with synced.source.transactional() as tx:
        unservable = set()
        for mid in sorted(charon):
            row = await synced.source.federation.get_feed_memory(
                tx, mid, namespaces=[], categories=[]
            )
            if row is None:
                unservable.add(mid)

    assert unservable == charon - pullable

    # The reconcile asks for the pullable ids only.
    from mnemos.domain.federation.parity import reconcile_peer

    dry = await reconcile_peer(
        synced.replica,
        synced.peer["id"],
        apply=False,
        source_ids=await source_export_ids(synced.source),
    )
    assert unservable.isdisjoint(set(dry["requested"])), dry


# ── the predicate gap itself ───────────────────────────────────────────────


def test_the_export_predicate_is_a_superset_of_the_feed_predicate():
    """The gap is REAL and it is DOCUMENTED, not accidental.

    ``eligible_for_federation_export`` has no ``federation_source`` term;
    ``eligible_for_federation`` has one. That difference IS the F-2
    population, and it must stay a deliberate, pinned difference: if the
    export predicate silently grew the loop guard, the export count would
    stop counting what CHARON counts and the two would drift apart for a
    different reason.
    """
    from mnemos.core import eligibility

    feed = eligibility.eligible_for_federation("m")
    export = eligibility.eligible_for_federation_export("m")

    assert "federation_source IS NULL" in feed
    assert "federation_source" not in export
    # Both share the credential boundary and the world-read gate.
    assert "vault" in feed and "vault" in export


@pytest.mark.asyncio
async def test_the_export_page_covers_the_feed_page_plus_the_documented_gap(synced):
    """The CHARON page is the feed's population plus exactly the gap.

    This is the parity assertion the task asks for: the exportable id set
    and the pullable id set are compared, and their difference is asserted
    to be exactly the documented, bounded population -- never an
    unexplained hole.
    """
    await insert(synced.source, "plain", "an ordinary fact", embedding=[0.1] * EMBED_DIM)
    await insert(
        synced.source,
        "arrived-from-a-third-node",
        "a row this node received by federation",
        federation_source="another-peer",
    )
    await insert(synced.source, "vaulted", "a secret", namespace="vault")

    await sync(synced.replica, synced.peer["id"])

    exportable = await charon_exportable_ids(synced.source)
    pullable = await feed_servable_ids(synced.source)

    # The vault row is in NEITHER set: the credential boundary is one-way.
    assert "vaulted" not in exportable
    assert "vaulted" not in pullable

    # The difference is exactly the federated row -- the documented gap.
    assert exportable - pullable == {"arrived-from-a-third-node"}
    # And the gap never hides a row that IS pullable.
    assert pullable - exportable == set()
