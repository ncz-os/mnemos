"""Regression tests for the write-after-invalidate (TOCTOU) race fix.

Bug history: per-user search responses (``mnemos:search:*``) were invalidated
post-commit on visibility-narrowing mutations. An in-flight search request
that had already read rows from the DB BEFORE the mutation committed would
then write its stale result under the search key AFTER the invalidation
deleted it. The stale entry would live until the search TTL expired (300s
fast/balanced, 30s deep), giving a principal whose read access was just
narrowed a window of up to TTL seconds to still see rows they should no
longer be able to read.

The fix (mnemos issue #1) folds a monotonic visibility/ACL epoch
(``mnemos:vis:epoch``) into every search cache key. Each search request
captures the epoch BEFORE its DB query; any in-flight write can only land
under its old-epoch key, which is unreachable by future searches (their
key includes the bumped epoch). The best-effort SCAN/DELETE cleanup is
retained for cache hygiene and for the pre-existing invalidation
contract.

These tests cover both layers:

* lifecycle-level: ``get_visibility_epoch`` / ``invalidate_visibility_caches``
  semantics, including the ``None`` fail-closed latch
* handler-level: ``search_memories`` actually folds the captured epoch into
  the cache key for both reads and writes
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.core import lifecycle as _lc
from mnemos.domain.models import MemorySearchRequest

from tests._fake_backend import install_fake_backend


_TS = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeCache:
    """Minimal aioredis-like cache used to exercise the visibility helper.

    Implements only what ``get_visibility_epoch`` and
    ``invalidate_visibility_caches`` call. The `_epoch` attribute tracks
    the counter value with the same atomic semantics ``INCR`` provides:
    `incr` always returns the post-increment value, even if Python's
    GIL-guarded single-threaded run races, because each call awaits the
    asyncio lock implicit in ``await yield``.
    """

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []
        self.scan_calls: list[tuple[str, int]] = []
        self._epoch = 0
        self._raise_on = raise_on or set()

    async def get(self, key: str):
        if key in self._raise_on:
            raise RuntimeError(f"boom on get({key})")
        return self.store.get(key)

    async def incr(self, key: str) -> int:
        if key in self._raise_on:
            raise RuntimeError(f"boom on incr({key})")
        self._epoch += 1
        # Simulate Redis INCR materialization: the counter is also a
        # key in the same store, not a separate variable.
        self.store[key] = str(self._epoch)
        return self._epoch

    async def set(self, key: str, value: str) -> None:
        if key in self._raise_on:
            raise RuntimeError(f"boom on set({key})")
        self.store[key] = value

    async def delete(self, key: str) -> None:
        if key in self._raise_on:
            raise RuntimeError(f"boom on delete({key})")
        self.deleted.append(key)
        self.store.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        self.scan_calls.append((match, count))
        prefix = match.rstrip("*")
        for key in list(self.store):
            if match.endswith("*") and key.startswith(prefix):
                yield key


def _install_cache(monkeypatch, cache):
    """Swap the lifecycle cache singleton for the test fake."""
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)
    # Also reset the untrusted latch so previous tests cannot leak
    # the bypass-everything state into this one.
    monkeypatch.setattr(_lc, "_epoch_untrusted", False, raising=False)


def _sample_search_key(epoch, user_id="alice", namespace="default", query="q1"):
    """Mirror the lifecycle._get_cache_key contract used by search_memories."""
    return f"mnemos:search:{epoch}:{user_id}:{namespace}:{query}"


@pytest.mark.asyncio
async def test_stale_write_lands_under_obsolete_epoch_and_is_never_read(monkeypatch):
    """Repro the issue-#1 race: search starts, mutation commits mid-flight,
    in-flight search caches under OLD epoch.

    Asserts:
      * new searches compute a NEW-epoch cache key (never the OLD one)
      * a fresh search MISSES the OLD entry (it was never written under
        that key)
      * the stale OLD entry is still isolated and is in fact garbage
        collected by the post-mutation SCAN sweep
    """
    cache = _FakeCache()
    _install_cache(monkeypatch, cache)

    # 1. Search starts. Captures epoch = 0 (initial state, key absent).
    stale_epoch = await _lc.get_visibility_epoch()
    assert stale_epoch == 0
    stale_key = _sample_search_key(stale_epoch)

    # 2. Visibility-narrowing mutation commits and advances the epoch.
    await _lc.invalidate_visibility_caches()
    post_mutation_epoch = await _lc.get_visibility_epoch()
    assert post_mutation_epoch == 1
    assert stale_epoch != post_mutation_epoch

    # 3. The in-flight search would now write its stale payload under
    #    the OLD-epoch key. (We simulate this directly because the
    #    full handler is exercised by the integration path.)
    await cache.set(stale_key, '{"count":0,"memories":[]}')

    # 4. A NEW search request computes a NEW-epoch key and never reads
    #    the obsolete epoch's entry.
    fresh_epoch = await _lc.get_visibility_epoch()
    assert fresh_epoch == 1
    fresh_key = _sample_search_key(fresh_epoch)
    assert fresh_key != stale_key
    cached = await cache.get(fresh_key)
    assert cached is None  # the stale write is unreachable

    # 5. The hygiene SCAN sweep does still reclaim the stranded stale
    #    entry — visible to operators and protects against unbounded
    #    growth if search traffic ever bursts under heavy invalidation.
    assert stale_key in cache.store
    await _lc.invalidate_visibility_caches()
    assert stale_key not in cache.store


@pytest.mark.asyncio
async def test_in_flight_stale_write_is_never_served_after_mutation(monkeypatch):
    """End-to-end race window.

    1. Search A captures epoch (misses cache).
    2. Mutation deletes current ``mnemos:search:*`` keys AND bumps epoch.
    3. Search A writes its now-stale payload under its OLD-epoch key.
    4. Search B captures new epoch and queries the cache: must MISS the
       stale payload. (A pre-fix implementation had a single shared key
       namespace, so a successful ``get`` here would replay a hit the
       principal should no longer be authorized to see.)
    """
    cache = _FakeCache()
    _install_cache(monkeypatch, cache)

    # Step 1: search A captures the visible epoch and does a miss.
    epoch_a = await _lc.get_visibility_epoch()
    assert epoch_a == 0
    key_a = _sample_search_key(epoch_a)
    assert await cache.get(key_a) is None  # miss, as expected

    # Step 2: mutation commits (e.g. ACL revoke / permission_mode tighten).
    await _lc.invalidate_visibility_caches("stats:global")
    epoch_after = await _lc.get_visibility_epoch()
    assert epoch_after == 1
    assert "stats:global" in cache.deleted

    # Step 3: search A, still in flight, writes its (stale) result.
    stale_payload = '{"count":1,"memories":[{"id":"m_secret"}]}'
    await cache.set(key_a, stale_payload)

    # Step 4: search B lands after the mutation. It MUST NOT read the
    # obsolete entry even though the SCAN sweep may be slow (we
    # explicitly DIDN'T call invalidate_visibility_caches again between
    # steps 3 and 4 to model the worst case where the SCAN reclaimed
    # nothing yet).
    epoch_b = await _lc.get_visibility_epoch()
    key_b = _sample_search_key(epoch_b)
    assert epoch_a != epoch_b
    assert key_a != key_b
    served = await cache.get(key_b)
    assert served is None  # search B misses the cache entirely

    # And the stale write is still stranded under key_a — the
    # unreachable key. It is not just "missing", it is keyed differently.
    assert key_a in cache.store
    assert await cache.get(key_a) == stale_payload


@pytest.mark.asyncio
async def test_incr_failure_disables_caching_until_next_successful_advance(monkeypatch):
    """Fail-closed: if Redis cannot advance the epoch, every subsequent
    epoch read short-circuits to None so handlers bypass cache reads
    and writes.

    Without this guard, a partial outage could leave the cache in a state
    where pre-outage entries still resolve under their old epoch key and
    post-outage entries accumulate under a key whose read returns None,
    silently serving stale data again.
    """
    cache = _FakeCache(raise_on={_lc.VISIBILITY_EPOCH_KEY})
    _install_cache(monkeypatch, cache)

    # First advance attempt fails.
    await _lc.invalidate_visibility_caches()
    assert _lc._epoch_untrusted is True

    # Reads fail closed until a future mutation succeeds.
    assert await _lc.get_visibility_epoch() is None
    assert await _lc.get_visibility_epoch() is None

    # Drop the simulated broker outage; the next mutation succeeds and
    # re-arms the read path.
    cache._raise_on.discard(_lc.VISIBILITY_EPOCH_KEY)
    await _lc.invalidate_visibility_caches()
    assert _lc._epoch_untrusted is False
    # Fresh epoch recovered and visible.
    epoch = await _lc.get_visibility_epoch()
    assert epoch == 1


@pytest.mark.asyncio
async def test_invalidate_passes_through_stats_keys_and_search_pattern(monkeypatch):
    """The retention of the pre-existing invalidation contract: callers
    can ask for named stats keys to be deleted alongside the epoch
    bump, and the ``mnemos:search:*`` SCAN/DELETE sweep still runs."""
    cache = _FakeCache()
    _install_cache(monkeypatch, cache)
    cache.store["stats:global"] = "{}"
    cache.store["stats:global:v2"] = "{}"
    cache.store["mnemos:search:foo"] = "{}"
    cache.store["mnemos:search:bar"] = "{}"
    cache.store["mnemos:webhook:outbox"] = "{}"  # unrelated, must be kept

    await _lc.invalidate_visibility_caches("stats:global", "stats:global:v2")

    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted
    assert "mnemos:search:foo" in cache.deleted
    assert "mnemos:search:bar" in cache.deleted
    # Out-of-pattern keys must NOT be touched by the search sweep.
    assert "mnemos:webhook:outbox" not in cache.deleted
    assert "mnemos:webhook:outbox" in cache.store
    # The epoch bump itself left the right artifact.
    assert cache.store[_lc.VISIBILITY_EPOCH_KEY] == "1"


@pytest.mark.asyncio
async def test_get_visibility_epoch_handles_missing_key_as_initial_generation(monkeypatch):
    """A never-bumped Redis (fresh install) reports generation zero, NOT
    ``None``. Treating 0 as "untrustworthy" would force every request on
    a fresh install to bypass the search cache and dominate the DB on
    cold starts."""
    cache = _FakeCache()
    _install_cache(monkeypatch, cache)
    assert await _lc.get_visibility_epoch() == 0
    assert _lc._epoch_untrusted is False


# ── Handler-level race-coverage tests ─────────────────────────────────────────
# The lifecycle-only tests above prove the helpers behave correctly in
# isolation. But the actual race is a property of the SEARCH handler
# combining those helpers with the cache: an in-flight search that captures
# epoch=N must (a) read the cache under that epoch and (b) write its result
# under that same epoch. Without those invariants, the fix is tautological:
# the helpers would never even be consulted by the production code path.


class _RecordingCache:
    """Cache fake that captures every key the handler computes.

    Production code calls ``get(key)`` then later ``setex(key, ttl,
    value)``. Both must reference the SAME key the handler passed on
    cache lookup time, which itself must include the captured
    visibility epoch.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []
        self.scan_calls: list[tuple[str, int]] = []
        self.reads: list[str] = []
        self.writes: list[tuple[str, int, str]] = []  # (key, ttl, value)
        self.epoch = 0

    async def get(self, key: str):
        self.reads.append(key)
        if key == _lc.VISIBILITY_EPOCH_KEY:
            return str(self.epoch) if self.epoch else None
        return self.store.get(key)

    async def incr(self, key: str) -> int:
        self.epoch += 1
        self.store[key] = str(self.epoch)
        return self.epoch

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.writes.append((key, ttl, value))
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        self.scan_calls.append((match, count))
        prefix = match.rstrip("*")
        for key in list(self.store):
            if match.endswith("*") and key.startswith(prefix):
                yield key

    async def set(self, key: str, value: str) -> None:
        # Production code uses setex, but the lifecycle helpers occasionally
        # touch the counter as a regular set; tolerate both shapes.
        if key == _lc.VISIBILITY_EPOCH_KEY:
            return  # ignore; epoch is materialized by incr()
        self.store[key] = value


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _memory_row(memory_id: str, content: str) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "category": "facts",
        "subcategory": None,
        "created": _TS,
        "updated": _TS,
        "metadata": {},
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": content,
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


