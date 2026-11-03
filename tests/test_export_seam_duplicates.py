"""Keyset-seam duplicates: the server drops them, the clients count one set.

Live evidence (PYTHIA, read-only export 2026-11-03): the deployed Oracle
server re-emitted 3 boundary rows across 29 pages of 1000 and its completion
marker declared the wire count (28455) while only 28452 identities were
unique. STYX then failed every night with
``materialized MPF record count does not match validated stream (N-k != N)``
because its validator and ``write_export`` counted different sets.
"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mnemos.tools.export_stream import ExportValidationError, completion_count_matches, write_export


def _record(number):
    return {
        "id": f"mem_{number}",
        "kind": "memory",
        "payload": {"created": "2026-11-02T09:54:19.168429+00:00", "content": "x" * 50},
    }


def _pages(ids_per_page, declared):
    for ids in ids_per_page:
        yield {"mpf_version": "0.2", "records": [_record(i) for i in ids]}
    yield {"export_complete": True, "record_count": declared}


# ── completion-count rule shared by the STYX validator and write_export ──


@pytest.mark.parametrize(
    ("declared", "ok"),
    [(5, True), (8, True), (6, False), (4, False), (-1, False), (True, False), ("5", False), (None, False)],
)
def test_completion_count_accepts_unique_or_wire_only(declared, ok):
    assert completion_count_matches(declared, unique=5, wire=8) is ok


def test_write_export_accepts_deployed_server_wire_count_and_writes_unique_records(tmp_path):
    out = tmp_path / "export.mpf.json"
    # Seams at 1 and at 2/3: wire 8, unique 5, marker = wire (deployed server).
    count = write_export(_pages([[0, 1], [1, 2, 3], [2, 3, 4]], declared=8), out)
    assert count == 5
    exported = json.loads(out.read_text())
    assert exported["record_count"] == 5
    assert [r["id"] for r in exported["records"]] == [f"mem_{i}" for i in range(5)]


def test_write_export_accepts_seam_guarded_server_unique_count(tmp_path):
    out = tmp_path / "export.mpf.json"
    assert write_export(_pages([[0, 1], [2, 3], [4]], declared=5), out) == 5


@pytest.mark.parametrize("declared", [4, 6, 9])
def test_write_export_rejects_marker_matching_neither_count(tmp_path, declared):
    out = tmp_path / "export.mpf.json"
    with pytest.raises(ExportValidationError, match="completion count"):
        write_export(_pages([[0, 1], [1, 2, 3], [2, 3, 4]], declared=declared), out)
    assert not out.exists()


def test_write_export_still_rejects_divergent_repeat(tmp_path):
    def pages():
        yield {"records": [_record(0), _record(1)]}
        changed = _record(1)
        changed["payload"]["content"] = "TAMPERED"
        yield {"records": [changed, _record(2)]}
        yield {"export_complete": True, "record_count": 4}

    with pytest.raises(ExportValidationError, match="divergent duplicate"):
        write_export(pages(), tmp_path / "export.mpf.json")


# ── server: stream_export (page-buffered) drops seam repeats ──


class _Backend:
    def __init__(self):
        self.active = []

    @asynccontextmanager
    async def transactional(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        self.active.append("tx")
        try:
            yield SimpleNamespace(name="tx")
        finally:
            self.active.pop()


def _envelope(records, raw_last_created):
    page = {"records": records}
    return SimpleNamespace(model_dump=lambda **_: dict(page), raw_last_created=raw_last_created)


_T0 = datetime(2026, 11, 2, 9, 54, 19, 168429, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stream_export_drops_seam_repeats_and_declares_unique_count(monkeypatch):
    """A lossy cursor bind makes page 2 start with page 1's last two rows
    (the live 2026-11-03 page-29 shape). The stream must emit each identity
    once and declare the unique count."""
    from mnemos.domain.portability import stream

    served = iter(
        [
            [_record(0), _record(1), _record(2)],
            [_record(1), _record(2), _record(3)],  # two-row seam re-emission
            [_record(4)],
        ]
    )

    async def export(_backend, _tx, **_kwargs):
        return _envelope(next(served), _T0)

    monkeypatch.setattr(stream, "export_memories", export)
    lines = [json.loads(line) async for line in stream.stream_export(_Backend(), limit=3, offset=0)]
    ids = [r["id"] for page in lines[:-1] for r in page["records"]]
    assert ids == ["mem_0", "mem_1", "mem_2", "mem_3", "mem_4"]
    assert lines[-1] == {"export_complete": True, "record_count": 5}
    # The marker now satisfies both the old wire-count client and new clients.
    assert len(ids) == lines[-1]["record_count"]


@pytest.mark.asyncio
async def test_stream_export_fails_closed_when_a_full_page_is_all_repeats(monkeypatch):
    from mnemos.domain.portability import stream

    served = iter([[_record(0), _record(1)], [_record(0), _record(1)]])

    async def export(_backend, _tx, **_kwargs):
        return _envelope(next(served), _T0)

    monkeypatch.setattr(stream, "export_memories", export)
    with pytest.raises(RuntimeError, match="did not advance"):
        _ = [line async for line in stream.stream_export(_Backend(), limit=2, offset=0)]


# ── server: stream_export_records (per-record framing) drops seam repeats ──


@pytest.mark.asyncio
async def test_per_record_stream_drops_seam_repeats_and_declares_unique_count(monkeypatch):
    from mnemos.domain.portability import stream

    pages = iter([[0, 1, 2], [2, 3, 4], [4, 5]])
    cursors = []

    async def iter_page_records(_backend, _tx, **kwargs):
        cursors.append(kwargs["record_cursor"])
        for number in next(pages):
            yield (
                SimpleNamespace(id=f"mem_{number}", model_dump=lambda n=number, **_: _record(n)),
                _T0 + timedelta(seconds=number),
            )

    sidecar_ids = []

    async def build_sidecars(_backend, _tx, *, memory_ids, **_kwargs):
        sidecar_ids.append(list(memory_ids))
        return {
            "kg_triples": None,
            "memory_versions": None,
            "compression_manifest": None,
            "deletion_log": None,
            "deletion_log_next_cursor": None,
        }

    monkeypatch.setattr(stream, "_iter_page_records", iter_page_records)
    monkeypatch.setattr(stream, "_build_export_sidecars", build_sidecars)
    lines = [
        json.loads(line)
        async for line in stream.stream_export_records(
            _Backend(),
            user=SimpleNamespace(role="root", user_id="u", namespace="n"),
            category=None,
            limit=3,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=False,
        )
    ]
    ids = [r["id"] for line in lines for r in line.get("records", [])]
    assert ids == [f"mem_{i}" for i in range(6)]
    assert lines[-1] == {"export_complete": True, "record_count": 6}
    # Sidecars are fetched for the NEW ids only; the keyset still advances
    # from the raw last row of each page, repeats included.
    assert sidecar_ids == [["mem_0", "mem_1", "mem_2"], ["mem_3", "mem_4"], ["mem_5"]]
    assert cursors[1] == (_T0 + timedelta(seconds=2), "mem_2")
    assert cursors[2] == (_T0 + timedelta(seconds=4), "mem_4")
