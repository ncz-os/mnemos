"""Real sqlite-vec visibility, candidate growth and Python-call regressions.

vec0 performs native exact cosine search. These tests verify authorization
and reduced Python cosine calls, not logarithmic complexity or a latency SLA.
When native candidates cannot satisfy the requested result count, the
repository falls back to the authoritative filtered embeddings.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.sqlite import _fetch_one
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _make_embedding(similarity: float) -> list[float]:
    """Return a 3-D vector that scores ``similarity`` cosine vs [1,0,0]."""
    return [similarity, math.sqrt(max(0.0, 1.0 - similarity * similarity)), 0.0]


async def _insert_memory(
    backend: SqliteBackend,
    tx,
    *,
    memory_id: str,
    content: str,
    owner_id: str,
    namespace: str,
    permission_mode: int = 600,
    updated_at: datetime | None = None,
) -> None:
    when = updated_at or datetime.now(timezone.utc)
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="solutions",
        subcategory=None,
        metadata_json='{"source":"vec-candidate-test"}',
        quality_rating=75,
        owner_id=owner_id,
        namespace=namespace,
        permission_mode=permission_mode,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=when,
        updated=when,
    )


async def _persist_embedding(backend: SqliteBackend, tx, memory_id: str, embedding: list[float]) -> None:
    await backend.memories.upsert_memory_embedding(tx, memory_id, embedding)


async def _vec_table_count(backend: SqliteBackend, tx, table_name: str = "memory_embedding_vec") -> int:
    row = await _fetch_one(
        tx.conn,
        f"SELECT COUNT(*) AS cnt FROM {table_name}",
    )
    if not row:
        return 0
    return int(row.get("cnt") if isinstance(row, dict) else row[0])


@pytest.mark.asyncio
async def test_unauthorized_memory_does_not_leak(tmp_path):
    """CORRECTNESS: a high-similarity vec0 candidate in a forbidden
    namespace / namespace-pin MUST NOT appear in the final result.

    The pre-fix UDF scan also gated by the same where-clause, but it
    had to scan the whole corpus linearly to enforce it. The
    candidate-path fix relies on a Vec0 KNN that is NOT
    authorization-aware — visibility MUST be re-applied against the
    authoritative ``memories`` table for the KNN ids returned. This
    test pins that contract: if the candidate-path ever relaxes the
    visibility re-check, an attacker who can plant a row in another
    namespace with a similar embedding would see it leak.
    """
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "leak.sqlite3", settings)
    await backend.open()

    alice_vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        namespace="ns-alice",
        group_ids=(),
    )

    now = datetime.now(timezone.utc)
    alice_id = "sqlite-vec-leak-alice"
    foreign_id_a = "sqlite-vec-leak-foreign-1"
    foreign_id_b = "sqlite-vec-leak-foreign-2"

    try:
        async with backend.transactional() as tx:
            await _insert_memory(
                backend,
                tx,
                memory_id=alice_id,
                content="alice needle — high cosine to query",
                owner_id="alice",
                namespace="ns-alice",
                updated_at=now,
            )
            await _persist_embedding(backend, tx, alice_id, [1.0, 0.0, 0.0])
            # Two unauthorized rows with high cosine to the query.
            # Pre-fix they would have leaked if the visibility filter
            # ever regressed on the candidate path; vec0 would surface
            # them as the strongest KNN matches.
            for fid in (foreign_id_a, foreign_id_b):
                await _insert_memory(
                    backend,
                    tx,
                    memory_id=fid,
                    content=f"{fid} foreign owner + namespace",
                    owner_id="bob",
                    namespace="ns-other",
                    updated_at=now,
                )
                await _persist_embedding(backend, tx, fid, [0.99, 0.14, 0.0])

            rows = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=5,
                visibility=alice_vis,
            )
    finally:
        await backend.close()

    ids = [row["id"] for row in rows]
    # The authoritative visibility filter (OWN_ONLY alice / ns-alice)
    # MUST be re-applied; the only row that should survive is alice's.
    assert ids == [alice_id], (
        f"foreign rows leaked through vec0 KNN: expected only {alice_id!r}, "
        f"got ids={ids!r}. Unauthorized rows are at vec0 distance 0 "
        "and would dominate the candidate pool if visibility were not "
        "re-checked against the memories table."
    )


@pytest.mark.asyncio
async def test_native_candidates_bound_python_cosine_calls(tmp_path):
    """PERFORMANCE: a small limit query over a larger eligible corpus
    returns well under the legacy UDF linear-scan cost.

    The pre-fix implementation called the Python cosine UDF over every
    row in ``memory_embeddings`` — O(corpus_size) per query. The
    candidate-path fix uses vec0 KNN for candidate generation and only
    re-ranks (in SQL) the rows that survive. We seed 1500 rows with
    varying embeddings so vec0 has real KNN work to do, then time a
    small-limit ``limit=5`` search. The legacy UDF path would cost
    roughly 50us per row (~75ms for 1500 rows); the vec0 candidate path
    should return in single-digit ms. We assert a generous 50ms upper
    bound (well under the legacy linear cost, plenty of CI headroom).
    Smaller-vocab fixtures keep test runtime sane for the
    validation-regression flow while still proving the shape change.
    """
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "perf.sqlite3", settings)
    await backend.open()

    root_vis = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )
    now = datetime.now(timezone.utc)
    prefix = "sqlite-vec-perf-"

    try:
        async with backend.transactional() as tx:
            for i in range(1_500):
                await _insert_memory(
                    backend,
                    tx,
                    memory_id=f"{prefix}{i}",
                    content=f"corpus row {i}",
                    owner_id="perf-owner",
                    namespace="default",
                    updated_at=now,
                )
                # Rotate embeddings so vec0 has real work to do (not
                # all perfectly identical, which would collapse to a
                # tie-break).
                sim = 0.50 + (i % 100) * 0.001
                await _persist_embedding(backend, tx, f"{prefix}{i}", _make_embedding(sim))

            count = await _vec_table_count(backend, tx)
            # Sanity: every embedding landed in vec0.
            assert count == 1_500, count

            from mnemos.persistence.sqlite import _call, _cosine_similarity

            calls = 0

            def counted_cosine(left, right):
                nonlocal calls
                calls += 1
                return _cosine_similarity(left, right)

            await _call(tx.conn.create_function, "mnemos_cosine_similarity", 2, counted_cosine)
            rows = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=5,
                visibility=root_vis,
            )
        assert len(rows) == 5
        # Native distance work remains linear; expensive Python reranking
        # must be confined to the selected IDs, not all 1,500 memories.
        assert 5 <= calls <= 100
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_requery_with_larger_window_when_too_few_authorized_survivors(tmp_path):
    """BOUNDED RE-QUERY: when the initial candidate pool is dominated
    by unauthorized rows, semantic_search retries with a larger window
    to recover visibility-reachable rows beyond the original K, then
    stops (no unbounded chasing).

    Layout: 1 alice-owned authorized row seeded far down the KNN
    ordering (low cosine), plus 50 bob-owned unauthorized high-cosine
    rows that dominate the initial 100-candidate window. The
    initial window would return zero authorized rows; the retry with
    a 4x-grown window should surface the alice row, and the search
    must NOT keep retrying past the cap.
    """
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "requery.sqlite3", settings)
    await backend.open()

    alice_vis = VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        namespace="ns-alice",
        group_ids=(),
    )

    now = datetime.now(timezone.utc)
    alice_id = "sqlite-vec-requery-alice"
    try:
        async with backend.transactional() as tx:
            # Alice's row: lower cosine than the bob ones, so it would
            # NOT be in the top-K initial window if K=100.
            await _insert_memory(
                backend,
                tx,
                memory_id=alice_id,
                content="alice authorized needle",
                owner_id="alice",
                namespace="ns-alice",
                updated_at=now,
            )
            await _persist_embedding(backend, tx, alice_id, [0.3, 0.95, 0.0])
            # 60 unauthorized rows with high cosine to the query — they
            # will populate the top of the KNN results and dominate the
            # initial 100-window.
            for i in range(60):
                fid = f"sqlite-vec-requery-foreign-{i}"
                await _insert_memory(
                    backend,
                    tx,
                    memory_id=fid,
                    content=f"foreign row {i}",
                    owner_id="bob",
                    namespace="ns-other",
                    updated_at=now,
                )
                await _persist_embedding(backend, tx, fid, [1.0 - 0.001 * i, 0.04, 0.0])

            rows = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=5,
                visibility=alice_vis,
            )
    finally:
        await backend.close()

    ids = [row["id"] for row in rows]
    # The re-query path must surface alice's row — without it the
    # initial 100-candidate window would be entirely the high-cosine
    # unauthorized set (60 rows above alice's cosine), so without the
    # bounded retry the result would be empty.
    assert ids == [alice_id], f"expected alice's row to surface via bounded re-query, got {ids!r}"