async def _noop_bump_recall_counters(_memory_ids: list[str]) -> None:
    return None


async def _empty_decay_table(_backend) -> dict:
    return {}


@pytest.mark.asyncio
async def test_search_memories_key_stable_within_an_epoch(monkeypatch):
    """Cache key construction is deterministic for a given visibility
    epoch. Two consecutive searches with identical request shapes and
    no intervening mutation must read/write the SAME key."""
    # IMPORTANT: install_fake_backend() resets ``_lc._cache`` to None,
    # so the cache fake must be installed AFTER the backend is wired up.
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [])
    cache = _RecordingCache()
    _install_cache(monkeypatch, cache)

    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    request = MemorySearchRequest(query="needle", limit=10, semantic=False)
    user = _alice()

    await memories_handler.search_memories(request, user=user)
    await memories_handler.search_memories(request, user=user)

    # Both searches ran in the same epoch → identical cache key for the
    # read attempt and for the write.
    assert cache.reads, "search_memories must consult the cache"
    assert cache.writes, "search_memories must write to the cache"
    read_keys = [k for k in cache.reads if k != _lc.VISIBILITY_EPOCH_KEY]
    written_keys = {entry[0] for entry in cache.writes}
    assert len(set(read_keys)) == 1, f"cache read keys diverged: {read_keys}"
    assert read_keys[0] in written_keys, (
        "cache read key must equal a written key for the same epoch"
    )


