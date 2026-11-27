"""Pins for the PERSEPHONE eligibility SQL and archive check (2026-11-06).

Scope: ``mnemos/domain/persephone/runner.py`` — ``eligible_sql`` and
``is_archived``. No database: the SQL is asserted as text and the archive check
runs against a fake connection exposing only ``fetchval``.

The module sits behind the ``persephone`` extra and imports ``zstandard`` at
import time, so it is loaded by path with a stand-in codec when that package is
absent; ``sys.modules`` is restored afterwards so no other test sees the stub.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mnemos.core import eligibility

UTC = timezone.utc
RUNNER_PATH = Path(__file__).resolve().parents[1] / "mnemos" / "domain" / "persephone" / "runner.py"


# The runner imports ``mnemos.persistence.archive_format``, which itself imports
# ``zstandard`` at module scope. Both may already be in ``sys.modules`` because
# another test imported them; whatever this loader replaces or evicts must be
# put back EXACTLY as it was found, or a later test re-executes a real module
# (fresh module dict, rebound function objects) or inherits this stub.
_STUBBED_MODULES = ("zstandard", "mnemos.persistence.archive_format")


def _load_runner():
    stub = None
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
    if importlib.util.find_spec("zstandard") is None:
        stub = types.ModuleType("zstandard")

        class _Codec:
            def compress(self, data):
                return b"stub:" + data

            def decompress(self, data):
                return bytes(data)[len(b"stub:") :]

        stub.ZstdCompressor = _Codec
        stub.ZstdDecompressor = _Codec
        sys.modules["zstandard"] = stub

    try:
        spec = importlib.util.spec_from_file_location("persephone_runner_20261106", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore, never pop blindly: a module that was absent before is
        # removed; one that was already imported is left untouched, so this
        # loader never evicts (or retains a stub over) another test's import.
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


runner = _load_runner()


def _sql() -> str:
    return runner.eligible_sql()


def _flat() -> str:
    return " ".join(_sql().split())


def _run(coro):
    return asyncio.run(coro)


class _FakeConn:
    """Connection exposing only fetchval, so any other call would fail."""

    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *params):
        self.calls.append((sql, tuple(params)))
        if self.error is not None:
            raise self.error
        return self.value


@pytest.fixture()
def fence_off(monkeypatch):
    monkeypatch.setattr(eligibility, "_replica_fence_enabled", lambda: False)


@pytest.fixture()
def fence_on(monkeypatch):
    monkeypatch.setattr(eligibility, "_replica_fence_enabled", lambda: True)


# ------------------------------------------------------------------ eligible_sql


def test_eligible_sql_takes_no_arguments():
    assert list(inspect.signature(runner.eligible_sql).parameters) == []


def test_eligible_sql_selects_only_the_id_column(fence_on):
    sql = _flat()
    assert sql.startswith("SELECT id FROM memories WHERE ")
    assert "SELECT *" not in sql
    assert " LIKE " not in sql
    assert "%" not in sql


def test_eligible_sql_carries_the_documented_predicates(fence_on):
    sql = _flat()
    for predicate in (
        "deleted_at IS NULL",
        "archived_at IS NULL",
        "consolidated_into IS NULL",
        "federation_source IS NULL",
        "namespace = $1",
    ):
        assert predicate in sql
    # Retention: last recall (or never recalled) AND creation are both older
    # than the day window supplied as $2.
    assert "(last_recalled_at IS NULL OR last_recalled_at < NOW() - ($2::int * INTERVAL '1 day'))" in sql
    assert "created < NOW() - ($2::int * INTERVAL '1 day')" in sql


def test_eligible_sql_is_bounded_and_locked(fence_on):
    sql = _flat()
    assert "ORDER BY created ASC" in sql
    assert "LIMIT $3" in sql
    assert sql.endswith("FOR UPDATE SKIP LOCKED")


def test_eligible_sql_uses_exactly_three_parameter_slots(fence_on):
    sql = _flat()
    assert set(re.findall(r"\$\d+", sql)) == {"$1", "$2", "$3"}
    assert "$4" not in sql
    assert sql.count("$1") == 1
    assert sql.count("$2") == 2
    assert sql.count("$3") == 1


def test_eligible_sql_leaves_no_format_placeholders(fence_on):
    assert "{" not in _sql()
    assert "}" not in _sql()


def test_fence_on_adds_the_replica_predicate(fence_on):
    sql = _flat()
    assert "AND federation_source IS NULL" in sql
    assert "m.federation_source" not in sql


def test_fence_off_omits_the_replica_predicate(fence_off):
    sql = _flat()
    assert "federation_source" not in sql
    assert "archived_at IS NULL" in sql


def test_fence_is_resolved_on_every_call(monkeypatch):
    monkeypatch.setattr(eligibility, "_replica_fence_enabled", lambda: True)
    with_fence = runner.eligible_sql()
    monkeypatch.setattr(eligibility, "_replica_fence_enabled", lambda: False)
    without_fence = runner.eligible_sql()
    assert "federation_source" in with_fence
    assert "federation_source" not in without_fence
    assert with_fence != without_fence


def test_eligible_sql_is_identical_across_calls(fence_on):
    assert runner.eligible_sql() == runner.eligible_sql() == _sql()


# -------------------------------------------------------------------- is_archived


def test_is_archived_true_for_a_live_archive_stub():
    conn = _FakeConn(datetime(2026, 1, 6, 12, 0, tzinfo=UTC))
    assert _run(runner.is_archived(conn, "mem_1")) is True


def test_is_archived_false_when_archived_at_is_null():
    assert _run(runner.is_archived(_FakeConn(None), "mem_1")) is False


@pytest.mark.parametrize("value", [0, "", False, 0.0])
def test_is_archived_treats_any_non_none_value_as_archived(value):
    # The check is `is not None`, not truthiness: a falsy non-null marker
    # still reads as archived.
    assert _run(runner.is_archived(_FakeConn(value), "mem_1")) is True


def test_is_archived_queries_the_live_row_by_id():
    conn = _FakeConn(None)
    _run(runner.is_archived(conn, "mem_42"))
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert " ".join(sql.split()) == "SELECT archived_at FROM memories WHERE id = $1 AND deleted_at IS NULL"
    assert params == ("mem_42",)


def test_is_archived_scopes_the_lookup_per_call():
    conn = _FakeConn(None)
    _run(runner.is_archived(conn, "mem_a"))
    _run(runner.is_archived(conn, "mem_b"))
    assert [params for _sql, params in conn.calls] == [("mem_a",), ("mem_b",)]


def test_is_archived_propagates_a_driver_error():
    # "Never raises on a missing column" does not hold: there is no row access
    # to guard, so an unknown-column error from fetchval surfaces unchanged.
    conn = _FakeConn(error=KeyError("archived_at"))
    with pytest.raises(KeyError):
        _run(runner.is_archived(conn, "mem_1"))
    assert len(conn.calls) == 1


def test_is_archived_returns_a_bool_not_the_raw_value():
    result = _run(runner.is_archived(_FakeConn("2026-01-06"), "mem_1"))
    assert result is True
    assert type(result) is bool


# ------------------------------------------------------- loader module hygiene


def test_loader_removes_modules_it_had_to_create(monkeypatch):
    # Findings FB404: the archive_format pop was unconditional. Whatever the
    # loader synthesises must be gone afterwards so no other test inherits it.
    monkeypatch.delitem(sys.modules, "mnemos.persistence.archive_format", raising=False)
    monkeypatch.delitem(sys.modules, "zstandard", raising=False)

    _load_runner()

    assert "mnemos.persistence.archive_format" not in sys.modules


def test_loader_restores_a_real_module_without_reexecuting_it(monkeypatch):
    # The end-to-end shape of the defect: a module another test already
    # imported must survive the loader byte-for-byte. Popping it would force a
    # re-import -- a fresh module dict and rebound function objects -- so
    # identity is checked, not just presence, and the module is proven not to
    # have been re-executed.
    pytest.importorskip("zstandard")
    import mnemos.persistence.archive_format as archive_format

    before = archive_format.__dict__.copy()
    monkeypatch.setitem(sys.modules, "mnemos.persistence.archive_format", archive_format)

    _load_runner()

    assert sys.modules["mnemos.persistence.archive_format"] is archive_format
    # Same namespace: nothing re-ran module-level code (the zstd import, the
    # constant definitions) behind the other test's back.
    assert archive_format.__dict__.keys() == before.keys()
    assert archive_format._archive_payload is before["_archive_payload"]
