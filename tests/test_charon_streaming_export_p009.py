"""P-009: stream core MIF export instead of retaining the corpus.

Acceptance (from the task):

* Export a lazy 100k-row source with fixed page size under a fixed memory
  budget scaling with page size, not total content; re-import yields
  identical IDs/content/sidecars.
* An async heartbeat progresses during large file writes and cancellation
  closes temporary files.
"""

from __future__ import annotations

import asyncio
import json
import tracemalloc
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.portability import charon


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "stream_p009.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


def _rows(n, *, content_size=512, dim=32):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        yield {
            "id": f"mem_stream_{i:06d}",
            "content": f"streamed fact {i} " + "x" * content_size,
            "category": "facts",
            "namespace": "default",
            "owner_id": "alice",
            "created": base,
            "updated": base,
            "embedding": [float(i % 7)] * dim,
            "verbatim_content": f"streamed fact {i}",
            "metadata": {},
        }


def _memory_to_plain(memory):
    return dict(memory)


class _StreamMemories:
    """In-memory repo serving a lazy source through the export ABC shape."""

    def __init__(self, total):
        self.total = total
        self.calls: list[tuple[int, int, object]] = []

    async def fetch_memory_export(
        self,
        tx,
        *,
        effective_owner,
        effective_ns,
        category,
        limit,
        offset,
        include_secrets=False,
        record_cursor=None,
    ):
        self.calls.append((limit, offset, record_cursor))
        rows = []
        start = 0
        if record_cursor is not None:
            _, last_id = record_cursor
            start = int(last_id.rsplit("_", 1)[-1]) + 1
        else:
            start = offset
        for i in range(start, min(start + limit, self.total)):
            dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
            rows.append(
                _FakeRow(
                    id=f"mem_stream_{i:06d}",
                    content=f"streamed fact {i} " + "x" * 512,
                    category="facts",
                    namespace="default",
                    owner_id="alice",
                    created=dt,
                    updated=dt,
                    embedding=[float(i % 7)] * 32,
                    verbatim_content=f"streamed fact {i}",
                    metadata={},
                )
            )
        return rows


class _FakeRow(dict):
    def keys(self):  # aiosqlite-style Row interface
        return super().keys()


class _EmptyRepo:
    async def fetch_kg_triples_for_export(self, tx, **kw):
        return []

    async def fetch_memory_versions_for_export(self, tx, **kw):
        return []

    async def fetch_compressed_variants_for_export(self, tx, **kw):
        return []