@pytest.mark.asyncio
async def test_search_memories_key_changes_after_mutation_bumps_epoch(monkeypatch):
    """After a visibility-narrowing mutation the next search must compute
    a NEW cache key (epoch=N+1) and not collide with the prior epoch's key.

    This is the core invariant of the fix at the handler layer: without
    it, the issue-#1 race returns.
    """
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [])
    cache = _RecordingCache()
    _install_cache(monkeypatch, cache)

    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    request = MemorySearchRequest(query="needle", limit=10, semantic=False)
    user = _alice()

    await memories_handler.search_memories(request, user=user)
    pre_mutation_keys = {entry[0] for entry in cache.writes}
    assert pre_mutation_keys

    # Visibility-narrowing mutation: drive the same invalidation the
    # memories route uses after a delete/update/archive.
    await memories_handler._invalidate_caches_after_mutation()

    cache.writes.clear()
    await memories_handler.search_memories(request, user=user)
    post_mutation_keys = {entry[0] for entry in cache.writes}
    assert post_mutation_keys

    assert pre_mutation_keys.isdisjoint(post_mutation_keys), (
        f"post-mutation cache key must not reuse the pre-mutation key "
        f"(pre={pre_mutation_keys}, post={post_mutation_keys})"
    )


@pytest.mark.asyncio
async def test_search_memories_bypasses_cache_when_epoch_untrusted(monkeypatch):
    """If the visibility epoch is untrusted (previous INCR failed), no cache
    read or write should occur: the read would be meaningless (no key encodes
    the trustworthy generation) and the write would be unretrievable later.
    """
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [])
    cache = _RecordingCache()
    _install_cache(monkeypatch, cache)
    # Force the untrusted latch without mocking individual methods.
    monkeypatch.setattr(_lc, "_epoch_untrusted", True, raising=False)
    # Even though the cache exists, get_visibility_epoch() will short-circuit
    # to None and the handler must skip cache interaction entirely.

    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    request = MemorySearchRequest(query="needle", limit=10, semantic=False)
    user = _alice()
    await memories_handler.search_memories(request, user=user)

    search_reads = [k for k in cache.reads if k != _lc.VISIBILITY_EPOCH_KEY]
    assert search_reads == [], (
        f"handler must not read from cache when epoch is untrusted, got {search_reads}"
    )
    assert cache.writes == [], (
        f"handler must not write to cache when epoch is untrusted, got {cache.writes}"
    )


