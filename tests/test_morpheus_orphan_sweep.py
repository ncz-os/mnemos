"""Regression coverage for MORPHEUS orphan-timeout sweeps.

Item 11a (ABC migration): the runner's ``sweep_orphan_runs`` now
takes a persistence backend (``backend``) instead of a raw
asyncpg.Pool and routes through ``backend.morpheus.sweep_orphan_runs``.
This test mocks the ``backend`` shape with an in-memory ``_Backend``
fixture whose ``morpheus.sweep_orphan_runs`` impl walks a fake
``runs`` dict and returns ``[{"id": …, "started_at": …}, …]`` rows so
the runner's per-row log line continues to assert which runs were
swept.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import pytest
from typer.testing import CliRunner

from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import sweep_orphan_runs


OLD_RUN_ID = "00000000-0000-0000-0000-000000000509"
FRESH_RUN_ID = "00000000-0000-0000-0000-000000000510"


class _Conn:
    """In-memory stand-in for the SQL backend used by the test."""

    def __init__(self, *, now: datetime):
        self.now = now
        self.runs: dict[str, dict] = {}

    def insert_morpheus_run(
        self,
        run_id: str,
        *,
        started_at: datetime,
        status: str = "running",
    ) -> None:
        self.runs[run_id] = {
            "id": run_id,
            "started_at": started_at,
            "status": status,
            "error": None,
            "finished_at": None,
        }


class _Morpheus:
    """Stand-in for ``backend.morpheus`` — implements only
    ``sweep_orphan_runs`` for this test."""

    def __init__(self, conn: _Conn):
        self._conn = conn

    async def sweep_orphan_runs(self, tx, *, threshold_hours: float):
        cutoff = self._conn.now - timedelta(hours=float(threshold_hours))
        swept = []
        for row in self._conn.runs.values():
            if row["status"] != "running" or row["started_at"] >= cutoff:
                continue
            row["status"] = "failed"
            row["error"] = "orphan_timeout_sweep"
            row["finished_at"] = self._conn.now
            swept.append({"id": row["id"], "started_at": row["started_at"]})
        return swept


class _Backend:
    """Backend-shaped mock — has ``morpheus`` and ``transactional``."""

    def __init__(self, conn: _Conn):
        self._conn = conn
        self.morpheus = _Morpheus(conn)
        self.transactional_calls = 0

    def transactional(self):
        """No-op async context manager (sweep_orphan_runs just calls
        into ``self.morpheus.sweep_orphan_runs`` inside the ``async with``
        block; the ABC impl uses the tx to dispatch the SELECT/UPDATE
        so the test mock doesn't need real transaction semantics)."""
        backend = self

        class _Ctx:
            async def __aenter__(self_inner):
                backend.transactional_calls += 1
                return None

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


def _old_and_fresh_pool() -> tuple[_Backend, _Conn]:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    conn = _Conn(now=now)
    conn.insert_morpheus_run(
        OLD_RUN_ID,
        started_at=now - timedelta(hours=3),
        status="running",
    )
    conn.insert_morpheus_run(
        FRESH_RUN_ID,
        started_at=now - timedelta(hours=1),
        status="running",
    )
    backend = _Backend(conn)
    return backend, conn


@pytest.mark.asyncio
async def test_sweep_orphan_runs_marks_old_running_rows_failed(caplog):
    backend, conn = _old_and_fresh_pool()
    caplog.set_level(logging.INFO, logger=runner.__name__)

    swept = await sweep_orphan_runs(backend, max_age_hours=2)

    assert swept == 1
    old = conn.runs[OLD_RUN_ID]
    assert old["status"] == "failed"
    assert old["error"] == "orphan_timeout_sweep"
    assert old["finished_at"] == conn.now
    assert conn.runs[FRESH_RUN_ID]["status"] == "running"
    assert OLD_RUN_ID in caplog.text
    assert str(old["started_at"]) in caplog.text


def test_morpheus_sweep_orphans_cli_prints_swept_count(monkeypatch):
    from mnemos.cli import main as cli_main

    backend, conn = _old_and_fresh_pool()

    async def _open_backend():
        # The CLI now opens a backend via ``_open_cli_persistence_backend``
        # (post-item-11a). The fake backend stands in for that.
        return backend, False

    monkeypatch.setattr(cli_main, "_open_cli_persistence_backend", _open_backend)
    result = CliRunner().invoke(
        cli_main.app,
        ["morpheus", "sweep-orphans", "--max-age-hours", "2"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Swept 1 MORPHEUS orphan run(s)."
    assert conn.runs[OLD_RUN_ID]["status"] == "failed"
    assert conn.runs[OLD_RUN_ID]["error"] == "orphan_timeout_sweep"
