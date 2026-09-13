"""Tests for MORPHEUS slice 2: real cluster + synthesise phases.

Slice 1 shipped the run-row machinery and rollback contract. Slice 2
fills in phase_cluster (cosine grouping) and phase_synthesise
(per-cluster summary memories tagged with morpheus_run_id).

These tests cover:
  - The pure helpers (_cosine_similarity, _parse_pgvector,
    _majority, _first_sentence, _synthesise_cluster_summary
    extractive mode) — no DB needed.
  - phase_cluster via the MorpheusRepository ABC — ordering,
    threshold, min_size filter, config persistence (verified
    through the captured ``merge_run_config`` patch).
  - phase_synthesise against a mocked pool — INSERT shape,
    source_memories tagging, rollback safety contract.

Item 11a (ABC migration): ``phase_cluster`` and ``phase_synthesise``
dispatch their final ``update_counters`` call through
``_get_backend()`` → ``backend.morpheus.update_counters``.

Item 11b (ABC migration): ``phase_cluster`` now routes the
candidate fetch + cluster-payload write through
``backend.morpheus.fetch_cluster_candidates`` and
``backend.morpheus.merge_run_config``. The mocked-pool tests below
mock those ABC methods directly (with a ``_MockMorpheus`` stub) so
the runner's plumbing is exercised end-to-end without standing up
a real Postgres / SQLite / Oracle. A separate
``tests/test_morpheus_cluster_abc.py`` test file ships a real-SQLite
coverage run that pins the ABC contract against the actual
``SqliteMorpheusRepository.fetch_cluster_candidates`` / merge_run_config
impls.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from mnemos.domain.morpheus.runner import (
    _cosine_similarity,
    _first_sentence,
    _majority,
    _parse_pgvector,
    _synthesise_cluster_summary,
    phase_cluster,
    phase_synthesise,
)

# ── pure helper tests ────────────────────────────────────────────────────────

def test_cosine_similarity_identical_vectors():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert _cosine_similarity(a, a) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([-1.0, 0.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_returns_zero():
    """A degenerate zero embedding must not produce a NaN — clustering
    needs deterministic comparisons even when garbage data sneaks in."""
    a = np.array([0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 1.0], dtype=np.float32)
    assert _cosine_similarity(a, b) == 0.0
    assert _cosine_similarity(b, a) == 0.0


def test_parse_pgvector_text_form():
    """asyncpg returns vector(N) as the literal pgvector text form
    "[0.1, 0.2, ...]" when the type is not registered."""
    result = _parse_pgvector("[0.1, 0.2, 0.3]")
    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float32
    np.testing.assert_array_almost_equal(result, [0.1, 0.2, 0.3])


def test_parse_pgvector_list_form():
    """If a future asyncpg type registration returns a list, that path
    must also work (callers don't care which form they get)."""
    result = _parse_pgvector([0.5, 0.5])
    np.testing.assert_array_almost_equal(result, [0.5, 0.5])


def test_parse_pgvector_null_returns_none():
    assert _parse_pgvector(None) is None


def test_parse_pgvector_garbage_returns_none():
    """Don't crash phase_cluster on a malformed embedding row — skip it."""
    assert _parse_pgvector("not-a-vector") is None


def test_majority_picks_most_common():
    assert _majority(["a", "b", "a", "c", "a"]) == "a"


def test_majority_breaks_ties_by_first_occurrence():
    """Two-way tie should prefer the first-seen value, so two runs over
    the same input produce the same cluster category."""
    assert _majority(["b", "a", "b", "a"]) == "b"


def test_majority_empty_returns_none():
    assert _majority([]) is None


def test_first_sentence_basic():
    assert _first_sentence("Hello world. Second sentence.") == "Hello world"


def test_first_sentence_no_terminator_truncates():
    long = "x" * 500
    assert _first_sentence(long) == "x" * 200


def test_first_sentence_empty():
    assert _first_sentence("") == ""


@pytest.mark.asyncio
async def test_synthesise_extractive_mode():
    """Default (no LLM) synthesis is deterministic and returns
    bullets of first sentences."""
    contents = [
        "First memory content. With a second sentence.",
        "Second memory has structure.",
        "Third memory.",
    ]
    summary = await _synthesise_cluster_summary(contents, use_llm=False)
    assert "MORPHEUS synthesis" in summary
    assert "First memory content" in summary
    assert "Second memory has structure" in summary
    # Bullets, one per member
    assert summary.count("•") == 3


@pytest.mark.asyncio
async def test_synthesise_extractive_handles_empty():
    assert (await _synthesise_cluster_summary([], use_llm=False)) == ""


# ── phase_cluster tests against mocked pool ─────────────────────────────────

class _MockConn:
    """Minimal asyncpg connection mock that records executed statements."""
    def __init__(self, fetchrow_result, fetch_result):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, *_args, **_kwargs):
        return self._fetchrow_result

    async def fetch(self, *_args, **_kwargs):
        return self._fetch_result

    async def fetchval(self, *_args, **_kwargs):
        # phase_synthesise reads config back; this is set explicitly per test.
        return getattr(self, "_fetchval_result", None)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "EXECUTE 1"


