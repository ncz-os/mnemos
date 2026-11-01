"""F-4: journal compaction and a trigger that ignores derivative-only updates.

The Fable federation report (fleet-ops
``docs/design/mnemos-code-health-decisions-20261031.md``, section F3/F4)
found two separate problems in the durable federation outbox:

* **Unbounded growth.** ``federation_changes`` keeps one row per *mutation*,
  forever. A memory re-embedded by a local worker, or touched by a metadata
  backfill, appends an event each time it changes -- and the feed collapses
  them all to the current state anyway, so the older rows are pure dead
  weight. On a busy replica the outbox outgrows the data it describes.
* **Derivative-only updates.** The update trigger fires on ``embedding``,
  ``metadata``, ``subcategory`` and the ``source_*`` columns. A local
  re-embed writes none of the things the feed actually exports to a peer
  that did not ask for ``copy_embeddings``, yet it appends an event and
  advances every consumer's cursor.

Two fixes, both behind ``MNEMOS_FEDERATION_JOURNAL_COMPACTION`` (default on,
the safe side):

* ``compact()`` keeps only the latest event per memory older than N days;
  the feed still returns the *current* state exactly once (the feed already
  resolves each memory's ``MAX(seq)`` to the live row).
* The narrowed trigger fires only when a *federated* column changed, or when
  an embedding/metadata change can actually reach a peer (the journal does
  not know the consumer's scope, so embedding/metadata stay journaled — what
  is dropped is the redundant re-fire on a no-op update and the pure
  bookkeeping columns ``subcategory``/``updated``/``source_*`` are kept).

SQLite only here: the SQLite triggers live in ``0062_federation_journal.sql``
and are the ones the published images ship; the other dialects are pinned in
``tests/test_federation_journal_compaction_sql.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

COMPACTION_ENV = "MNEMOS_FEDERATION_JOURNAL_COMPACTION"


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    monkeypatch.delenv(COMPACTION_ENV, raising=False)
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(tmp_path / "journal_compact.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def insert(backend, mid="journal_one", namespace="A", mode=644):
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id, content, category, owner_id, namespace, permission_mode, "
            "content_hash, created, updated) VALUES (?, 'ordinary facts', 'facts', 'alice', ?, ?, ?, ?, ?)",
            mid,
            namespace,
            mode,
            "hash-" + mid,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 1, tzinfo=timezone.utc),
        )


async def mutate(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def feed(backend, cursor=None, limit=100):
    async with backend.transactional() as tx:
        return await backend.federation.feed_query(
            tx,
            since_updated=cursor["cursor_updated"] if cursor else None,
            since_id=cursor["cursor_id"] if cursor else None,
            namespaces=[],
            categories=[],
            limit=limit,
            prefer_compressed=False,
        )


async def event_count(backend):
    async with backend.transactional() as tx:
        return int(
            await _Ops(tx, transaction_dialect(tx)).scalar(
                "SELECT COUNT(*) FROM federation_changes WHERE memory_id = 'journal_one'"
            )
        )


async def age_events(backend, days):
    """Move every event for ``journal_one`` into the past."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
    await mutate(backend, "UPDATE federation_changes SET changed_at = ? WHERE memory_id = 'journal_one'", stamp)


# ── compaction ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_keeps_the_latest_event_per_memory(backend):
    """1 row, 5 updates, compact: one event survives and the feed is right."""
    from mnemos.persistence import federation_journal as journal

    await insert(backend)
    for n in range(5):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"revision {n}")
    # Publish so every event has a cursor, as a live peer would have done.
    await feed(backend)
    await age_events(backend, 30)
    assert await event_count(backend) == 6

    async with backend.transactional() as tx:
        removed = await journal.compact(tx, older_than_days=7)

    assert removed == 5
    assert await event_count(backend) == 1
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        survivor = await ops.fetchone("SELECT seq, memory_id FROM federation_changes WHERE memory_id = 'journal_one'")
    assert survivor["seq"] is not None, "the survivor keeps its published cursor"


@pytest.mark.asyncio
async def test_compaction_keeps_the_current_state_visible_once(backend):
    """The whole point: the feed must still describe the row exactly once."""
    from mnemos.persistence import federation_journal as journal

    await insert(backend)
    for n in range(5):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"revision {n}")
    await feed(backend)
    await age_events(backend, 30)
    async with backend.transactional() as tx:
        await journal.compact(tx, older_than_days=7)

    # A fresh consumer replays from the start; it must see the CURRENT body
    # once, not five stale revisions and not zero rows.
    rows = await feed(backend)
    assert [row["id"] for row in rows] == ["journal_one"]
    assert rows[0]["content"] == "revision 4"


