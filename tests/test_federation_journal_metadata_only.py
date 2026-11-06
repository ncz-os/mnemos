"""Recall bookkeeping must not append federation journal events (0072).

Driven against a real SQLite file through the shipped backend, so the
triggers under test are the ones the migration actually installs -- not a copy
of their SQL. The other dialects ship the same predicate shape (see
``mnemos/db_migrations/migrations_*/0072_federation_journal_metadata_only.sql``)
but only SQLite is exercised here; the rest are covered by the static
cross-dialect assertions in ``test_federation_journal_metadata_only_static.py``
and remain unverified against a live server.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(
        tmp_path / "journal_metadata.db",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _exec(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def _rows(backend, sql, *params):
    async with backend.transactional() as tx:
        return list(await _Ops(tx, transaction_dialect(tx)).fetchall(sql, *params))


async def _journal(backend, memory_id):
    return await _rows(
        backend,
        "SELECT event_kind, new_namespace, new_exportable FROM federation_changes "
        "WHERE memory_id = ? ORDER BY event_id",
        memory_id,
    )


async def _seed(backend, memory_id="m1", namespace="A"):
    await _exec(
        backend,
        "INSERT INTO memories(id, content, category, owner_id, namespace, "
        "permission_mode, created, updated, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        memory_id,
        "original body",
        "facts",
        "alice",
        namespace,
        644,
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        "hash-" + memory_id,
    )


@pytest.mark.asyncio
async def test_recall_bump_appends_no_journal_event(backend):
    await _seed(backend)
    before = len(await _journal(backend, "m1"))

    for _ in range(5):
        await _exec(
            backend,
            "UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP WHERE id = ?",
            "m1",
        )

    assert len(await _journal(backend, "m1")) == before, "recall bookkeeping is metadata-only"


@pytest.mark.asyncio
async def test_content_edit_after_recalls_yields_exactly_one_event(backend):
    await _seed(backend)
    base = len(await _journal(backend, "m1"))
    await _exec(
        backend,
        "UPDATE memories SET recall_count = recall_count + 1 WHERE id = ?",
        "m1",
    )
    await _exec(backend, "UPDATE memories SET content = ? WHERE id = ?", "edited body", "m1")

    rows = (await _journal(backend, "m1"))[base:]
    assert [row["event_kind"] for row in rows] == ["content"], rows
    assert rows[0]["new_namespace"] == "A"
    assert rows[0]["new_exportable"] == 1


@pytest.mark.asyncio
async def test_namespace_move_still_journals(backend):
    await _seed(backend)
    base = len(await _journal(backend, "m1"))
    await _exec(backend, "UPDATE memories SET namespace = ? WHERE id = ?", "B", "m1")
    rows = (await _journal(backend, "m1"))[base:]
    assert [row["event_kind"] for row in rows] == ["content"], rows


@pytest.mark.asyncio
async def test_delete_still_journals(backend):
    await _seed(backend)
    base = len(await _journal(backend, "m1"))
    await _exec(backend, "DELETE FROM memories WHERE id = ?", "m1")
    rows = (await _journal(backend, "m1"))[base:]
    assert [row["event_kind"] for row in rows] == ["content"], rows


@pytest.mark.asyncio
async def test_same_value_write_appends_nothing(backend):
    await _seed(backend)
    base = len(await _journal(backend, "m1"))
    await _exec(backend, "UPDATE memories SET content = content WHERE id = ?", "m1")
    assert len(await _journal(backend, "m1")) == base


async def _pending_metadata_count(backend, memory_id):
    rows = await _rows(
        backend,
        "SELECT event_id FROM federation_changes WHERE memory_id = ? AND seq IS NULL AND event_kind = 'metadata'",
        memory_id,
    )
    return len(rows)


async def _queue_metadata_row(backend, memory_id="m1"):
    """Queue a pending metadata-only row the way the trigger would have.

    The trigger inserts none (that is the point of 0072), so tests that need
    one to be absorbed must create it directly.
    """
    await _exec(
        backend,
        "INSERT INTO federation_changes(memory_id, changed_at, old_public, "
        "old_exportable, new_public, new_exportable, event_kind) "
        "VALUES (?,?,0,0,0,1,'metadata')",
        memory_id,
        "2020-01-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_metadata_only_write_absorbs_pending_metadata_rows(backend):
    """The review's stated failure mode: delete of rows that never exist.

    Absorption targets rows *already queued* -- produced by an older node, a
    restored backup, or the pre-0072 trigger -- not rows the trigger inserts.
    A recall bump must therefore remove them, and appending nothing itself.
    """
    await _seed(backend)
    await _queue_metadata_row(backend)
    assert await _pending_metadata_count(backend, "m1") == 1

    await _exec(
        backend,
        "UPDATE memories SET recall_count = recall_count + 1 WHERE id = ?",
        "m1",
    )

    assert await _pending_metadata_count(backend, "m1") == 0, "pending metadata-only row was not absorbed"


@pytest.mark.asyncio
async def test_absorption_never_touches_published_or_content_rows(backend):
    """Absorption must only remove *pending metadata-only* rows."""
    await _seed(backend)
    await _queue_metadata_row(backend)
    # A second memory's pending metadata row, which must survive.
    await _seed(backend, memory_id="m2")
    await _queue_metadata_row(backend, "m2")
    # Mark m2's row published: absorption must not reach sequenced rows either.
    await _exec(
        backend,
        "UPDATE federation_changes SET seq = 7 WHERE memory_id = 'm2' AND event_kind = 'metadata'",
    )

    content_before = len(await _rows(backend, "SELECT event_id FROM federation_changes WHERE event_kind = 'content'"))

    await _exec(
        backend,
        "UPDATE memories SET recall_count = recall_count + 1 WHERE id = ?",
        "m1",
    )

    assert await _pending_metadata_count(backend, "m1") == 0
    assert await _pending_metadata_count(backend, "m2") == 0, "the m2 row was sequenced, not pending"
    published = await _rows(
        backend,
        "SELECT event_kind FROM federation_changes WHERE memory_id = 'm2' AND seq IS NOT NULL ORDER BY event_id",
    )
    assert published == [{"event_kind": "metadata"}], "a sequenced row must never be deleted"
    content_after = len(await _rows(backend, "SELECT event_id FROM federation_changes WHERE event_kind = 'content'"))
    assert content_after == content_before, "content events must never be absorbed"


@pytest.mark.asyncio
async def test_recall_between_edit_and_publish_does_not_drop_the_edit(backend):
    """The stale-entry risk the review describes, from the other direction."""
    await _seed(backend)
    await _exec(backend, "UPDATE memories SET content = ? WHERE id = ?", "edited body", "m1")
    await _exec(
        backend,
        "UPDATE memories SET recall_count = recall_count + 1 WHERE id = ?",
        "m1",
    )

    rows = await _journal(backend, "m1")
    assert [row["event_kind"] for row in rows] == ["content", "content"], rows


@pytest.mark.asyncio
async def test_null_transition_is_journalled_which_angle_brackets_would_miss(backend):
    """``IS NOT`` is deliberate: it is SQLite's null-safe inequality.

    A change *to* NULL (``verbatim_content = NULL``) is a real federated
    change, and ``<>`` evaluates to NULL for it — so a ``<>`` predicate would
    silently drop the event. This pins the transition that the naive
    "IS NOT is only for NULLs, use <>" advice would break.
    """
    await _seed(backend)
    await _exec(backend, "UPDATE memories SET verbatim_content = ? WHERE id = ?", "keep", "m1")
    before = len(await _journal(backend, "m1"))

    await _exec(backend, "UPDATE memories SET verbatim_content = NULL WHERE id = ?", "m1")

    after = await _journal(backend, "m1")
    assert len(after) == before + 1, "a non-NULL -> NULL content change must be journalled"
    assert after[-1]["event_kind"] == "content"


@pytest.mark.asyncio
async def test_seq_is_publishable_so_seq_is_null_is_the_pending_marker(backend):
    """``seq`` is not the primary key and is not always set.

    Every queued row starts with ``seq IS NULL`` and the publisher assigns it.
    If ``seq`` were never NULL the journal would have no pending rows at all,
    so the absorption predicate's ``seq IS NULL`` is what selects unpublished
    rows — not a filter that can never match.
    """
    await _seed(backend)
    rows = await _rows(
        backend,
        "SELECT event_id, seq FROM federation_changes WHERE memory_id = ?",
        "m1",
    )
    assert rows, "seed insert must queue a journal row"
    assert all(row["seq"] is None for row in rows), "queued rows are pending (seq IS NULL)"
    assert all(row["event_id"] is not None for row in rows), "event_id, not seq, is the identity"
