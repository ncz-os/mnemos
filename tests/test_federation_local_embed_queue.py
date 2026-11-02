"""F-3 — a federated row must never sit at ``embedding IS NULL`` forever.

The field problem: on the PROTEUS replica every one of the 24,579 rows pulled
from PYTHIA landed with a NULL embedding, because federation only ever COPIES
a peer's vector (``copy_embeddings``) and never embeds locally.  Vector search
excludes NULL-embedding rows, so the whole replica corpus was FTS-only.  The
PROTEUS operator had to run a manual 25 k-row backfill by hand after an
embedding-column resize.

On the base commit nothing in the tree queues a NULL-embedding federated row:

* ``insert_federated_memory`` omits the embedding column on every backend, so
  a pulled row is born without a vector;
* ``_store_memories`` never calls the embedder -- it only copies a peer vector
  when the peer opted in AND the model/dim fingerprints agree, and every skip
  is counted and logged;
* the only thing that ever filled a NULL embedding was a hand-run script
  (``scripts/backfill_embeddings*.py``), never the service.

So the row was durable, countable, reported -- and invisible to semantic
search forever.  This module pins the fix:

1. the queue is the PREDICATE ``federation_source IS NOT NULL AND embedding IS
   NULL`` (durable by construction: a restart resumes, nothing is held in
   memory between passes);
2. every new federated row whose embedding is NULL is queued, bounded and
   idempotent -- the same row is embedded exactly once;
3. the counters are honest: a row WAITING for an embedding is reported as
   waiting, not as embedded.

The tests run on SQLite (the backend the field incident was reproduced on);
the cross-backend SQL is pinned statically.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain import federation as fed
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

PEER = "pythia"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    """A real SQLite backend sized for 3-dimension vectors."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    instance = SqliteBackend(tmp_path / "fed-local-embed.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _make_peer(backend, name=PEER):
    async with backend.transactional() as tx:
        return await backend.federation.create_peer(
            tx,
            name=name,
            base_url="https://pythia.example",
            auth_token="token-abc",
            namespace_filter=None,
            category_filter=None,
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )


def _wire_row(remote_id, *, content="facts", sequence=1, updated="2020-01-01T00:00:00Z"):
    """One inbound federation wire item, shaped like /v1/federation/feed."""
    return {
        "id": remote_id,
        "content": content,
        "category": "facts",
        "namespace": "default",
        "created": updated,
        "updated": updated,
        "federation_sequence": sequence,
    }


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _install_pull(monkeypatch, batches):
    """Wire ``_pull_batch`` to serve canned pages."""
    state = {"index": 0}

    async def fake_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        index = min(state["index"], len(batches) - 1)
        state["index"] += 1
        body = batches[index]
        next_cursor = body.get("next_cursor")
        if next_cursor:
            next_cursor = fed._decode_feed_cursor(next_cursor)
        return body.get("memories", []), next_cursor, bool(body.get("has_more"))

    async def fake_client(*args, **kwargs):
        return _NullCtx(), object()

    async def schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "7.0.0",
            "schema_signature": "7.0",
            "migrations_fingerprint": None,
        }

    monkeypatch.setattr(fed, "_check_peer_schema", schema_ok)
    monkeypatch.setattr(fed, "make_safe_client", fake_client)
    monkeypatch.setattr(fed, "_pull_batch", fake_pull)


async def _run(backend, statement, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(statement, *params)


async def _pull_one_row(backend, monkeypatch, *, remote_id="m1", content="facts", sequence=1):
    """Run one federation sync that applies exactly one row."""
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row(remote_id, content=content, sequence=sequence)],
            "next_cursor": fed._encode_feed_cursor(
                fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), f"journal:{sequence}"
            ),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)
    await fed.sync_peer(backend, peer["id"])
    return peer


# ── The defect: a pulled row is born without a vector and nothing queues it ──