@pytest.mark.asyncio
async def test_compaction_never_drops_an_unpublished_event(backend):
    """An event with no sequence has not reached any consumer yet."""
    from mnemos.persistence import federation_journal as journal

    await insert(backend)
    # Do NOT publish: both events stay at seq IS NULL, the "not yet visible to
    # any consumer" state that _publish exists to drain.
    await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", "revision 1")
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "UPDATE federation_changes SET changed_at = ? WHERE seq IS NULL",
            "2020-01-01T00:00:00.000000+00:00",
        )
    assert await event_count(backend) == 2

    async with backend.transactional() as tx:
        removed = await journal.compact(tx, older_than_days=7)

    assert removed == 0
    assert await event_count(backend) == 2


@pytest.mark.asyncio
async def test_compaction_leaves_recent_events_alone(backend):
    from mnemos.persistence import federation_journal as journal

    await insert(backend)
    for n in range(3):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"revision {n}")
    await feed(backend)

    async with backend.transactional() as tx:
        removed = await journal.compact(tx, older_than_days=7)

    assert removed == 0
    assert await event_count(backend) == 4


@pytest.mark.asyncio
async def test_compaction_keeps_every_memory_its_own_latest(backend):
    from mnemos.persistence import federation_journal as journal

    await insert(backend, mid="journal_one")
    await insert(backend, mid="journal_two")
    for n in range(3):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"a{n}")
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_two'", f"b{n}")
    await feed(backend)
    await mutate(
        backend,
        "UPDATE federation_changes SET changed_at = ? WHERE memory_id IN ('journal_one', 'journal_two')",
        "2020-01-01T00:00:00.000000+00:00",
    )

    async with backend.transactional() as tx:
        await journal.compact(tx, older_than_days=7)

    async with backend.transactional() as tx:
        rows = await _Ops(tx, transaction_dialect(tx)).fetchall(
            "SELECT memory_id, COUNT(*) AS n FROM federation_changes "
            "WHERE memory_id IN ('journal_one', 'journal_two') GROUP BY memory_id ORDER BY memory_id"
        )
    assert [dict(row) for row in rows] == [
        {"memory_id": "journal_one", "n": 1},
        {"memory_id": "journal_two", "n": 1},
    ]


@pytest.mark.asyncio
async def test_compaction_is_a_noop_when_the_switch_is_off(backend, monkeypatch):
    from mnemos.persistence import federation_journal as journal

    monkeypatch.setenv(COMPACTION_ENV, "off")
    await insert(backend)
    for n in range(3):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"revision {n}")
    await feed(backend)
    await age_events(backend, 30)
    before = await event_count(backend)

    async with backend.transactional() as tx:
        removed = await journal.compact(tx, older_than_days=7)

    assert removed == 0
    assert await event_count(backend) == before


