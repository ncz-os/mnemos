"""F-06 slice 2: KRONOS MCP tools are classified Postgres-only and say so.

``mnemos/mcp/tools/kronos.py`` hands ``_lc._pool`` to the KRONOS domain
functions. Those functions are genuinely Postgres-only: their SQL uses
``date_trunc``/``AT TIME ZONE``/``NOW() - n * INTERVAL`` and a two-stage
``memory_recall_log`` -> ``memories`` fallback that only exists on the
Postgres schema. Forcing them through the persistence ABC would mean either
translating the SQL per backend or fabricating values on backends that lack
``memory_recall_log`` — the §0b rule's "never a fabricated default" clause
excludes both.

The honest contract is therefore:

* on Postgres the tools take the direct pool path (unchanged behaviour), and
* on every other backend they return a null-valued, reason-bearing error
  instead of a fabricated 0-count success.

This file pins the non-Postgres half on the SQLite test backend: the tool
must *not* report ``{"success": True, "count": 0, ...}`` (the pre-classification
shape, which reads as "no anomalies — all quiet") when the backend simply
cannot answer.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from mnemos.core.auth_context import UserContext
from mnemos.core.config import _reset_settings_for_tests, get_settings
from mnemos.mcp.tools import kronos as mcp_kronos
from mnemos.persistence.sqlite import SqliteBackend

pytestmark = pytest.mark.asyncio

ALICE = UserContext(user_id="alice", group_ids=[], role="user", namespace="alice-ns", authenticated=True)
ROOT = UserContext(user_id="root", group_ids=[], role="root", namespace="root", authenticated=True)


@pytest_asyncio.fixture
async def sqlite_backend_no_pool(monkeypatch, tmp_path):
    """A SQLite backend as the active persistence facade, with no Postgres pool.

    Simulates a non-Postgres deployment: ``lifecycle._pool`` is None and the
    only backend available is a SQLite facade that cannot answer KRONOS SQL.
    """
    os.environ["MNEMOS_KRONOS_ENABLED"] = "true"
    _reset_settings_for_tests()
    import mnemos.core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_pool", None, raising=False)
    monkeypatch.setattr(lifecycle, "_persistence_backend", None, raising=False)

    backend = SqliteBackend(str(tmp_path / "kronos_test.db"), get_settings())
    await backend.open()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    yield backend
    await backend.close()
    os.environ.pop("MNEMOS_KRONOS_ENABLED", None)
    _reset_settings_for_tests()


async def test_kronos_anomalies_reports_unsupported_on_non_postgres_backend(sqlite_backend_no_pool, monkeypatch):
    result = await mcp_kronos.tool_kronos_anomalies("root", user=ROOT)

    # Not a fabricated empty success, and not the generic "Database
    # unavailable" — a reason-bearing unsupported-backend error.
    assert result["success"] is False, result
    assert "Postgres" in result["error"], result
    assert "count" not in result, result


async def test_kronos_forecast_reports_unsupported_on_non_postgres_backend(sqlite_backend_no_pool, monkeypatch):
    result = await mcp_kronos.tool_kronos_forecast("root", hours_ahead=12, user=ROOT)

    assert result["success"] is False, result
    assert "Postgres" in result["error"], result
    assert "predicted_total_recalls" not in result, result