@pytest.mark.asyncio
async def test_search_memories_write_uses_captured_epoch_not_post_query_epoch(monkeypatch):
    """The cache write must use the SAME epoch as the cache read.

    We verify this by bumping the epoch between the read and the write
    and asserting the handler still wrote under the epoch captured at
    the top of the function (i.e. the key uses the pre-bump epoch and
    the bumped epoch is never written by this request).
    """
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [])
    cache = _RecordingCache()
    _install_cache(monkeypatch, cache)

    monkeypatch.setattr(memories_handler, "_bump_recall_counters", _noop_bump_recall_counters)
    monkeypatch.setattr(memories_handler, "load_decay_table", _empty_decay_table)

    # Wrap the DB call site so the epoch is advanced AFTER the search's
    # cache READ but BEFORE the cache WRITE. This is the precise ordering
    # the race exploits.
    real_fts_search = backend.memories.fts_search

    async def _fts_then_poke_epoch(*args, **kwargs):
        # Advance the visibility epoch AFTER any preceding cache read but
        # BEFORE the handler writes. In production this would be a
        # concurrent ACL revoke / archive / permission_mode tighten.
        await _lc.invalidate_visibility_caches()
        return await real_fts_search(*args, **kwargs)

    backend.memories.fts_search = _fts_then_poke_epoch  # type: ignore[method-assign]

    request = MemorySearchRequest(query="needle", limit=10, semantic=False)
    user = _alice()
    response = await memories_handler.search_memories(request, user=user)

    # The handler's captured epoch was 0 (initial). After the in-flight
    # mutation, the in-memory store now has epoch=1, but the handler
    # wrote under the epoch-0 key because it captured epoch=0 up front.
    search_reads = [k for k in cache.reads if k != _lc.VISIBILITY_EPOCH_KEY]
    written_keys = {entry[0] for entry in cache.writes}
    assert search_reads, "handler should have attempted the cache read"
    assert written_keys, "handler should have written to the cache"
    # The write key is the same as the read key (same captured epoch,
    # ignoring the bump that happened mid-request).
    for wkey, _ttl, _value in cache.writes:
        assert wkey in set(search_reads)
    # And the current cache key (epoch=1) is NOT in the writes done by
    # THIS request — proving the handler wrote under the OLD epoch.
    fresh_epoch = await _lc.get_visibility_epoch()
    assert fresh_epoch == 1
    # The response body itself is still the search result from before
    # the in-flight mutation — that's expected (the principal asked
    # for the data when they had visibility); the fix prevents future
    # requests from serving that payload, not this one.
    assert response is not None
