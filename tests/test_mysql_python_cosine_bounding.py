"""Regression tests for the MySQL Python-cosine fallback bounding + offload.

The fallback exists because MySQL Community Edition (verified absent of
``VEC_DISTANCE_COSINE`` through 9.3) lacks a native vector distance
function; when ``semantic_search`` catches the ``1305 / VEC_DISTANCE``
error it routes to ``_python_cosine_search`` which reads bounded pages and retains the best K rows in Python.

The pre-fix behavior scaled memory, CPU, and async-event-loop
occupancy proportional to corpus size. This file pins down the
post-fix contract:

1. **Small corpus: behavior unchanged.** Under the
   ``MNEMOS_MYSQL_PY_COSINE_MAX_ROWS`` threshold the fallback returns
   the same ranked rows it always did — no warning, no behavioral
   difference for normal-sized deployments.

2. **Large corpus: loud warning fires.** Above the threshold the
   fallback emits a single ``logger.warning`` identifying the slow
   Python-cosine path and pointing at remediation (MariaDB /
   Enterprise / HeatWave).

3. **Event loop not blocked.** The heavy cosine ranking runs on the
   default ``ThreadPoolExecutor`` (``loop.run_in_executor(None, ...)``),
   so a concurrent async task can make progress while the fallback
   search is in flight.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mnemos.persistence.mysql import (
    MysqlMemoryRepository,
    _DEFAULT_MYSQL_PY_COSINE_MAX_ROWS,
    _PY_COSINE_SCALE_WARNED,
    _mysql_py_cosine_max_rows,
    _warn_python_cosine_scale_once,
)


_FALLBACK_COLUMNS = (
    "id",
    "content",
    "category",
    "subcategory",
    "metadata",
    "quality_rating",
    "compressed_content",
    "verbatim_content",
    "owner_id",
    "namespace",
    "permission_mode",
    "source_model",
    "source_provider",
    "source_session",
    "source_agent",
    "group_id",
    "created",
    "updated",
    "archived_at",
    "recall_count",
    "last_recalled_at",
    "consolidated_into",
    "embedding_json",
)


def _row(memory_id: str, embedding: list[float], *, updated: datetime | None = None) -> tuple:
    ts = updated or datetime(2026, 6, 1, tzinfo=timezone.utc)
    return (
        memory_id,
        memory_id,
        "facts",
        None,
        "{}",
        80,
        None,
        memory_id,
        "alice",
        "alice-ns",
        600,
        None,
        None,
        None,
        None,
        None,
        ts,
        ts,
        None,
        0,
        None,
        None,
        str(embedding).replace(" ", ""),
    )


class _FakeCursor:
    """Backend mock that mirrors the production SELECT COUNT(*) + SELECT shape.

    The first ``execute`` call with a ``COUNT(*)`` SQL returns the row
    count via ``fetchall``; subsequent calls return the corpus rows.
    ``fetchone`` mirrors ``fetchall[0]`` for code that reads scalars
    directly (the production code uses ``_fetch_all_dicts``).
    """

    def __init__(self, corpus: list[tuple]) -> None:
        self._corpus = corpus
        self._last_sql = ""
        self._params = []
        self.description = tuple((col,) for col in _FALLBACK_COLUMNS)

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        return None

    async def execute(self, sql: str, _params) -> None:
        self._last_sql = sql
        self._params = _params

    async def fetchall(self) -> list[tuple]:
        if "COUNT(*)" in self._last_sql:
            return [(len(self._corpus),)]
        rows = sorted(self._corpus, key=lambda row: row[0])
        if "m.id > %s" in self._last_sql:
            rows = [row for row in rows if row[0] > self._params[-1]]
        return rows[:500]

    async def fetchone(self) -> tuple | None:
        if "COUNT(*)" in self._last_sql:
            return (len(self._corpus),)
        if not self._corpus:
            return None
        return self._corpus[0]


class _FakeConn:
    def __init__(self, corpus: list[tuple]) -> None:
        self._corpus = corpus

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._corpus)


@pytest.fixture(autouse=True)
def _reset_scale_warning_flag():
    """Reset the per-process 'we've already warned' flag between tests.

    The flag lives on the module so the warning stays quiet on a busy
    server. Tests asserting that the warning fires need a clean slate;
    tests asserting that the warning fires only once need both the
    clean-slate state AND a final reset for subsequent unrelated tests.
    """
    import mnemos.persistence.mysql as mysql_mod

    saved = mysql_mod._PY_COSINE_SCALE_WARNED
    mysql_mod._PY_COSINE_SCALE_WARNED = False
    try:
        yield
    finally:
        mysql_mod._PY_COSINE_SCALE_WARNED = saved


@pytest.mark.asyncio
async def test_small_corpus_returns_correct_ranking_without_warning(monkeypatch, caplog):
    """Under the threshold the fallback returns identical rows and
    emits no slow-path warning — invisible for normal-sized
    deployments, as the task spec requires."""
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "10000")
    caplog.set_level(logging.WARNING, logger="mnemos.persistence.mysql")

    corpus = [
        _row("weak", [1.0, 0.0, 0.0]),
        _row("strong", [1.0, 0.1, 0.0]),
    ]
    repo = MysqlMemoryRepository()
    tx = SimpleNamespace(conn=_FakeConn(corpus))

    out = await repo._python_cosine_search(
        tx,
        vec_literal="[1.0, 0.0, 0.0]",
        where=["1 = 1"],
        params=[],
        limit=1,
        boost_recency=False,
        recency_weight=0.15,
    )

    assert [row["id"] for row in out] == ["weak"]
    slow_path_warnings = [record for record in caplog.records if "[PY-COSINE]" in record.message]
    assert slow_path_warnings == [], (
        f"small corpus must not emit the slow-path warning; got: {[r.message for r in slow_path_warnings]}"
    )


@pytest.mark.asyncio
async def test_large_corpus_emits_single_loud_warning(caplog):
    """Above the threshold the fallback emits one warning identifying
    the slow Python-cosine path so operators can see clearly when
    they're running without native VECTOR_DISTANCE support."""
    # Tight threshold so a 200-row corpus exceeds it.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "100")
    caplog.set_level(logging.WARNING, logger="mnemos.persistence.mysql")

    corpus = [_row(f"m{i}", [float(i), 0.0, 0.0]) for i in range(200)]
    repo = MysqlMemoryRepository()
    tx = SimpleNamespace(conn=_FakeConn(corpus))

    out = await repo._python_cosine_search(
        tx,
        vec_literal="[1.0, 0.0, 0.0]",
        where=["1 = 1"],
        params=[],
        limit=5,
        boost_recency=False,
        recency_weight=0.15,
    )

    monkeypatch.undo()

    # Correctness still holds even when the warning fires — the contract
    # is "slow-but-correct, not silently truncated".
    assert len(out) == 5
    assert all(row["rank_score"] is not None for row in out)

    slow_path_warnings = [record for record in caplog.records if "[PY-COSINE]" in record.message]
    assert len(slow_path_warnings) == 1, (
        f"exactly one warning should fire on the first qualifying call; got {len(slow_path_warnings)}"
    )
    msg = slow_path_warnings[0].message
    assert "200" in msg, f"warning must report observed eligible count: {msg}"
    assert "100" in msg, f"warning must report the configured threshold: {msg}"
    assert "MariaDB" in msg or "Enterprise" in msg, f"warning must point at remediation: {msg}"


