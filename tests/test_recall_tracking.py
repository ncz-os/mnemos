"""Tests for the recall-frequency bump in /v1/memories/search.

The bump is fire-and-forget on the search hot path:
  - One UPDATE for the whole hit set, not per-memory
  - Failures logged + swallowed (observability, not correctness)
  - Skipped on empty hits or missing pool

These tests pin the SQL shape and the failure-mode semantics so a
future refactor can't silently turn the bump into N round-trips or
a search-blocking exception.

The UPDATE is issued by the persistence repository
(``backend.memories.bump_recall_counts``), NOT by hand-written SQL in
the route. The previous route-level implementation ran Postgres-only
SQL (``WHERE id = ANY($1::text[])``, ``now()``) through the
backend-neutral ``backend.transactional()`` seam: on SQLite / MySQL /
MariaDB / Oracle / Db2 that statement raises, the error is swallowed,
and recall counters never move on any backend except Postgres.

The end-to-end parity proof (search → ``recall_count`` rises on
SQLite) lives in ``tests/test_recall_counters_parity.py``.
"""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.routes.memories import _bump_recall_counters


class _Conn:
    def __init__(self, raise_on_execute: bool = False):
        self.executed: List[Tuple[str, tuple]] = []
        self.raise_on_execute = raise_on_execute

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if self.raise_on_execute:
            raise RuntimeError("simulated DB error")
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn: _Conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool._conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


class _FakeMemoryRepo:
    """Records the batched recall bump the route delegates to."""

    def __init__(self, raise_on_bump: bool = False):
        self.bumped: List[List[str]] = []
        self.raise_on_bump = raise_on_bump

    async def bump_recall_counts(self, tx, memory_ids):
        if self.raise_on_bump:
            raise RuntimeError("simulated DB error")
        self.bumped.append(list(memory_ids))
        return len(memory_ids)


class _FakeBackend:
    def __init__(self, repo: _FakeMemoryRepo):
        self.memories = repo

    def transactional(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _tx():
            yield object()

        return _tx()


@pytest.fixture
def stub_pool(monkeypatch):
    holder: dict = {}

    def install(raise_on_execute: bool = False) -> _FakeMemoryRepo:
        conn = _Conn(raise_on_execute=raise_on_execute)
        monkeypatch.setattr(_lc, "_pool", _Pool(conn))
        repo = _FakeMemoryRepo(raise_on_bump=raise_on_execute)
        monkeypatch.setattr(_lc, "_persistence_backend", _FakeBackend(repo))
        holder["repo"] = repo
        holder["conn"] = conn
        return repo

    return install


@pytest.mark.asyncio
async def test_bump_uses_single_update_for_hit_set(stub_pool):
    """Five hits should produce one batched bump, not five."""
    repo = stub_pool()
    await _bump_recall_counters(["mem_1", "mem_2", "mem_3", "mem_4", "mem_5"])
    assert len(repo.bumped) == 1
    assert repo.bumped[0] == ["mem_1", "mem_2", "mem_3", "mem_4", "mem_5"]


@pytest.mark.asyncio
async def test_bump_skips_empty_hit_set(stub_pool):
    """Search with zero hits → no DB roundtrip."""
    repo = stub_pool()
    await _bump_recall_counters([])
    assert repo.bumped == []


@pytest.mark.asyncio
async def test_bump_swallows_db_failure(stub_pool):
    """A DB failure during the bump must NOT propagate — the search
    response has already gone out; the bump is observability, not
    correctness."""
    repo = stub_pool(raise_on_execute=True)
    # No exception expected.
    await _bump_recall_counters(["mem_x"])
    assert len(repo.bumped) == 0


@pytest.mark.asyncio
async def test_bump_skips_when_pool_missing(monkeypatch):
    """Search before pool is initialised (rare but possible during
    boot) shouldn't crash the search path."""
    monkeypatch.setattr(_lc, "_pool", None)
    monkeypatch.setattr(_lc, "_persistence_backend", None)
    # No exception expected.
    await _bump_recall_counters(["mem_x"])


@pytest.mark.asyncio
async def test_bump_passes_list_not_iterator(stub_pool):
    """Ensure the helper's `list(memory_ids)` cast survives — passing
    a generator should still work end-to-end."""
    repo = stub_pool()

    def _gen():
        yield "mem_a"
        yield "mem_b"

    await _bump_recall_counters(list(_gen()))
    assert repo.bumped[0] == ["mem_a", "mem_b"]
