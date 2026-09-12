"""Regression coverage for MORPHEUS extract run counters.

Item 11a (ABC migration): ``phase_extract`` internally calls
``_get_backend()`` to dispatch ``update_counters`` through the new
ABC on the early-exit branches (``run_row is None`` or
``_extract_enabled`` is False). The current test takes the happy
path so it doesn't trigger that lookup, but the autouse fixture
below wires a no-op backend into the lifecycle for every test in
defense-in-depth style so a future regression that adds a new
``_get_backend()`` call site in the extract path doesn't break
this test.
"""
from __future__ import annotations

import pytest

from mnemos.core import config as core_config
from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import ExtractedTriple, phase_extract
from tests.test_morpheus_extract import RUN_ID, _Conn, _Pool, _memory


@pytest.fixture(autouse=True)
def reset_morpheus_extract_settings(monkeypatch):
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFY", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CHARS", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MUSE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFIER", raising=False)
    core_config._reset_settings_for_tests()
    yield
    core_config._reset_settings_for_tests()


@pytest.fixture(autouse=True)
def _install_noop_morpheus_backend(monkeypatch):
    """Wire a no-op backend into the lifecycle global for every test.

    Item 11a: ``phase_extract`` internally calls ``_get_backend()`` to
    dispatch ``update_counters`` through the new ABC. The lifecycle
    global ``_persistence_backend`` is None by default in this test
    process, so wire a no-op backend for the duration of each test
    so a future regression that adds a new ``_get_backend()`` call
    site in the extract path doesn't break this test.
    """
    from mnemos.core import lifecycle as _lifecycle
    from tests.test_morpheus_extract import _Backend

    monkeypatch.setattr(_lifecycle, "_persistence_backend", _Backend(_Conn()))


@pytest.mark.asyncio
async def test_phase_extract_increments_run_counter_mid_phase(monkeypatch):
    conn = _Conn(memories=[
        _memory("mem_0", created_offset=0),
        _memory("mem_1", created_offset=1),
    ])
    calls = 0

    async def one_triple(content: str) -> list[ExtractedTriple]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert conn.run_row["triples_extracted"] > 0
            assert conn.run_row["memories_processed_for_extraction"] > 0
        memory_id = content.split()[0]
        return [
            ExtractedTriple(
                f"{memory_id}:subject",
                "relates_to",
                f"{memory_id}:object",
                0.9,
            )
        ]

    monkeypatch.setattr(runner, "_extract_triples_from_prose", one_triple)

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert calls == 2
    assert n == 2
    assert conn.run_row["triples_extracted"] == 2
    assert conn.run_row["memories_processed_for_extraction"] == 2