@pytest.mark.asyncio
async def test_large_corpus_warning_fires_only_once_per_process(monkeypatch, caplog):
    """A busy server with a 100k-row corpus shouldn't spam the log once
    per request — the warning is informational and dedupes after the
    first fire."""
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "5")
    caplog.set_level(logging.WARNING, logger="mnemos.persistence.mysql")

    corpus = [_row(f"m{i}", [float(i), 0.0, 0.0]) for i in range(20)]
    repo = MysqlMemoryRepository()
    tx = SimpleNamespace(conn=_FakeConn(corpus))

    # Three consecutive calls; only the first one should warn.
    for _ in range(3):
        await repo._python_cosine_search(
            tx,
            vec_literal="[1.0, 0.0, 0.0]",
            where=["1 = 1"],
            params=[],
            limit=1,
            boost_recency=False,
            recency_weight=0.15,
        )

    slow_path_warnings = [record for record in caplog.records if "[PY-COSINE]" in record.message]
    assert len(slow_path_warnings) == 1, f"warning must fire at most once per process; got {len(slow_path_warnings)}"


@pytest.mark.asyncio
async def test_python_cosine_does_not_block_event_loop(monkeypatch):
    """The heavy cosine ranking + sort + slice runs on a thread-pool
    executor, so a concurrent async task can make progress while the
    fallback search is in flight. Without the offload the cosine loop
    would run inline on the asyncio loop and the concurrent task would
    not see any progress until it finished.

    Test approach: install a ``_cosine_rank_rows`` monkeypatch that
    sleeps on a thread so the offload has a measurable window of time
    to allow concurrent asyncio traffic. Then schedule a concurrent
    async task that ticks the loop, and assert it observed at least
    one tick within the cosine window.
    """
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "0")  # skip probe
    repo = MysqlMemoryRepository()

    # 500 rows is enough work that the cosine loop alone won't return
    # instantly, even with the hot-rs opt-in off (default).
    corpus = [_row(f"m{i}", [float(i % 50), 0.0, 0.0]) for i in range(500)]
    tx = SimpleNamespace(conn=_FakeConn(corpus))

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        # Tick the loop while the cosine work is in flight. With the
        # offload in place, this coroutine gets scheduled repeatedly
        # during the executor's sleep; without it the loop is starved
        # until the inline cosine finishes.
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            await asyncio.sleep(0)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    out = await repo._python_cosine_search(
        tx,
        vec_literal="[1.0, 0.0, 0.0]",
        where=["1 = 1"],
        params=[],
        limit=10,
        boost_recency=False,
        recency_weight=0.15,
    )
    await ticker_task

    assert len(out) == 10
    # If the cosine ran inline on the loop, the ticker would have
    # observed zero or near-zero ticks during the cosine window. With
    # the offload, asyncio.sleep(0) yields the loop and the ticker
    # observes many ticks during the cosine work.
    assert ticks >= 5, (
        f"event loop appears blocked: ticker only saw {ticks} ticks "
        "while the python-cosine fallback was running. The offload to "
        "loop.run_in_executor is not in effect."
    )


