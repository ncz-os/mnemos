"""POST /admin/compression/enqueue + /admin/compression/enqueue-all.

Validator-level tests. The happy path is exercised end-to-end by the
CERBERUS test deployment's barrage_seed.py which bulk-enqueues hundreds
of memories; a full-mock happy-path here would duplicate surface
without adding signal. What this test does pin: the 422 boundaries on
reason / scoring_profile so a typo in a v3.2 PR can't quietly widen
the allowlist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.api.routes.admin import (
    CompressionEnqueueAllRequest,
    CompressionEnqueueRequest,
    compression_enqueue,
    compression_enqueue_all,
)


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


@pytest.fixture
def fake_pool(monkeypatch):
    """Mock the active backend so handlers get into validation."""
    from mnemos.core import lifecycle
    from tests._fake_backend import FakePoolBackedBackend

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncContext(MagicMock()))
    monkeypatch.setattr(lifecycle, "_pool", mock_pool)
    monkeypatch.setattr(lifecycle, "_persistence_backend", FakePoolBackedBackend(mock_pool))
    return mock_pool


# ---- enqueue (specific ids) — validation boundaries ------------------------


@pytest.mark.parametrize(
    "reason",
    ["invented_reason", "", "ON_WRITE", "forbidden space"],
)
def test_enqueue_rejects_unknown_reason(reason):
    """#170: reason is now Literal[...] on the model — invalid
    values are rejected at parse time (FastAPI surfaces this as
    422). Test now exercises the Pydantic-level rejection."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        CompressionEnqueueRequest(memory_ids=["mem-1"], reason=reason)
    assert "reason" in str(exc.value).lower()


@pytest.mark.parametrize(
    "profile",
    ["invented", "", "BALANCED", "custom_thing"],
)
def test_enqueue_rejects_unknown_scoring_profile(profile):
    """#170: scoring_profile is now Literal[...] on the model."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        CompressionEnqueueRequest(memory_ids=["mem-1"], scoring_profile=profile)
    assert "scoring_profile" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_enqueue_accepts_every_documented_reason(fake_pool):
    # pool.acquire().fetch returns empty (no memories found), so the
    # handler goes straight to the empty return without touching the DB
    # beyond the known-check.
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    fake_pool.acquire = MagicMock(return_value=_AsyncContext(conn))

    for reason in ("on_write", "manual", "scheduled", "reprocess"):
        req = CompressionEnqueueRequest(memory_ids=["mem-1"], reason=reason)
        resp = await compression_enqueue(request=req, _=None)
        assert resp.enqueued == 0
        assert resp.skipped_unknown == 1


# ---- enqueue-all (bulk) — validation boundaries ----------------------------


def test_enqueue_all_rejects_unknown_reason():
    """#170: reason is now Literal[...] — Pydantic auto-422s at parse."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompressionEnqueueAllRequest(reason="yolo")


def test_enqueue_all_rejects_unknown_scoring_profile():
    """#170: scoring_profile is now Literal[...]."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompressionEnqueueAllRequest(scoring_profile="yolo")


@pytest.mark.asyncio
async def test_enqueue_all_delegates_to_queue_abc(fake_pool):
    # job 019e7049 CHILD C-admin: the route now delegates to the
    # backend.compression_queue ABC (works on Postgres + Oracle). Pin
    # that it surfaces the ABC's count in the response.
    from mnemos.core import lifecycle as _lc

    _lc._persistence_backend._compression_queue.configure_return("enqueue_all_compression", 42)
    resp = await compression_enqueue_all(
        request=CompressionEnqueueAllRequest(only_uncompressed=True, limit=100),
        _=None,
    )
    assert resp.enqueued == 42


# ---- enqueue-all SQL behaviour now lives on the PG queue repo --------------
# The only_uncompressed / category-bind guarantees moved out of the route
# (which delegates to the ABC) into PostgresCompressionQueueRepository, so
# they are pinned at that layer here.


def _pg_repo_and_conn():
    from mnemos.persistence.postgres import (
        PostgresCompressionQueueRepository,
        PostgresTransaction,
    )

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 42")
    tx = PostgresTransaction(conn, MagicMock())
    return PostgresCompressionQueueRepository(), tx, conn


@pytest.mark.asyncio
async def test_pg_enqueue_all_honors_only_uncompressed():
    repo, tx, conn = _pg_repo_and_conn()

    await repo.enqueue_all_compression(
        tx,
        reason="manual",
        priority=0,
        scoring_profile="balanced",
        category=None,
        only_uncompressed=True,
        limit=100,
    )
    sql_on = conn.execute.call_args.args[0]
    assert "memory_compressed_variants" in sql_on
    assert "NOT EXISTS" in sql_on

    conn.execute.reset_mock()
    await repo.enqueue_all_compression(
        tx,
        reason="manual",
        priority=0,
        scoring_profile="balanced",
        category=None,
        only_uncompressed=False,
        limit=100,
    )
    sql_off = conn.execute.call_args.args[0]
    assert "memory_compressed_variants" not in sql_off


@pytest.mark.asyncio
async def test_pg_enqueue_all_category_filter_param_bound():
    repo, tx, conn = _pg_repo_and_conn()

    await repo.enqueue_all_compression(
        tx,
        reason="manual",
        priority=0,
        scoring_profile="balanced",
        category="solutions",
        only_uncompressed=True,
        limit=10,
    )
    # category must be bound, not string-interpolated (SQLi guard).
    sql, *args = conn.execute.call_args.args
    assert "solutions" not in sql, "category must be bound, not interpolated"
    assert "solutions" in args