@pytest.mark.asyncio
async def test_a_pulled_federated_row_is_queued_for_embedding(backend, monkeypatch):
    """The regression: a pulled row with no vector must be VISIBLE to the queue.

    On the base commit the row lands with ``embedding IS NULL`` and no queue
    predicate exists at all, so nothing ever embeds it and the replica stays
    FTS-only forever.
    """
    await _pull_one_row(backend, monkeypatch)

    async with backend.transactional() as tx:
        queued = await backend.federation.list_federated_missing_embeddings(tx, after_id=None, limit=10)
        depth = await backend.federation.count_federated_missing_embeddings(tx)

    assert [row["id"] for row in queued] == ["fed:pythia:m1"], (
        "the pulled row landed with no vector and nothing queued it: it is invisible "
        "to semantic search until something embeds it"
    )
    assert depth == 1


@pytest.mark.asyncio
async def test_the_queue_skips_rows_that_already_carry_a_vector(backend, monkeypatch):
    """A row that DID get a vector is not queued again."""
    await _pull_one_row(backend, monkeypatch)

    async with backend.transactional() as tx:
        await backend.memories.upsert_memory_embedding(tx, "fed:pythia:m1", [0.1, 0.2, 0.3])

    async with backend.transactional() as tx:
        queued = await backend.federation.list_federated_missing_embeddings(tx, after_id=None, limit=10)
        depth = await backend.federation.count_federated_missing_embeddings(tx)

    assert queued == []
    assert depth == 0


@pytest.mark.asyncio
async def test_the_queue_ignores_a_deleted_replica(backend, monkeypatch):
    """A deleted replica is not work to redo."""
    await _pull_one_row(backend, monkeypatch)

    await _run(
        backend,
        "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
        "fed:pythia:m1",
    )

    async with backend.transactional() as tx:
        depth = await backend.federation.count_federated_missing_embeddings(tx)
    assert depth == 0


@pytest.mark.asyncio
async def test_the_queue_ignores_an_archived_replica(backend, monkeypatch):
    """An archived replica is not work to redo."""
    await _pull_one_row(backend, monkeypatch)

    await _run(
        backend,
        "UPDATE memories SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
        "fed:pythia:m1",
    )

    async with backend.transactional() as tx:
        depth = await backend.federation.count_federated_missing_embeddings(tx)
    assert depth == 0


@pytest.mark.asyncio
async def test_the_queue_ignores_a_local_row(backend, monkeypatch):
    """A row this node wrote itself is not federation work."""
    await _pull_one_row(backend, monkeypatch)
    await _run(
        backend,
        "INSERT INTO memories (id, content, category, metadata, verbatim_content, quality_rating,"
        " owner_id, namespace, permission_mode, created, updated)"
        " VALUES ('local-1', 'mine', 'facts', '{}', 'mine', 75, 'me', 'default', 600,"
        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    )

    async with backend.transactional() as tx:
        queued = await backend.federation.list_federated_missing_embeddings(tx, after_id=None, limit=10)
    assert [row["id"] for row in queued] == ["fed:pythia:m1"]


# ── The worker: bounded, idempotent, and honest ──────────────────────────────


@pytest.mark.asyncio
async def test_the_local_embed_worker_embeds_the_queued_row(backend, monkeypatch):
    """One pass embeds the queued row and drains the queue."""
    await _pull_one_row(backend, monkeypatch, content="the fleet standard is bge-m3")

    from mnemos.domain.federation.local_embed import embed_pending_once

    seen: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        seen.append(text)
        return [0.1, 0.2, 0.3]

    result = await embed_pending_once(backend, embed=fake_embed)

    assert result["embedded"] == 1
    assert result["remaining"] == 0
    assert len(seen) == 1
    assert "bge-m3" in seen[0]

    async with backend.transactional() as tx:
        depth = await backend.federation.count_federated_missing_embeddings(tx)
    assert depth == 0


@pytest.mark.asyncio
async def test_the_local_embed_worker_is_idempotent(backend, monkeypatch):
    """A second pass embeds nothing: the queue is drained by the first pass."""
    await _pull_one_row(backend, monkeypatch)

    from mnemos.domain.federation.local_embed import embed_pending_once

    calls: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        calls.append(text)
        return [0.1, 0.2, 0.3]

    first = await embed_pending_once(backend, embed=fake_embed)
    second = await embed_pending_once(backend, embed=fake_embed)

    assert first["embedded"] == 1
    assert second["embedded"] == 0
    assert len(calls) == 1, "the same row must not be embedded twice"


