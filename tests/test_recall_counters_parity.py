"""Recall-counter parity (M3) and dead search-cache removal (M4).

M3 — the recall-frequency bump
-------------------------------
``/v1/memories/search`` bumps ``recall_count`` / ``last_recalled_at`` for
the rows it returned.  The bump used to be hand-written Postgres SQL
(``WHERE id = ANY($1::text[])``, ``now()``) executed through the
backend-neutral ``backend.transactional()`` seam.  On SQLite / MySQL /
MariaDB / Oracle / Db2 that statement raises; the route swallowed the
error, so **recall counters never moved on any backend except
Postgres**.

The fix routes the bump through the persistence ABC
(``backend.memories.bump_recall_counts``) and schedules it with the
lifecycle's tracked-background registry (``_schedule_background``)
instead of a bare ``asyncio.create_task`` that keeps no reference.

M4 — the dead search cache
--------------------------
The per-user search cache was wired to ``_lc._cache``, which the
lifecycle hard-wires to ``None``: there is no Redis in the runtime, so
every cache read missed and every cache write went nowhere.  What it did
NOT cost nothing: every search read the visibility epoch
(``_vis_epoch_current`` — a NATS-KV round-trip) and folded it into a
27-field cache key that was then thrown away.

These tests pin:
  * the SQLite parity contract (search → ``recall_count`` rises),
  * that the bump goes through the ABC and not through raw SQL,
  * that the bump is scheduled as a TRACKED background task,
  * that a search performs NO visibility-epoch read,
  * that the endpoint docstring no longer advertises response caching.
"""

from __future__ import annotations

import asyncio
import inspect
import typing
from collections.abc import Sequence
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence import SqliteBackend
from mnemos.persistence import sqlite as storage


# ── helpers ─────────────────────────────────────────────────────────────────


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


async def _empty_decay(_backend):
    return {}


def _hit_row(memory_id: str) -> dict:
    """A minimal searchable memory row for the fake backend."""
    return {
        "id": memory_id,
        "content": f"{memory_id} content",
        "category": "facts",
        "subcategory": None,
        "created": "2026-06-14T00:00:00+00:00",
        "updated": "2026-06-14T00:00:00+00:00",
        "metadata": {},
        "quality_rating": 75,
        "compressed_content": None,
        "verbatim_content": None,
        "owner_id": "admin",
        "group_id": None,
        "namespace": "default",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "archived_at": None,
        "consolidated_into": None,
        "recall_count": 0,
        "last_recalled_at": None,
    }


@pytest.fixture
def _no_decay(monkeypatch):
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)


def _sqlite_settings():
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=3))


async def _insert(backend, tx, memory_id: str, content: str) -> None:
    when = datetime.now(timezone.utc)
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=75,
        owner_id="admin",
        namespace="default",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=when,
        updated=when,
    )


async def _recall_count(backend, tx, memory_id: str) -> int:
    row = await storage._fetch_one(
        tx.conn,
        "SELECT recall_count FROM memories WHERE id = ?",
        (memory_id,),
    )
    if row is None:
        return -1
    return int(row["recall_count"] if isinstance(row, dict) else row[0])


# ── M3: the bump actually moves the counters on SQLite ─────────────────────


