"""FB296 — retention-policy enforcement in the PERSEPHONE archival sweep.

Drives ``mnemos.persistence.worker_lifecycle.sweep_for_archival`` — the
backend-neutral sweep the archival worker calls — against a real SQLite
backend with a frozen clock, and pins what the policy actually does:

* past the window a memory is **archived** (compressed payload in
  ``memory_archive``, body replaced by ``ARCHIVED:<id>``), never deleted;
* the window is measured on ``created``, with the hot set excluded by
  ``last_recalled_at``;
* there is no pin column, no delete mode, and no dry-run on this path.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

# The persephone subsystem is gated on the ``zstandard`` extra and raises
# RuntimeError rather than ImportError, which errors collection instead of
# skipping — gate on the extra's probe.
pytest.importorskip(
    "zstandard",
    reason="persephone subsystem not installed; pip install mnemos-core[persephone]",
)

from mnemos.persistence import worker_lifecycle as wl
from mnemos.persistence.sqlite import SqliteBackend, _execute, _fetch_one

NS = "retention-ns"
WINDOW_DAYS = 30
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """Fake clock. ``sweep_for_archival``'s only time source is ``datetime.now``."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - datetime API
        # A plain ``datetime``: sqlite3 resolves its adapter by exact type, so
        # handing back the subclass would fail to bind the cutoff parameter.
        if tz is None:
            return datetime(2026, 6, 1, 12, 0, 0)
        return datetime(2026, 6, 1, 12, 0, 0, tzinfo=tz)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(wl, "datetime", _FrozenDatetime)
    return NOW


@pytest_asyncio.fixture
async def backend(tmp_path):
    instance = SqliteBackend(tmp_path / "retention.db", SimpleNamespace())
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _insert(
    backend: Any,
    memory_id: str,
    *,
    created_days: int = 60,
    updated_days: int | None = None,
    last_recalled_days: int | None = None,
    consolidated_into: str | None = None,
    namespace: str = NS,
) -> None:
    created = NOW - timedelta(days=created_days)
    updated = NOW - timedelta(days=updated_days) if updated_days is not None else created
    last_recalled = NOW - timedelta(days=last_recalled_days) if last_recalled_days is not None else None
    async with backend.transactional() as tx:
        await _execute(
            tx.conn,
            "INSERT INTO memories "
            "(id, content, owner_id, namespace, created, updated, last_recalled_at, "
            " consolidated_into) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                f"payload-{memory_id}",
                "user-1",
                namespace,
                created,
                updated,
                last_recalled,
                consolidated_into,
            ),
        )


async def _sweep(backend: Any, *, window: int = WINDOW_DAYS, batch: int = 10) -> int:
    return await wl.sweep_for_archival(
        backend,
        namespace=NS,
        archive_after_days=window,
        batch_size=batch,
    )


async def _row(backend: Any, memory_id: str) -> Any:
    async with backend.transactional() as tx:
        return await _fetch_one(
            tx.conn,
            "SELECT content, verbatim_content, archived_at FROM memories WHERE id = ?",
            (memory_id,),
        )


async def _archive_row(backend: Any, memory_id: str) -> Any:
    async with backend.transactional() as tx:
        return await _fetch_one(
            tx.conn,
            "SELECT compression_algo, schema_version, original_size_bytes FROM memory_archive WHERE id = ?",
            (memory_id,),
        )


async def _count(backend: Any) -> int:
    async with backend.transactional() as tx:
        row = await _fetch_one(tx.conn, "SELECT COUNT(*) AS n FROM memories", ())
    return row["n"]


# ── archive, not delete ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cold_memory_is_archived_not_deleted(backend, clock):
    await _insert(backend, "mem-cold", created_days=60)
    before = await _count(backend)

    archived = await _sweep(backend)

    assert archived == 1
    live = await _row(backend, "mem-cold")
    assert live["content"] == "ARCHIVED:mem-cold"
    assert live["verbatim_content"] is None
    # The retention timestamp is the frozen clock, not wall time.
    assert str(live["archived_at"]).startswith("2026-06-01")
    stored = await _archive_row(backend, "mem-cold")
    assert stored["compression_algo"] == "zstd"
    assert stored["schema_version"] == 1
    # Archived in place: the row survives the sweep.
    assert await _count(backend) == before


@pytest.mark.asyncio
async def test_the_policy_is_archive_only_with_no_delete_switch(backend, clock):
    """DIVERGENCE: there is no "policy says delete" branch to opt into."""
    params = list(inspect.signature(wl.sweep_for_archival).parameters)
    assert params == ["backend", "namespace", "archive_after_days", "batch_size"]

    await _insert(backend, "mem-cold", created_days=60)
    before = await _count(backend)

    await _sweep(backend)

    assert await _count(backend) == before


# ── the window and the timestamp it is measured against ──────────────────────


@pytest.mark.asyncio
async def test_window_boundary_is_measured_on_created(backend, clock):
    await _insert(backend, "mem-inside", created_days=WINDOW_DAYS - 1)
    await _insert(backend, "mem-outside", created_days=WINDOW_DAYS + 1)

    archived = await _sweep(backend)

    assert archived == 1
    assert (await _row(backend, "mem-inside"))["archived_at"] is None
    assert (await _row(backend, "mem-outside"))["archived_at"] is not None


