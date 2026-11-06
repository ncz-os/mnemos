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