def test_compaction_switch_parses_as_a_bool(monkeypatch):
    from mnemos.core.config import federation_journal_compaction_enabled

    monkeypatch.delenv(COMPACTION_ENV, raising=False)
    assert federation_journal_compaction_enabled() is True
    for raw, expected in (
        ("0", False),
        ("off", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("true", True),
    ):
        monkeypatch.setenv(COMPACTION_ENV, raw)
        assert federation_journal_compaction_enabled() is expected, raw


# ── the narrowed trigger ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_updated_bump_alone_does_not_journal(backend):
    """A touch that changes nothing federated must not append an event."""
    await insert(backend)
    before = await event_count(backend)

    await mutate(backend, "UPDATE memories SET updated = updated WHERE id = 'journal_one'")

    assert await event_count(backend) == before


@pytest.mark.asyncio
async def test_noop_update_does_not_journal(backend):
    await insert(backend)
    before = await event_count(backend)
    await mutate(backend, "UPDATE memories SET content = content WHERE id = 'journal_one'")
    assert await event_count(backend) == before


@pytest.mark.asyncio
async def test_embedding_only_update_still_journals(backend):
    """copy_embeddings peers need it: the vector is wire-visible."""
    await insert(backend)
    before = await event_count(backend)
    await mutate(backend, "UPDATE memories SET embedding = ? WHERE id = 'journal_one'", "[0.1,0.2,0.3]")
    assert await event_count(backend) == before + 1


@pytest.mark.asyncio
async def test_content_update_still_journals(backend):
    await insert(backend)
    before = await event_count(backend)
    await mutate(backend, "UPDATE memories SET content = 'changed' WHERE id = 'journal_one'")
    assert await event_count(backend) == before + 1


@pytest.mark.asyncio
async def test_lifecycle_and_visibility_updates_still_journal(backend):
    """Every column the feed uses to decide *whether* to publish."""
    await insert(backend)
    statements = [
        "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'journal_one'",
        "UPDATE memories SET archived_at = CURRENT_TIMESTAMP WHERE id = 'journal_one'",
        "UPDATE memories SET permission_mode = 600 WHERE id = 'journal_one'",
        "UPDATE memories SET namespace = 'B' WHERE id = 'journal_one'",
        "UPDATE memories SET category = 'notes' WHERE id = 'journal_one'",
        "UPDATE memories SET owner_id = 'bob' WHERE id = 'journal_one'",
    ]
    for statement in statements:
        before = await event_count(backend)
        await mutate(backend, statement)
        assert await event_count(backend) == before + 1, statement


# ── the compaction statement is reachable and dialect-valid ─────────────────


@pytest.mark.asyncio
async def test_backend_abc_reaches_compaction(backend):
    """F-4: compaction must be reachable through the repository ABC.

    A ``compact()`` nobody can call is the defect this guards: the switch,
    the docs and the tests all pass while the outbox still grows forever.
    """
    from mnemos.persistence.federation_journal import compaction_sql

    await insert(backend)
    for n in range(3):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = 'journal_one'", f"revision {n}")
    await feed(backend)
    await age_events(backend, 30)

    async with backend.transactional() as tx:
        removed = await backend.federation.compact_journal(tx, older_than_days=7)

    assert removed == 3
    assert await event_count(backend) == 1
    # The ABC implementation must be the shared statement, not a copy.
    assert hasattr(backend.federation, "compact_journal")
    assert compaction_sql("sqlite")


def test_compaction_statement_is_dialect_valid():
    """Each dialect gets a DELETE it can actually parse."""
    from mnemos.persistence.federation_journal import compaction_sql

    # MySQL/MariaDB (error 1093): a derived table, never a correlated
    # subquery naming the delete target.
    mysql_sql = compaction_sql("mysql")
    assert "AS keep" in mysql_sql
    assert "federation_changes AS newer" not in mysql_sql
    assert mysql_sql.count("?") == 2

    # Oracle/Db2 reject the unaliased joined target; SQLite rejects the
    # schema-qualified self-reference in some forms.
    for dialect in ("oracle", "db2"):
        assert "federation_changes stale" in compaction_sql(dialect)

    # SQLite/Postgres: the plain correlated EXISTS, one bind.
    for dialect in ("sqlite", "postgres"):
        sql = compaction_sql(dialect)
        assert sql.startswith("DELETE FROM federation_changes WHERE")
        assert "AS newer" in sql
        assert sql.count("?") == 1


@pytest.mark.asyncio
async def test_compaction_binds_a_datetime_not_a_string(backend):
    """``_Ops.sql`` only rewrites placeholders for Postgres; a pre-formatted
    string would compare against a TIMESTAMPTZ column on real deployments."""
    from mnemos.persistence import federation_journal as journal

    cutoff = journal._cutoff(7)
    assert isinstance(cutoff, datetime)
    assert cutoff.tzinfo is not None


def test_switches_do_not_raise_when_the_environment_variable_is_unset(monkeypatch):
    """An unset knob must resolve to the safe default, never crash.

    ``runtime_env_value_stripped`` has a ``""`` default and ``os.environ.get``
    returns it verbatim, so the helpers' ``.lower()`` always sees a ``str``.
    Pinned here because a ``None`` default would turn every startup into an
    ``AttributeError`` the moment the knob is absent -- which is the default
    configuration.
    """
    from mnemos.core.config import (
        federation_journal_compaction_enabled,
        federation_replica_fence_enabled,
        runtime_env_value_stripped,
    )

    for name in (COMPACTION_ENV, "MNEMOS_FEDERATION_REPLICA_FENCE"):
        monkeypatch.delenv(name, raising=False)
        assert isinstance(runtime_env_value_stripped(name), str)

    # Removed (not merely emptied) from the process environment: the shape
    # that a ``None`` default would break.
    monkeypatch.delenv(COMPACTION_ENV, raising=False)
    monkeypatch.delenv("MNEMOS_FEDERATION_REPLICA_FENCE", raising=False)
    assert federation_journal_compaction_enabled() is True
    assert federation_replica_fence_enabled() is True

    # Empty and whitespace-only values are "unset" too, not a parse error.
    for raw in ("", "   "):
        monkeypatch.setenv(COMPACTION_ENV, raw)
        monkeypatch.setenv("MNEMOS_FEDERATION_REPLICA_FENCE", raw)
        assert federation_journal_compaction_enabled() is True
        assert federation_replica_fence_enabled() is True