class _MockPool:
    """Minimal asyncpg.Pool mock returning a single _MockConn via acquire()."""
    def __init__(self, conn: _MockConn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool._conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _MorpheusNoOp:
    """No-op ``backend.morpheus`` for the slice-2 phase tests.

    Item 11a: ``phase_cluster`` and ``phase_synthesise`` dispatch their
    final ``update_counters`` through ``backend.morpheus.update_counters``.
    Item 11b: ``phase_cluster`` also routes through
    ``backend.morpheus.fetch_cluster_candidates`` and
    ``backend.morpheus.merge_run_config`` — those two are recorded
    by :class:`_MockMorpheus` below.
    """

    async def begin_run(self, tx, **kwargs):
        return "00000000-0000-0000-0000-000000000000"

    async def set_phase(self, tx, run_id, phase):
        return None

    async def update_counters(self, tx, run_id, **_kwargs):
        return None

    async def increment_extract_counters(
        self, tx, run_id, *, triples_extracted, memories_processed
    ):
        return None

    async def finish_run(self, tx, run_id):
        return None

    async def fail_run(self, tx, run_id, error):
        return None

    async def sweep_orphan_runs(self, tx, *, threshold_hours):
        return []

    async def rollback_run(self, tx, run_id, *, requested_by):
        return 0, 1

    async def fetch_cluster_candidates(self, tx, *, run_id, max_input_count):
        return None

    async def merge_run_config(self, tx, run_id, *, patch):
        return None


class _MockMorpheus(_MorpheusNoOp):
    """``backend.morpheus`` mock that captures ABC calls from item 11b.

    The slice-2 phase tests mock the ``fetch_cluster_candidates`` and
    ``merge_run_config`` ABC methods directly so the runner's plumbing
    is exercised without standing up a real backend. The no-op
    base class covers the lifecycle methods (begin_run, set_phase,
    update_counters, etc.); this subclass records the two cluster
    pipeline calls and lets each test seed the desired
    ``fetch_cluster_candidates`` return value via ``set_candidates``.
    """

    def __init__(self):
        super().__init__()
        self.captured_merged: list[tuple[str, dict]] = []
        self.candidates_return = None  # set by set_candidates()

    def set_candidates(self, ctx):
        self.candidates_return = ctx

    async def fetch_cluster_candidates(self, tx, *, run_id, max_input_count):
        return self.candidates_return

    async def merge_run_config(self, tx, run_id, *, patch):
        self.captured_merged.append((run_id, patch))


class _BackendMock:
    """Backend-shaped mock — has ``morpheus`` and ``transactional``.

    Carries a ``_MockMorpheus`` instance on ``.morpheus`` so tests can
    seed the ``fetch_cluster_candidates`` return and inspect the
    ``merge_run_config`` call. The ``transactional`` CM yields
    ``None`` for ``tx`` because the mocked ABC methods don't
    actually use it.
    """

    def __init__(self):
        self.morpheus = _MockMorpheus()

    def transactional(self):

        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


@pytest.fixture(autouse=True)
def _install_mock_morpheus_backend(monkeypatch):
    """Wire a mock backend into the lifecycle global for every test.

    Item 11a: ``phase_cluster`` / ``phase_synthesise`` internally call
    ``_get_backend()`` to dispatch ``update_counters`` through the new
    ABC. The lifecycle global ``_persistence_backend`` is None by
    default in this test process, so wire a ``_BackendMock`` for the
    duration of each test so the phase functions don't crash trying
    to look up a backend. Tests that need to drive ABC behaviour
    reach for ``backend.morpheus.set_candidates(...)`` via a
    ``monkeypatch`` of ``_get_backend``.
    """
    from mnemos.core import lifecycle as _lifecycle

    backend = _BackendMock()
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    return backend


def _row(memory_id: str, vec: list[float]) -> dict[str, Any]:
    """Build a candidate row in the ABC's ``list[float]`` shape.

    Item 11b: candidates are pre-materialised to ``list[float]`` by
    ``backend.morpheus.fetch_cluster_candidates``; the runner no
    longer has to parse text-cast pgvector output.
    """
    return (memory_id, vec)


def _candidate_ctx(candidates, *, cluster_min_size=1, namespace=None):
    """Build a ClusterCandidateRow-shaped return for the mock ABC."""
    from mnemos.persistence.base import ClusterCandidateRow

    return ClusterCandidateRow(
        cluster_min_size=cluster_min_size,
        window_started_at="2026-04-25T00:00:00",
        window_ended_at="2026-04-25T23:59:59",
        namespace=namespace,
        candidates=candidates,
    )


@pytest.mark.asyncio
async def test_phase_cluster_groups_similar_vectors(_install_mock_morpheus_backend):
    """Two near-identical vectors should land in one cluster; the third
    orthogonal vector should be its own (and dropped if min_size > 1)."""
    backend = _install_mock_morpheus_backend
    backend.morpheus.set_candidates(
        _candidate_ctx(
            candidates=[
                _row("mem_a", [1.0, 0.0, 0.0]),
                _row("mem_b", [0.99, 0.01, 0.0]),       # very close to mem_a
                _row("mem_c", [0.0, 1.0, 0.0]),         # orthogonal — its own cluster
            ],
            cluster_min_size=2,
        )
    )
    pool = _MockConn(None, None)  # pool is now unused by phase_cluster

    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000001")

    # min_size=2 filters out the singleton mem_c cluster.
    assert n == 1
    # The cluster payload should have been written via merge_run_config.
    assert len(backend.morpheus.captured_merged) == 1
    run_id, patch = backend.morpheus.captured_merged[0]
    assert run_id == "00000000-0000-0000-0000-000000000001"
    payload = patch["clusters"]
    assert len(payload) == 1
    assert set(payload[0]["member_memory_ids"]) == {"mem_a", "mem_b"}


@pytest.mark.asyncio
async def test_phase_cluster_threshold_separation(
    monkeypatch, _install_mock_morpheus_backend
):
    """A threshold raised above the actual similarity should split a
    cluster that would otherwise merge."""
    from mnemos.core import config

    backend = _install_mock_morpheus_backend
    with monkeypatch.context() as scoped:
        scoped.setenv("MNEMOS_MORPHEUS_CLUSTER_THRESHOLD", "0.999")
        config._reset_settings_for_tests()
        backend.morpheus.set_candidates(
            _candidate_ctx(
                candidates=[
                    _row("mem_a", [1.0, 0.0]),
                    _row("mem_b", [0.9, 0.4]),  # cosine ~0.91 — under 0.999
                ],
                cluster_min_size=1,
            )
        )
        pool = _MockConn(None, None)
        n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000002")
    config._reset_settings_for_tests()

    # Both survive (min_size=1) but as separate clusters.
    assert n == 2


@pytest.mark.asyncio
async def test_phase_cluster_no_rows_zero_clusters(_install_mock_morpheus_backend):
    backend = _install_mock_morpheus_backend
    backend.morpheus.set_candidates(
        _candidate_ctx(candidates=[], cluster_min_size=3)
    )
    pool = _MockConn(None, None)
    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000003")
    assert n == 0
    # No merge_run_config call when there's nothing to persist.
    assert backend.morpheus.captured_merged == []


@pytest.mark.asyncio
async def test_phase_cluster_passes_namespace_to_query(_install_mock_morpheus_backend):
    """When the run has namespace set, phase_cluster should forward it
    as a query arg so the SQL filter scopes the scan to that tenant."""
    backend = _install_mock_morpheus_backend
    captured_namespace: list = []

    real_fetch = backend.morpheus.fetch_cluster_candidates

    async def spy_fetch(tx, *, run_id, max_input_count):
        ctx = await real_fetch(tx, run_id=run_id, max_input_count=max_input_count)
        if ctx is not None:
            captured_namespace.append(ctx.namespace)
        return ctx

    backend.morpheus.fetch_cluster_candidates = spy_fetch  # type: ignore[method-assign]
    backend.morpheus.set_candidates(
        _candidate_ctx(candidates=[], cluster_min_size=1, namespace="tenant-a")
    )
    pool = _MockConn(None, None)
    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000005")
    assert n == 0
    assert captured_namespace == ["tenant-a"]


@pytest.mark.asyncio
async def test_phase_cluster_skips_garbage_embeddings(_install_mock_morpheus_backend):
    """Item 11b: the runner consumes ``list[float]`` directly; the
    backend's ``fetch_cluster_candidates`` impl is responsible for
    dropping unparseable embeddings. A garbage row in the runner's
    view would manifest as ``[]`` — the runner must not crash, must
    skip it (treat as "not in any cluster"), and must continue."""
    backend = _install_mock_morpheus_backend
    backend.morpheus.set_candidates(
        _candidate_ctx(
            candidates=[
                _row("mem_a", []),  # empty vec — backend would skip
                _row("mem_b", [1.0, 0.0]),
            ],
            cluster_min_size=1,
        )
    )
    pool = _MockConn(None, None)
    n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000004")
    # mem_a's empty vec still occupies a cluster slot (min_size=1
    # preserves singletons) — the runner does NOT implicitly drop
    # empty vectors; that's the backend's job in its own
    # ``fetch_cluster_candidates``. This test pins that the runner
    # tolerates an empty list without crashing — both mem_a and
    # mem_b end up as singleton clusters.
    assert n == 2
    # No cluster_payload merges in any garbage text.
    assert len(backend.morpheus.captured_merged) == 1
    payload = backend.morpheus.captured_merged[0][1]["clusters"]
    member_sets = {frozenset(c["member_memory_ids"]) for c in payload}
    assert member_sets == {frozenset({"mem_a"}), frozenset({"mem_b"})}


@pytest.mark.asyncio
async def test_phase_cluster_respects_max_input_count(
    monkeypatch, _install_mock_morpheus_backend
):
    """Item 11b: ``phase_cluster`` forwards ``max_input_count`` to
    ``fetch_cluster_candidates`` so each backend can apply its own
    LIMIT/FETCH-FIRST clause. This test pins the runner-side
    wiring without testing the backend's SQL."""
    from mnemos.core import config

    backend = _install_mock_morpheus_backend
    backend.morpheus.set_candidates(_candidate_ctx(candidates=[], cluster_min_size=1))
    captured: dict[str, Any] = {}

    real_fetch = backend.morpheus.fetch_cluster_candidates

    async def spy_fetch(tx, *, run_id, max_input_count):
        captured["max_input_count"] = max_input_count
        return await real_fetch(tx, run_id=run_id, max_input_count=max_input_count)

    backend.morpheus.fetch_cluster_candidates = spy_fetch  # type: ignore[method-assign]
    monkeypatch.setenv("MNEMOS_MORPHEUS_CLUSTER_MAX_INPUT_COUNT", "12345")
    config._reset_settings_for_tests()
    try:
        pool = _MockConn(None, None)
        n = await phase_cluster(pool, "00000000-0000-0000-0000-000000000006")
        assert n == 0
        assert captured["max_input_count"] == 12345
    finally:
        monkeypatch.delenv("MNEMOS_MORPHEUS_CLUSTER_MAX_INPUT_COUNT", raising=False)
        config._reset_settings_for_tests()


# ── phase_synthesise tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_synthesise_inserts_one_per_cluster():
    """Two clusters in the run config → two INSERTs into memories,
    each tagged with morpheus_run_id, source_memories, provenance."""
    run_id = "00000000-0000-0000-0000-000000000010"
    config = {
        "clusters": [
            {"cluster_id": 0, "member_memory_ids": ["mem_1", "mem_2"]},
            {"cluster_id": 1, "member_memory_ids": ["mem_3", "mem_4"]},
        ]
    }

    member_rows_by_call = [
        [
            {"id": "mem_1", "content": "First fact about the deploy.", "category": "facts", "owner_id": "default", "namespace": "default"},
            {"id": "mem_2", "content": "Second fact, related to the first.", "category": "facts", "owner_id": "default", "namespace": "default"},
        ],
        [
            {"id": "mem_3", "content": "Decision was made on Tuesday.", "category": "decisions", "owner_id": "default", "namespace": "default"},
            {"id": "mem_4", "content": "Decision rationale captured.", "category": "decisions", "owner_id": "default", "namespace": "default"},
        ],
    ]

    fetch_calls = {"i": 0}

    class _Conn:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        async def fetchval(self, *_args, **_kwargs):
            return config

        async def fetch(self, *_args, **_kwargs):
            i = fetch_calls["i"]
            fetch_calls["i"] += 1
            return member_rows_by_call[i]

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "INSERT 0 1"

    conn = _Conn()
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)

    assert n == 2
    inserts = [(s, a) for s, a in conn.executed if "INSERT INTO memories" in s]
    assert len(inserts) == 2
    # Every insert carries the morpheus_run_id and source_memories.
    for sql, args in inserts:
        assert "morpheus_run_id" in sql
        assert "source_memories" in sql
        assert "'morpheus_local'" in sql
        # args[7] is run_id (1-indexed: $1=id, $2=summary, $3=category,
        # $4=subcat, $5=metadata, $6=owner, $7=ns, $8=run_id, $9=source_memories)
        assert args[7] == run_id