@pytest.mark.asyncio
async def test_sqlite_search_bumps_recall_count(tmp_path, monkeypatch, _no_decay):
    """A search over SQLite must raise ``recall_count`` on the hit rows.

    This is the parity test the bug report asks for.  On the base
    revision the bump issued Postgres-only SQL through the neutral
    transaction seam; SQLite rejected it, the route swallowed the
    error, and ``recall_count`` stayed at 0 forever.
    """
    backend = SqliteBackend(tmp_path / "recall-parity.sqlite3", _sqlite_settings())
    await backend.open()
    monkeypatch.setattr(memories_handler, "_backend_or_503", lambda: backend)
    monkeypatch.setattr(_lc, "_cache", None)
    monkeypatch.setattr(_lc, "_persistence_backend", backend)

    try:
        async with backend.transactional() as tx:
            await _insert(backend, tx, "mem-hit", "parsnip planted row")
            await _insert(backend, tx, "mem-miss", "unrelated zucchini row")
            assert await _recall_count(backend, tx, "mem-hit") == 0

        response = await memories_handler.search_memories(
            MemorySearchRequest(query="parsnip", semantic=False, min_score=0.0),
            user=_root(),
        )
        # The bump is fire-and-forget: let the scheduled task run.
        for _ in range(50):
            await asyncio.sleep(0)
            async with backend.transactional() as tx:
                if await _recall_count(backend, tx, "mem-hit") > 0:
                    break

        assert [m.id for m in response.memories] == ["mem-hit"]
        async with backend.transactional() as tx:
            assert await _recall_count(backend, tx, "mem-hit") == 1
            assert await _recall_count(backend, tx, "mem-miss") == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_bump_recall_counts_skips_deleted_and_archived(tmp_path):
    """The repository method must not bump soft-deleted / archived rows."""
    backend = SqliteBackend(tmp_path / "recall-skip.sqlite3", _sqlite_settings())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await _insert(backend, tx, "mem-live", "live row")
            await _insert(backend, tx, "mem-deleted", "deleted row")
            await _insert(backend, tx, "mem-archived", "archived row")
            await storage._execute(
                tx.conn,
                "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("mem-deleted",),
            )
            await storage._execute(
                tx.conn,
                "UPDATE memories SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("mem-archived",),
            )

            bumped = await backend.memories.bump_recall_counts(
                tx,
                ["mem-live", "mem-deleted", "mem-archived", "mem-absent"],
            )

            assert bumped == 1
            assert await _recall_count(backend, tx, "mem-live") == 1
            assert await _recall_count(backend, tx, "mem-deleted") == 0
            assert await _recall_count(backend, tx, "mem-archived") == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_bump_recall_counts_empty_id_set_is_a_noop(tmp_path):
    backend = SqliteBackend(tmp_path / "recall-empty.sqlite3", _sqlite_settings())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            assert await backend.memories.bump_recall_counts(tx, []) == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_bump_recall_counts_is_abc_level_not_route_sql(monkeypatch):
    """The route must call the repository, never hand-written SQL.

    The base revision called ``tx.conn.execute(...)`` with Postgres-only
    SQL from inside the route.  That is the defect: it works on exactly
    one backend and is swallowed on the other five.
    """
    from tests._fake_backend import install_fake_backend

    backend = install_fake_backend(monkeypatch)
    await memories_handler._bump_recall_counters(["mem_a", "mem_b"])

    names = [name for name, _kw in backend.memories.calls]
    assert "bump_recall_counts" in names
    assert "bump_recall_and_get_memory" not in names
    call = next(kw for name, kw in backend.memories.calls if name == "bump_recall_counts")
    assert call["memory_ids"] == ["mem_a", "mem_b"]


@pytest.mark.asyncio
async def test_bump_is_scheduled_as_a_tracked_background_task(monkeypatch):
    """The bump must be scheduled through ``_schedule_background``.

    A bare ``asyncio.create_task`` keeps no reference: the event loop
    may garbage-collect the task mid-flight (the bump silently never
    lands) and graceful shutdown cannot await it.

    This fails on the base revision, which calls
    ``asyncio.create_task(_bump_recall_counters(hit_ids))`` directly.
    """
    from tests._fake_backend import install_fake_backend

    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [_hit_row("mem_hit")])
    scheduled: list = []

    def _capture(coro):
        # Record the coroutine WITHOUT handing it to the real scheduler:
        # awaiting it here is enough to drain it and to observe that the
        # bump ran.
        scheduled.append(coro)
        return None

    monkeypatch.setattr(_lc, "_schedule_background", _capture)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    await memories_handler.search_memories(
        MemorySearchRequest(query="anything", semantic=False),
        user=_root(),
    )

    # The bump went through the lifecycle's tracked scheduler, not a
    # bare asyncio.create_task.
    assert len(scheduled) == 1
    # Drain the scheduled coroutine so no "never awaited" warning fires.
    for coro in scheduled:
        await coro

    # The search itself must not have called the bump inline; the
    # scheduled bump did.
    names = [name for name, _kw in backend.memories.calls]
    assert names.count("bump_recall_counts") == 1
    assert names.index("bump_recall_counts") > names.index("fts_search")


def test_search_handler_schedules_the_bump_through_the_lifecycle(monkeypatch):
    """Source-level pin: no bare ``asyncio.create_task`` for the bump."""
    source = inspect.getsource(memories_handler.search_memories)
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "asyncio.create_task" not in code
    assert "_schedule_background" in code


@pytest.mark.asyncio
async def test_bump_recall_counters_swallows_backend_failure(monkeypatch, caplog):
    """A failing bump must never break the search path.

    The base revision also swallowed failures, but it did so around
    hand-written SQL.  Here the failure comes from the repository, and
    the route must still swallow it (recall counters are observability,
    not user-content correctness).
    """
    from tests._fake_backend import install_fake_backend

    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_raise("bump_recall_counts", RuntimeError("boom"))

    # No exception expected.
    await memories_handler._bump_recall_counters(["mem_x"])

    names = [name for name, _kw in backend.memories.calls]
    assert "bump_recall_counts" in names


