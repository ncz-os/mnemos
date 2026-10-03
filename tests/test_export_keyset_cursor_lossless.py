"""Streaming-export keyset must never re-emit or skip a boundary row.

Regression for the 2026-10-03 intermittent STYX offsite backup failures:
the Oracle streaming export emitted the last record of page N again as the
first record of page N+1 (byte-identical duplicate at the page seam)
whenever rows shared `created` timestamps and the page boundary fell inside
the tie group. The WHERE clause was already strict — the loss came from the
cursor VALUE the driver bound between pages.

These tests prove the invariant the server owes the STYX client:

1. Keyset walks over rows sharing `created` timestamps, with the Oracle
   ordering `(created ASC, id ASC)` and page sizes 1 and 1000, emit every
   id EXACTLY ONCE — no duplicates at the page seam, no skipped rows — on
   both the pure keyset predicate and a full-corpus in-memory backend.
2. A real SQLite-backed streaming export over tied timestamps emits every
   id exactly once (page sizes 1 and 1000).
3. On Oracle, the cursor timestamp is bound with an explicit
   DB_TYPE_TIMESTAMP_TZ bind (the same pattern the federation feed keyset
   uses), so the driver can never coerce the bind to a lossy type.

The STYX client-side defense (tolerate byte-identical duplicates on the
wire, still fail hard on divergent ones) is covered in
tests/test_styx_source.py.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

TIE_TS = datetime(2026, 10, 1, 12, 30, 45, 987654, tzinfo=UTC)
TIE_TS_STR = TIE_TS.replace(tzinfo=None).isoformat()  # SQLite stores naive ISO text


def _oracle_style_order(rows):
    """ORDER BY created ASC, id ASC — the exact ordering the export walks."""
    return sorted(rows, key=lambda r: (r["created"], r["id"]))


def _keyset_walk(rows, *, limit, record_cursor=None):
    """The keyset predicate the backends encode, in Python.

    Strictly greater than (created, id): `(created > c OR (created = c AND id > i))`.
    """
    rows = _oracle_style_order(rows)
    if record_cursor is not None:
        cur_created, cur_id = record_cursor
        rows = [r for r in rows if r["created"] > cur_created or (r["created"] == cur_created and r["id"] > cur_id)]
    return rows[:limit]


# ── 1. Page walks: every id exactly once ────────────────────────────────────


def test_keyset_walk_tied_timestamps_page_size_1_and_1000_every_id_exactly_once():
    """Rows sharing `created` straddle page boundaries; page sizes 1 and 1000."""
    rows = []
    # 2,000 rows, all sharing one timestamp, id ordering interleaved so the
    # tie group is contiguous in (created, id) order: ids r0..r1999.
    for i in range(2000):
        rows.append(
            {
                "id": f"mem_{i:05d}",
                "created": TIE_TS_STR,
            }
        )
    rows = _oracle_style_order(rows)
    for limit in (1, 1000):
        seen = []
        record_cursor = None
        while True:
            page = _keyset_walk(rows, limit=limit, record_cursor=record_cursor)
            assert len(page) <= limit
            seen.extend(r["id"] for r in page)
            if len(page) < limit:
                break
            record_cursor = (page[-1]["created"], page[-1]["id"])
        assert seen == [r["id"] for r in rows], "every id exactly once, in (created, id) order"


def test_keyset_walk_mixed_ties_straddling_1000_seam():
    """A 1000-page seam landing mid-tie-group: the boundary row's own cursor
    must exclude itself (strict comparison), so page 2 starts at the NEXT id.
    """
    rows = []
    # 998 rows at T1, then 6 rows at T2 (tie group spans the page-1/2 seam),
    # then 12 rows at T3. Page size 1000 -> seam falls 2 rows into the T2 tie.
    for i in range(998):
        rows.append({"id": f"a_{i:04d}", "created": "2026-10-01T00:00:00"})
    for i in range(6):
        rows.append({"id": f"b_{i}", "created": "2026-10-01T00:00:01"})
    for i in range(12):
        rows.append({"id": f"c_{i:02d}", "created": "2026-10-01T00:00:02"})
    rows = _oracle_style_order(rows)
    seen = []
    record_cursor = None
    while True:
        page = _keyset_walk(rows, limit=1000, record_cursor=record_cursor)
        seen.extend(r["id"] for r in page)
        if len(page) < 1000:
            break
        record_cursor = (page[-1]["created"], page[-1]["id"])
    assert len(seen) == len(set(seen)) == 1016
    assert seen == [r["id"] for r in rows]


# ── 2. Real streaming export over an in-memory full corpus ──────────────────


class _InMemoryCorpusBackend:
    """Backend-neutral fake serving `fetch_memory_export` from an in-memory
    corpus ordered (created ASC, id ASC) — the exact ordering the real
    backends' SQL guarantees. Records are plain dicts (the row shape
    `_iter_page_records` consumes).
    """

    def __init__(self, rows):
        self._rows = _oracle_style_order([dict(r) for r in rows])
        self.memories = self

    @asynccontextmanager
    async def transactional(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        yield SimpleNamespace(conn=SimpleNamespace())

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
        rows = self._rows
        if record_cursor is not None:
            cur_created, cur_id = record_cursor
            # The cursor now arrives as the RAW row value (a str on this
            # fake, matching SQLite's naive-ISO-text domain) — compare in
            # that domain.
            key = cur_created if isinstance(cur_created, str) else cur_created.isoformat().replace("+00:00", "")
            rows = [r for r in rows if r["created"] > key or (r["created"] == key and r["id"] > cur_id)]
        if offset:
            rows = rows[offset:]
        return [dict(r) for r in rows[:limit]]


def _corpus_rows():
    """2,400 rows: 1,990 sharing one timestamp (straddles the 1000/2000
    page seams), then a second tie group of 410."""
    rows = []
    for i in range(1990):
        rows.append(
            {
                "id": f"mem_{i:05d}",
                "content": f"content-{i}",
                "created": TIE_TS_STR,
                "updated": TIE_TS_STR,
            }
        )
    for i in range(410):
        rows.append(
            {
                "id": f"tail_{i:04d}",
                "content": f"tail-{i}",
                "created": "2026-10-01T12:30:46",
                "updated": "2026-10-01T12:30:46",
            }
        )
    return rows


def _root_user():
    from mnemos.core.auth_context import UserContext

    return UserContext(user_id="u1", group_ids=[], role="root", namespace="default", authenticated=True)


def _stream_options(limit):
    return dict(
        user=_root_user(),
        category=None,
        owner_id=None,
        namespace=None,
        include_sidecars=False,
        limit=limit,
        offset=0,
    )


def _drain_stream(backend, limit):
    from mnemos.domain.portability.stream import stream_export

    async def _drain():
        return [line async for line in stream_export(backend, **_stream_options(limit))]

    import asyncio

    return asyncio.run(_drain())


@pytest.mark.parametrize("page_size", [1, 1000])
def test_stream_export_tied_created_page_sizes_1_and_1000_every_id_exactly_once(page_size):
    """stream_export over a corpus whose rows share `created` timestamps
    straddling page boundaries emits every id exactly once, in (created, id)
    order, with the completion count matching the corpus."""
    backend = _InMemoryCorpusBackend(_corpus_rows())
    frames = [json.loads(line) for line in _drain_stream(backend, page_size)]

    record_ids = [r["id"] for frame in frames if frame.get("records") for r in frame["records"]]
    assert len(record_ids) == 2400
    assert len(set(record_ids)) == 2400, "no id may be re-emitted across a page seam"
    # Order must be (created, id): the tie group first (id-ordered), then tail.
    assert record_ids[:1990] == [f"mem_{i:05d}" for i in range(1990)]
    assert record_ids[1990:] == [f"tail_{i:04d}" for i in range(410)]
    assert frames[-1] == {"export_complete": True, "record_count": 2400}


@pytest.mark.parametrize("page_size", [1, 1000])
def test_stream_export_records_tied_created_page_sizes_1_and_1000(page_size):
    """Same guarantee on the per-record framing path (stream_records=true is
    the intra-page variant STYX could equally use)."""
    from mnemos.domain.portability.stream import stream_export_records

    backend = _InMemoryCorpusBackend(_corpus_rows())

    async def _drain():
        return [line async for line in stream_export_records(backend, **_stream_options(page_size))]

    import asyncio

    frames = [json.loads(line) for line in asyncio.run(_drain())]
    record_ids = [r["id"] for frame in frames if frame.get("records") for r in frame["records"]]
    assert len(record_ids) == 2400
    assert len(set(record_ids)) == 2400, "no id may be re-emitted across a page seam"
    assert frames[-1] == {"export_complete": True, "record_count": 2400}


# ── 2b. Real SQLite-backed streaming export (schema via the real backend) ──


async def _sqlite_backend_with_tied_rows(tmp_path: Path):
    from mnemos.persistence.sqlite import SqliteBackend

    path = tmp_path / "keyset.db"
    backend = SqliteBackend(path, SimpleNamespace())
    await backend.open()
    async with backend.transactional() as tx:
        for i in range(1990):
            await backend.memories.insert_memory(
                tx,
                memory_id=f"mem_{i:05d}",
                content=f"content-{i}",
                category="t",
                subcategory=None,
                metadata_json="{}",
                quality_rating=5,
                owner_id="u1",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=None,
                created=TIE_TS_STR,
                updated=TIE_TS_STR,
            )
        for i in range(410):
            await backend.memories.insert_memory(
                tx,
                memory_id=f"tail_{i:04d}",
                content=f"tail-{i}",
                category="t",
                subcategory=None,
                metadata_json="{}",
                quality_rating=5,
                owner_id="u1",
                namespace="default",
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=None,
                created="2026-10-01T12:30:46",
                updated="2026-10-01T12:30:46",
            )
    return backend


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 1000])
async def test_sqlite_streaming_export_tied_created_page_sizes_1_and_1000(tmp_path, page_size):
    """End-to-end over a REAL SQLite store (real migrations, real SQL
    keyset predicate) whose rows share `created` timestamps straddling page
    boundaries: every id exactly once, in order, completion count = corpus.
    """
    from mnemos.domain.portability.stream import stream_export

    backend = await _sqlite_backend_with_tied_rows(tmp_path)
    try:
        frames = [json.loads(line) async for line in stream_export(backend, **_stream_options(page_size))]
    finally:
        await backend.close()

    record_ids = [r["id"] for frame in frames if frame.get("records") for r in frame["records"]]
    assert len(record_ids) == 2400
    assert len(set(record_ids)) == 2400, "no id may be re-emitted across a page seam"
    assert record_ids[:1990] == [f"mem_{i:05d}" for i in range(1990)]
    assert record_ids[1990:] == [f"tail_{i:04d}" for i in range(410)]
    assert frames[-1] == {"export_complete": True, "record_count": 2400}


# ── 3. Oracle: cursor bound as an explicit TIMESTAMP_TZ variable ────────────


def _oracle_timestamp_tz_bind_type():
    """Return oracledb.DB_TYPE_TIMESTAMP_TZ, or None if the driver is absent."""
    try:
        import oracledb
    except ImportError:
        return None
    return oracledb.DB_TYPE_TIMESTAMP_TZ


class _FakeVar:
    def __init__(self, type_code, value=None):
        self.type_code = type_code
        self.value = value

    def setvalue(self, index, value):
        assert index == 0
        self.value = value


async def _fake_fetch_all_dicts(cursor):
    return []


def _fake_var_factory(bind_type):
    """A cursor.var that records what type was requested."""

    def _var(type_code, *args, **kwargs):
        assert type_code == bind_type, "cursor bind must request DB_TYPE_TIMESTAMP_TZ explicitly"
        return _FakeVar(type_code)

    return _var


@pytest.mark.asyncio
async def test_oracle_fetch_memory_export_binds_cursor_as_explicit_timestamp_tz(monkeypatch):
    """The Oracle export keyset must bind its cursor timestamp with an
    explicit DB_TYPE_TIMESTAMP_TZ type — the same pattern the federation feed
    keyset uses (fetch_exportable_memories' :upd bind). Without it, thin-mode
    binds are coerced by the driver and the boundary row can compare as
    greater than its own cursor, re-emitted as the first row of the next page
    (the 2026-10-03 defect; on the feed path, the 2026-05-23 infinite loops).

    Parametrized over the three cursor value domains the raw side channel can
    carry: aware datetime (fast path, bound verbatim), naive datetime
    (normalized to a UTC instant so setvalue accepts it), and ISO string
    (passed through so a naive-text row is never coerced).
    """
    import sys
    import types

    import mnemos.persistence.oracle as oracle_mod

    bind_type = _oracle_timestamp_tz_bind_type()
    if bind_type is None:
        # Driver not installed: stub the module the production code imports
        # at call time. The assert inside _fake_var_factory is what pins the
        # behavior; the stub's type constant is a stand-in.
        bind_type = "TIMESTAMP_TZ"  # stand-in; assert below pins the real driver's constant
        fake = types.ModuleType("oracledb")
        fake.DB_TYPE_TIMESTAMP_TZ = bind_type
        monkeypatch.setitem(sys.modules, "oracledb", fake)

    executed = []
    cursor = SimpleNamespace()
    cursor.description = ()
    cursor.close = lambda: None
    cursor.var = _fake_var_factory(bind_type)

    async def _execute(sql, params):
        executed.append((sql, params))
        return None

    cursor.execute = _execute
    monkeypatch.setattr(oracle_mod, "_conn_from_tx", lambda tx: SimpleNamespace(cursor=lambda: cursor))
    monkeypatch.setattr(oracle_mod, "_fetch_all_dicts", _fake_fetch_all_dicts)

    # (cursor value, expected bound value after _export_cursor_instant)
    aware = datetime(2026, 10, 1, 12, 30, 45, 987654, tzinfo=UTC)
    naive = datetime(2026, 10, 1, 12, 30, 45, 987654)
    iso_str = "2026-10-01T12:30:45.987654"
    cases = [
        (aware, aware),
        (naive, aware),  # naive -> UTC instant
        (iso_str, iso_str),  # string passthrough
    ]
    repo = oracle_mod.OracleMemoryRepository.__new__(oracle_mod.OracleMemoryRepository)
    for cursor_value, expected_bound in cases:
        executed.clear()
        await oracle_mod.OracleMemoryRepository.fetch_memory_export(
            repo,
            SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cursor)),
            effective_owner=None,
            effective_ns=None,
            category=None,
            limit=1000,
            offset=0,
            include_secrets=True,
            record_cursor=(cursor_value, "mem_01999"),
        )
        assert executed, "fetch_memory_export must issue the keyset query"
        sql, params = executed[0]
        assert "(created > :cur_created OR (created = :cur_created_eq AND id > :cur_id))" in sql
        assert isinstance(params["cur_created"], _FakeVar)
        assert isinstance(params["cur_created_eq"], _FakeVar)
        assert params["cur_created"].type_code == bind_type
        assert params["cur_created_eq"].type_code == bind_type
        assert params["cur_created"].value == expected_bound
        assert params["cur_created_eq"].value == expected_bound
        assert params["cur_id"] == "mem_01999"


def test_export_cursor_instant_normalizes_value_domain():
    """The Oracle cursor normalizer preserves the value domain:
    aware datetime binds verbatim, naive datetime becomes a UTC instant,
    ISO string passes through, and an unknown type is returned unchanged so
    it fails loudly at bind time rather than being silently mutated."""
    import mnemos.persistence.oracle as oracle_mod

    aware = datetime(2026, 10, 1, 12, 30, 45, 987654, tzinfo=UTC)
    naive = datetime(2026, 10, 1, 12, 30, 45, 987654)
    iso_str = "2026-10-01T12:30:45.987654"
    assert oracle_mod._export_cursor_instant(aware) == aware
    assert oracle_mod._export_cursor_instant(naive) == aware  # naive -> UTC instant
    assert oracle_mod._export_cursor_instant(iso_str) == iso_str  # string passthrough
    assert oracle_mod._export_cursor_instant(123) == 123  # unknown -> unchanged
    # A non-UTC aware offset is normalized to the same UTC instant.
    shifted = datetime(2026, 10, 1, 14, 30, 45, 987654, tzinfo=timezone(timedelta(hours=2)))
    assert oracle_mod._export_cursor_instant(shifted) == aware


def test_oracle_feed_keyset_already_uses_explicit_timestamp_tz_bind():
    """The federation feed keyset (fetch_exportable_memories) ALREADY binds
    its cursor with an explicit DB_TYPE_TIMESTAMP_TZ variable — the export
    walk is the outlier this fix brings into parity. Assert the pattern is
    present in the source so the two keysets cannot silently drift apart.
    """
    from pathlib import Path

    source = Path(mnemos_oracle_source()).read_text(encoding="utf-8")
    assert "DB_TYPE_TIMESTAMP_TZ" in source
    # The export path must now carry the same pattern (regression guard).
    export_body = source.split("async def fetch_memory_export", 1)[1].split("async def ", 1)[0]
    assert "DB_TYPE_TIMESTAMP_TZ" in export_body, (
        "fetch_memory_export must bind its cursor with an explicit DB_TYPE_TIMESTAMP_TZ variable, like the feed keyset"
    )


def mnemos_oracle_source():
    import mnemos.persistence.oracle as oracle_mod

    return oracle_mod.__file__