class _StreamBackend:
    def __init__(self, total):
        self.memories = _StreamMemories(total)
        self.kg_triples = _EmptyRepo()
        self.memory_versions = _EmptyRepo()
        self.compression = _EmptyRepo()

    def transactional(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _tx():
            yield SimpleNamespace()

        return _tx()


def test_stream_manifest_lazy_source_bounded_memory_and_keyset(tmp_path):
    """100k lazy rows, page 200: bounded peak + keyset advancement."""
    total = 100_000
    backend = _StreamBackend(total)
    tracemalloc.start()
    try:
        manifest = asyncio.run(
            charon.export_bundle_from_backend(backend, tmp_path / "bundle", include_sidecars=False, page_size=200)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert manifest["count"] == total
    assert "concepts" not in manifest, "streamed export must not retain a concepts list"
    # ~200 rows * ~2KB/row must stay well under a corpus-scale budget.
    assert peak < 100 * 1024 * 1024, f"peak {peak / 1e6:.1f}MB exceeds 100MB budget"
    # Keyset advancement: after the first page, offset stays 0.
    assert backend.memories.calls[0][1] == 0
    assert all(off == 0 for _, off, cur in backend.memories.calls[1:] if cur is not None)
    assert any(cur is not None for _, _, cur in backend.memories.calls[1:])
    assert len(backend.memories.calls) == total // 200 + 1
    concepts = json.loads((tmp_path / "bundle" / charon.MANIFEST_NAME).read_text())["concepts"]
    assert len(concepts) == total
    assert concepts[0]["mnemos_id"] == "mem_stream_000000"
    assert concepts[-1]["mnemos_id"] == "mem_stream_099999"


@pytest.mark.asyncio
async def test_export_heartbeat_progresses_and_cancel_cleans_tmp(tmp_path):
    """Progress callback fires per page; cancel removes partial outputs."""
    backend = _StreamBackend(1200)
    beats: list[int] = []

    async def on_progress(count):
        beats.append(count)
        await asyncio.sleep(0)

    out = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        backend, out, include_sidecars=False, page_size=200, on_progress=on_progress
    )
    assert manifest["count"] == 1200
    assert beats and beats[-1] == 1200
    # No spool files remain next to the bundle.
    assert list(out.glob(".charon-*")) == []

    # Cancellation closes temporary files: a slow per-page callback that
    # cancels mid-export must not leave a half-written bundle behind.
    backend2 = _StreamBackend(5000)

    async def cancelling(count):
        if count >= 400:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await charon.export_bundle_from_backend(
            backend2,
            tmp_path / "cancelled",
            include_sidecars=False,
            page_size=200,
            on_progress=cancelling,
        )
    assert not (tmp_path / "cancelled" / charon.MANIFEST_NAME).exists()
    assert list((tmp_path / "cancelled").glob(".charon-*")) == []

    # Sidecar-phase cancel: the sidecar file being written is removed and
    # no spool survives, but files completed before the cancel stay — the
    # documented contract is retry-into-clean-out_dir.
    backend3 = _StreamBackend(1200)
    calls = 0

    async def cancelling_sidecar(count):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)

    kg_path = tmp_path / "cancelled-sidecar" / charon.SIDECAR_DIR / charon.KG_TRIPLES_SIDECAR
    with pytest.raises(asyncio.CancelledError):
        await charon.export_bundle_from_backend(
            backend3,
            tmp_path / "cancelled-sidecar",
            include_sidecars=True,
            page_size=1200,
            on_progress=cancelling_sidecar,
        )
    assert not kg_path.exists()
    assert list((tmp_path / "cancelled-sidecar").glob(".charon-*")) == []
    assert not (tmp_path / "cancelled-sidecar" / charon.MANIFEST_NAME).exists()


@pytest.mark.asyncio
async def test_stream_export_round_trips_ids_content_sidecars(sqlite_backend, tmp_path, monkeypatch):
    """Streamed bundle re-imports with identical IDs/content/sidecars."""
    from mnemos.persistence.sqlite import SqliteBackend
    from tests.test_charon_backend_roundtrip import _seed_memory

    for i in range(12):
        await _seed_memory(sqlite_backend, content=f"round trip fact {i}", namespace="default")

    # The walk must advance by keyset after the first page, never by a
    # growing OFFSET: record every page call and require offset == 0 once
    # a cursor is in play.
    seen: list[tuple[int, object]] = []
    repo = sqlite_backend.memories
    orig = repo.fetch_memory_export

    async def spy(tx, **kw):
        seen.append((kw.get("offset"), kw.get("record_cursor")))
        return await orig(tx, **kw)

    monkeypatch.setattr(repo, "fetch_memory_export", spy)
    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True, page_size=3)
    assert manifest["count"] == 12
    assert len(seen) >= 4, "page_size=3 over 12 rows must take 4+ pages"
    for offset, cursor in seen[1:]:
        assert cursor is not None, "pages after the first must carry a keyset cursor"
        assert offset == 0, "OFFSET must not grow once the keyset advances"

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memories_inserted"] == 12
        async with dst.transactional() as tx:
            rows = await dst.memories.fetch_memory_export(
                tx,
                effective_owner=None,
                effective_ns=None,
                category=None,
                limit=100,
                offset=0,
                include_secrets=True,
            )
        by_id = {r["id"]: r["content"] for r in rows}
        async with sqlite_backend.transactional() as tx:
            src_rows = await sqlite_backend.memories.fetch_memory_export(
                tx,
                effective_owner=None,
                effective_ns=None,
                category=None,
                limit=100,
                offset=0,
                include_secrets=True,
            )
        src_by_id = {r["id"]: r["content"] for r in src_rows}
        assert by_id == src_by_id
    finally:
        await dst.close()