@pytest.mark.asyncio
async def test_a_wrong_dimension_vector_is_refused_and_the_row_stays_queued(backend, monkeypatch):
    """F-4's guard, as far as it is merged: a wrong-width vector never lands."""
    await _pull_one_row(backend, monkeypatch)

    from mnemos.domain.federation.local_embed import embed_pending_once

    async def wrong_dim(text: str) -> list[float]:
        return [0.1, 0.2]

    result = await embed_pending_once(backend, embed=wrong_dim)

    assert result["embedded"] == 0
    assert result["failed"] == 1
    assert result["remaining"] == 1, "a refused vector must leave the row queued"


@pytest.mark.asyncio
async def test_the_pass_is_bounded(backend, monkeypatch):
    """A pass embeds at most ``max_rows`` rows, no matter how deep the queue."""
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row(f"m{i}", sequence=i) for i in range(1, 6)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:5"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)
    await fed.sync_peer(backend, peer["id"])

    from mnemos.domain.federation.local_embed import embed_pending_once

    async def fake_embed(text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    result = await embed_pending_once(backend, embed=fake_embed, max_rows=2)

    assert result["embedded"] == 2
    assert result["remaining"] == 3


@pytest.mark.asyncio
async def test_a_failing_row_does_not_block_the_queue(backend, monkeypatch):
    """One bad row must not stop the rows behind it."""
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [
                _wire_row("m1", content="the broken one", sequence=1),
                _wire_row("m2", content="the good one", sequence=2),
            ],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:2"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)
    await fed.sync_peer(backend, peer["id"])

    from mnemos.domain.federation.local_embed import embed_pending_once

    async def flaky(text: str) -> list[float]:
        if "broken" in text:
            raise RuntimeError("embedder exploded")
        return [0.1, 0.2, 0.3]

    result = await embed_pending_once(backend, embed=flaky)

    assert result["failed"] == 1
    assert result["embedded"] == 1
    assert result["remaining"] == 1


@pytest.mark.asyncio
async def test_a_secret_is_redacted_before_the_text_reaches_the_embedder(backend, monkeypatch):
    """The embedder must never see raw secret text (F2b)."""
    await _pull_one_row(backend, monkeypatch, content="password: hunter2secret")

    from mnemos.domain.federation.local_embed import embed_pending_once

    seen: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        seen.append(text)
        return [0.1, 0.2, 0.3]

    await embed_pending_once(backend, embed=fake_embed)

    assert seen
    assert "hunter2secret" not in seen[0]


# ── Honest counters ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sync_counters_report_rows_waiting_for_an_embedding(backend, monkeypatch):
    """A row waiting for a vector is reported as waiting, not as embedded."""
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    await fed.sync_peer(backend, peer["id"])

    async with backend.transactional() as tx:
        log = await backend.federation.fetch_sync_log(tx, peer["id"], 10)

    assert log
    entry = log[0]
    assert entry["rows_without_embedding"] == 1, (
        "the sync must report that one row is still waiting for an embedding"
    )


@pytest.mark.asyncio
async def test_the_status_count_reports_rows_waiting_for_an_embedding(backend, monkeypatch):
    """``/v1/federation/status`` exposes the gap as rows_without_embedding."""
    await _pull_one_row(backend, monkeypatch)

    async with backend.transactional() as tx:
        without = await backend.federation.count_federated_rows_without_embedding_per_peer(tx)

    assert without.get(PEER) == 1


@pytest.mark.asyncio
async def test_the_queue_depth_is_reported_per_peer(backend, monkeypatch):
    """The queue depth can be read per peer, so an operator sees WHICH peer lags."""
    await _pull_one_row(backend, monkeypatch)

    async with backend.transactional() as tx:
        total = await backend.federation.count_federated_missing_embeddings(tx)
        per_peer = await backend.federation.count_federated_missing_embeddings(tx, peer_name=PEER)
        other = await backend.federation.count_federated_missing_embeddings(tx, peer_name="nobody")

    assert total == 1
    assert per_peer == 1
    assert other == 0