# ── M4: the dead search cache is gone ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_does_not_read_the_visibility_epoch(monkeypatch, _no_decay):
    """A search must NOT touch the epoch store at all.

    The epoch existed only to key the search cache.  With the cache
    gone, the per-search epoch read is a pure cost — a NATS-KV
    round-trip on every single search.  The read-only accessor
    (``_vis_epoch_current``) was removed with it, so an attempt to read
    -- by name or through the epoch store -- must never happen.
    """
    from tests._fake_backend import install_fake_backend

    install_fake_backend(monkeypatch)

    hits: list[str] = []

    class _EpochSpy:
        async def current(self) -> int:
            hits.append("current")
            return 0

        async def bump(self) -> int:
            hits.append("bump")
            return 1

    monkeypatch.setattr(_lc, "_vis_epoch", lambda: _EpochSpy())
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay)

    await memories_handler.search_memories(
        MemorySearchRequest(query="epoch probe", semantic=False),
        user=_root(),
    )

    assert hits == [], "search must not read or bump the visibility epoch"


def test_vis_epoch_current_accessor_is_gone():
    """The read-only epoch accessor existed only to key the search cache."""
    assert not hasattr(_lc, "_vis_epoch_current")
    source = inspect.getsource(_lc)
    assert "_vis_epoch_current" not in source


def test_get_cache_key_builder_is_gone():
    """The cache-key builder existed only for the removed search cache."""
    assert not hasattr(_lc, "_get_cache_key")
    source = inspect.getsource(_lc)
    assert "_get_cache_key" not in source
    assert "mnemos:search:" not in source, "the dead search-key namespace must not linger"


def test_search_endpoint_docstring_does_not_advertise_response_caching():
    """The docstring must not promise a 5-minute response cache."""
    doc = (inspect.getdoc(memories_handler.search_memories) or "").lower()
    assert "5-minute" not in doc
    assert "response caching" not in doc
    assert "cached" not in doc
    assert "caching" not in doc


def test_search_handler_source_has_no_cache_key_build():
    """The 27-field cache-key build must be gone from the handler."""
    source = inspect.getsource(memories_handler.search_memories)
    # Strip comments so a prose mention of the removed machinery does not
    # read as live code.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "_get_cache_key" not in code
    assert "_lc._cache" not in code
    assert "cache_key" not in code
    assert "setex" not in code


def test_invalidate_caches_after_mutation_has_no_search_scan():
    """The ``SCAN MATCH "mnemos:search:*"`` sweep is dead code.

    It only ever matched keys of a cache that never existed
    (``_lc._cache`` is hard-wired to ``None``).  The visibility-epoch
    bump it also performs must STAY: the MCP principal-context cache
    subscribes to it.
    """
    source = inspect.getsource(memories_handler._invalidate_caches_after_mutation)
    assert "scan_iter" not in source
    assert "_vis_epoch_get_incr" in source


# ── M3: the new ABC method is callable on every backend repository ─────────


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        # The ABC declares the contract the backends implement. It is first
        # so a broken annotation on the contract fails before any backend.
        ("mnemos.persistence.base", "MemoryRepository"),
        ("mnemos.persistence.sqlite", "SqliteMemoryRepository"),
        ("mnemos.persistence.postgres", "PostgresMemoryRepository"),
        ("mnemos.persistence.mysql", "MysqlMemoryRepository"),
        ("mnemos.persistence.mariadb", "MariadbMemoryRepository"),
        ("mnemos.persistence.oracle", "OracleMemoryRepository"),
        ("mnemos.persistence.db2", "Db2MemoryRepository"),
    ],
)
def test_bump_recall_counts_signature_resolves_on_every_backend(module_name, class_name):
    """``bump_recall_counts`` must be resolvable on the ABC and all six backends.

    ``typing.get_type_hints`` resolves each annotation BY NAME against the
    defining module's globals, so an annotation that referenced an
    unimported name raises ``NameError`` here. This pins the real contract
    the class bodies already depend on: ``Sequence`` comes from
    ``collections.abc`` (the convention used throughout
    ``mnemos/persistence/``), not ``typing``.

    It also proves MariaDB really is covered: it inherits the batch bump
    from ``MysqlMemoryRepository`` rather than declaring its own.
    """
    import importlib

    mod = importlib.import_module(module_name)
    repo_cls = getattr(mod, class_name)

    assert hasattr(repo_cls, "bump_recall_counts"), f"{class_name} cannot bump recall counts"

    hints = typing.get_type_hints(repo_cls.bump_recall_counts)
    assert hints["memory_ids"] == Sequence[str]
    assert "return" in hints and hints["return"] is int


@pytest.mark.asyncio
async def test_bump_recall_counts_executes_not_just_resolves(tmp_path):
    """Invoke the method for real: a missing ``Sequence`` name would blow up
    at CALL time, which a signature-only check would not catch."""
    backend = SqliteBackend(tmp_path / "recall-exec.sqlite3", _sqlite_settings())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await _insert(backend, tx, "mem-exec", "executor row")
            assert await backend.memories.bump_recall_counts(tx, ["mem-exec"]) == 1
            assert await _recall_count(backend, tx, "mem-exec") == 1
    finally:
        await backend.close()