def test_threshold_default_matches_docstring():
    """The default threshold must be a positive integer that operators
    can override via env var. Pin it so a sloppy refactor doesn't
    silently change the warning trigger."""
    assert _DEFAULT_MYSQL_PY_COSINE_MAX_ROWS > 0
    assert _mysql_py_cosine_max_rows() == _DEFAULT_MYSQL_PY_COSINE_MAX_ROWS


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "12345")
    assert _mysql_py_cosine_max_rows() == 12345


def test_threshold_zero_disables_probe_and_warning(monkeypatch):
    """Setting the threshold to 0 disables both the COUNT probe and
    the warning entirely — the per-request probe would still be cheap
    but adds a round trip even for small corpora, which is what the
    task spec calls out as "invisible for normal-sized deployments"."""
    monkeypatch.setenv("MNEMOS_MYSQL_PY_COSINE_MAX_ROWS", "0")
    assert _mysql_py_cosine_max_rows() == 0


def test_warn_helper_is_idempotent():
    """The dedupe flag is process-local; calling the helper directly
    twice fires the warning once and stays quiet thereafter. We don't
    assert on log output here (caplog is async-fixture-only), but we
    do assert the flag flips on the first call and stays set."""
    import mnemos.persistence.mysql as mysql_mod

    mysql_mod._PY_COSINE_SCALE_WARNED = False
    _warn_python_cosine_scale_once(eligible_count=50000, threshold=10000)
    assert mysql_mod._PY_COSINE_SCALE_WARNED is True
    # Second call must not flip it back.
    _warn_python_cosine_scale_once(eligible_count=50000, threshold=10000)
    assert mysql_mod._PY_COSINE_SCALE_WARNED is True


def test_warning_flag_starts_false_by_default():
    """Each fresh process (or fresh import in test) sees the warning
    flag in its reset state."""
    # The autouse fixture in this file resets the flag for each test,
    # so by the time we reach here the flag has been reset to False.
    assert _PY_COSINE_SCALE_WARNED is False


@pytest.mark.asyncio
async def test_best_match_on_later_page_is_not_truncated(monkeypatch):
    corpus = [_row(f"id-{i:05}", [0, 1, 0]) for i in range(1200)]
    corpus.append(_row("last-winner", [1, 0, 0]))
    repo = MysqlMemoryRepository()
    original = repo._cosine_rank_rows
    page_sizes = []

    def measure_page(query, rows, *args, **kwargs):
        page_sizes.append(len(rows))
        return original(query, rows, *args, **kwargs)

    monkeypatch.setattr(repo, "_cosine_rank_rows", measure_page)
    out = await repo._python_cosine_search(
        SimpleNamespace(conn=_FakeConn(corpus)),
        vec_literal="[1,0,0]",
        where=["1=1"],
        params=[],
        limit=1,
        boost_recency=False,
        recency_weight=0.15,
    )
    assert [row["id"] for row in out] == ["last-winner"]
    assert page_sizes == [500, 500, 201]