@pytest.mark.asyncio
async def test_a_recent_update_does_not_rescue_a_cold_memory(backend, clock):
    """Age is ``created``, not ``updated``: touching a row does not reset retention."""
    await _insert(backend, "mem-touched", created_days=60, updated_days=1)

    assert await _sweep(backend) == 1
    assert (await _row(backend, "mem-touched"))["archived_at"] is not None


@pytest.mark.asyncio
async def test_another_namespace_is_not_swept(backend, clock):
    await _insert(backend, "mem-other", created_days=60, namespace="other-ns")

    assert await _sweep(backend) == 0
    assert (await _row(backend, "mem-other"))["archived_at"] is None


# ── what protects a row ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recently_recalled_memory_is_protected(backend, clock):
    """The hot-set guard: recalled inside the window means still in use."""
    await _insert(backend, "mem-hot", created_days=60, last_recalled_days=1)
    await _insert(backend, "mem-cold", created_days=60, last_recalled_days=WINDOW_DAYS + 10)

    archived = await _sweep(backend)

    assert archived == 1
    assert (await _row(backend, "mem-hot"))["archived_at"] is None
    assert (await _row(backend, "mem-cold"))["archived_at"] is not None


@pytest.mark.asyncio
async def test_a_consolidated_memory_is_protected(backend, clock):
    # ``consolidated_into`` is a real FK, so the canonical row has to exist.
    await _insert(backend, "mem-canonical", created_days=5)
    await _insert(backend, "mem-merged", created_days=60, consolidated_into="mem-canonical")

    assert await _sweep(backend) == 0
    assert (await _row(backend, "mem-merged"))["archived_at"] is None
    assert (await _row(backend, "mem-canonical"))["archived_at"] is None


@pytest.mark.asyncio
async def test_there_is_no_pin_column_so_a_pinned_memory_cannot_be_exempt(backend, clock):
    """DIVERGENCE: the brief's "pinned memory is never archived" has no mechanism.

    ``memories`` carries no pin column on any backend, and the sweep consults
    none, so an operator who pins a memory has no way to keep it out of the
    archive; the hot set (``last_recalled_at``) is the only recency guard.
    """
    async with backend.transactional() as tx:
        ddl = await _fetch_one(
            tx.conn,
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memories'",
            (),
        )
    assert "pin" not in ddl["sql"].lower()

    # A cold, never-recalled memory is archived and there is no column to set.
    await _insert(backend, "mem-would-be-pinned", created_days=60)
    assert await _sweep(backend) == 1
    assert (await _row(backend, "mem-would-be-pinned"))["archived_at"] is not None


# ── dry run ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_has_no_dry_run_mode(backend, clock):
    """DIVERGENCE: ``dry_run`` does not exist on this path."""
    params = inspect.signature(wl.sweep_for_archival).parameters
    assert "dry_run" not in params
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@pytest.mark.asyncio
async def test_an_eligible_preview_reports_the_ids_and_writes_nothing(backend, clock):
    """The only preview available: run the eligible SELECT, then sweep for real."""
    await _insert(backend, "mem-cold", created_days=60, updated_days=0)
    await _insert(backend, "mem-warm", created_days=5)

    cutoff = NOW - timedelta(days=WINDOW_DAYS)
    async with backend.transactional() as tx:
        preview = await _fetch_one(
            tx.conn,
            "SELECT id FROM memories "
            "WHERE deleted_at IS NULL AND archived_at IS NULL "
            "AND consolidated_into IS NULL "
            "AND (last_recalled_at IS NULL OR last_recalled_at < ?) "
            "AND created < ? AND namespace = ? "
            "ORDER BY created ASC",
            (cutoff, cutoff, NS),
        )

    assert preview["id"] == "mem-cold"
    # The preview wrote nothing…
    assert (await _row(backend, "mem-cold"))["archived_at"] is None
    assert await _archive_row(backend, "mem-cold") is None
    # …and the real sweep then archives exactly that id.
    assert await _sweep(backend) == 1
    assert (await _row(backend, "mem-cold"))["archived_at"] is not None
    assert (await _row(backend, "mem-warm"))["archived_at"] is None


# ── batching and validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_size_bounds_one_pass_and_drains_oldest_first(backend, clock):
    for index in range(5):
        await _insert(backend, f"mem-{index}", created_days=60 + index)

    assert await _sweep(backend, batch=2) == 2
    assert (await _row(backend, "mem-4"))["archived_at"] is not None  # oldest first
    assert await _sweep(backend, batch=2) == 2
    assert await _sweep(backend, batch=2) == 1
    assert await _sweep(backend, batch=2) == 0


@pytest.mark.asyncio
async def test_a_window_below_one_day_is_rejected(backend, clock):
    with pytest.raises(ValueError):
        await wl.sweep_for_archival(backend, namespace=NS, archive_after_days=0, batch_size=10)
    with pytest.raises(ValueError):
        await wl.sweep_for_archival(backend, namespace=NS, archive_after_days=30, batch_size=0)