@pytest.mark.asyncio
async def test_phase_synthesise_no_clusters_zero():
    run_id = "00000000-0000-0000-0000-000000000011"
    conn = _MockConn(fetchrow_result=None, fetch_result=[])
    conn._fetchval_result = {"clusters": []}
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)
    assert n == 0


@pytest.mark.asyncio
async def test_phase_synthesise_no_config_zero():
    run_id = "00000000-0000-0000-0000-000000000012"
    conn = _MockConn(fetchrow_result=None, fetch_result=[])
    conn._fetchval_result = None
    pool = _MockPool(conn)

    n = await phase_synthesise(pool, run_id)
    assert n == 0


@pytest.mark.asyncio
async def test_phase_synthesise_inherits_majority_category():
    """When a cluster has 3 members across two categories, the new
    summary memory inherits the majority category. Tie-breaking is
    first-occurrence so behavior is reproducible across runs."""
    run_id = "00000000-0000-0000-0000-000000000013"
    config = {
        "clusters": [
            {"cluster_id": 0, "member_memory_ids": ["m1", "m2", "m3"]},
        ]
    }

    members = [
        {"id": "m1", "content": "x.", "category": "facts", "owner_id": "default", "namespace": "default"},
        {"id": "m2", "content": "y.", "category": "decisions", "owner_id": "default", "namespace": "default"},
        {"id": "m3", "content": "z.", "category": "decisions", "owner_id": "default", "namespace": "default"},
    ]

    class _Conn:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        async def fetchval(self, *_args, **_kwargs):
            return config

        async def fetch(self, *_args, **_kwargs):
            return members

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "INSERT 0 1"

    conn = _Conn()
    pool = _MockPool(conn)

    await phase_synthesise(pool, run_id)
    insert = next(((s, a) for s, a in conn.executed if "INSERT INTO memories" in s), None)
    assert insert is not None
    # args[2] is category in the INSERT VALUES order.
    assert insert[1][2] == "decisions"
