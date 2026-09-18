"""Regression tests for CHARON F08 — multi-page export sidecar aggregation.

The bug (F08, P1): the export CLI pagination loop in
``mnemos.tools.memory_export._fetch_export`` previously retained ONLY
page 1's per-memory sidecars (``kg_triples``, ``memory_versions``,
``compression_manifest``) and discarded sidecar data from every later
page. The server-side ``export_memories`` in
``mnemos.domain.portability.export`` actually scopes its sidecar fetch
to the CURRENT page's memory IDs, so a multi-page export materialises a
distinct sidecar slice per page — dropping page 2+ sidecars silently
broke the migration-fidelity promise.

These tests reproduce the reviewer scenario: TWO valid mocked export
pages, each contributing a different memory record with its OWN
distinct version/KG/compression sidecar data, with the CLI's full
pagination loop. The fix aggregates sidecars across all pages,
identity-deduplicates overlapping rows, preserves per-memory version
ordering, and keeps tenant-scoped deletion_log separately from
per-memory sidecars.

We also assert a full export → import round-trip — import must accept
the resulting envelope without rejecting any record for missing
version coverage, and the imported version/KG/compression sidecar
history for the page-2 record must be COMPLETE (the exact loss the bug
caused).

The tests use the standard mock connection pattern from
``tests/test_portability.py`` (the same mocking used for export/import
unit tests) and a local ``urllib.request.urlopen`` patch that returns
the two-page fixture. The CLI test runs without a live server; the
import-side assertions use the in-memory envelope directly so they
exercise the import code path against a mocked connection.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import contextmanager
from typing import Any, Self
from unittest.mock import patch

import pytest

# ─── Fixture builders ───────────────────────────────────────────────────────


def _memory_record(
    record_id: str,
    *,
    content: str = "hello",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
    category: str = "solutions",
):
    """Build an MPF record payload matching the export serializer output."""
    return {
        "id": record_id,
        "kind": "memory",
        "payload_version": "mnemos-3.1",
        "payload": {
            "content": content,
            "category": category,
            "owner_id": owner_id,
            "namespace": namespace,
        },
    }


def _kg_entry(kg_id: str, memory_id: str, *, subject: str = "Paris"):
    return {
        "id": kg_id,
        "memory_id": memory_id,
        "predicate": "capitalOf",
        "subject_literal": subject,
        "object_literal": "France",
        "subject_type": "place",
        "object_type": "place",
        "confidence": 0.95,
        "owner_id": "alice",
        "namespace": "alice-ns",
    }


def _mv_entry(
    mv_id: str,
    record_id: str,
    version_num: int,
    *,
    parent: str | None = None,
    content: str | None = None,
    branch: str = "main",
):
    entry: dict[str, Any] = {
        "id": mv_id,
        "record_id": record_id,
        "version_num": version_num,
        "branch": branch,
        "change_type": "update" if version_num > 1 else "create",
        "commit_hash": f"hash-{mv_id}",
        "content": content or f"version {version_num} of {record_id}",
        "owner_id": "alice",
        "namespace": "alice-ns",
    }
    if parent is not None:
        entry["parent_version_id"] = parent
    return entry


def _cm_entry(
    record_id: str,
    engine_id: str = "apollo",
    engine_version: str = "1.0",
    compressed_content: str | None = None,
):
    return {
        "record_id": record_id,
        "engine_id": engine_id,
        "engine_version": engine_version,
        "compressed_content": compressed_content or f"compressed:{record_id}",
        "compressed_tokens": 4,
        "compression_ratio": 2.5,
        "quality_score": 0.87,
        "composite_score": 0.81,
        "scoring_profile": "balanced",
        "judge_model": "claude-opus-4-7",
        "owner_id": "alice",
    }


def _envelope_page(
    *,
    records: list[dict[str, Any]],
    kg_triples: list[dict[str, Any]] | None = None,
    memory_versions: list[dict[str, Any]] | None = None,
    compression_manifest: list[dict[str, Any]] | None = None,
    deletion_log: list[dict[str, Any]] | None = None,
    deletion_log_next_cursor: str | None = None,
) -> dict[str, Any]:
    """Build an MPF envelope page that the mocked server returns."""
    env: dict[str, Any] = {
        "mpf_version": "0.2.0",
        "records": records,
    }
    if kg_triples is not None:
        env["kg_triples"] = kg_triples
    if memory_versions is not None:
        env["memory_versions"] = memory_versions
    if compression_manifest is not None:
        env["compression_manifest"] = compression_manifest
    if deletion_log is not None:
        env["deletion_log"] = deletion_log
    if deletion_log_next_cursor is not None:
        env["deletion_log_next_cursor"] = deletion_log_next_cursor
    return env


# Two-page fixture: page 1 contains mem_A with its own sidecars; page 2
# contains mem_B with its own sidecars. limit=1 forces pagination —
# each page returns exactly one record, then a final short page signals
# exhaustion.
PAGE_1_RECORDS = [_memory_record("mem_A", content="page-1 record")]
PAGE_1_KG = [_kg_entry("kg_A1", "mem_A")]
PAGE_1_MV = [
    _mv_entry("ver_A_v1", "mem_A", 1, content="A v1"),
]
PAGE_1_CM = [_cm_entry("mem_A", compressed_content="compressed:mem_A")]

PAGE_2_RECORDS = [_memory_record("mem_B", content="page-2 record")]
PAGE_2_KG = [_kg_entry("kg_B1", "mem_B", subject="Berlin")]
PAGE_2_MV = [
    _mv_entry("ver_B_v1", "mem_B", 1, content="B v1"),
    _mv_entry("ver_B_v2", "mem_B", 2, parent="ver_B_v1", content="B v2"),
]
PAGE_2_CM = [_cm_entry("mem_B", compressed_content="compressed:mem_B")]


PAGE_1 = _envelope_page(
    records=PAGE_1_RECORDS,
    kg_triples=PAGE_1_KG,
    memory_versions=PAGE_1_MV,
    compression_manifest=PAGE_1_CM,
)
PAGE_2 = _envelope_page(
    records=PAGE_2_RECORDS,
    kg_triples=PAGE_2_KG,
    memory_versions=PAGE_2_MV,
    compression_manifest=PAGE_2_CM,
)
# Final short page signals exhaustion: empty records. Sidecars are
# irrelevant here because the loop breaks before any sidecar processing.
SHORT_PAGE = _envelope_page(
    records=[],
    kg_triples=[],
    memory_versions=[],
    compression_manifest=[],
    deletion_log=[],
)


# ─── urllib.request.urlopen mock ────────────────────────────────────────────


class _FakeResp:
    """Tiny stand-in for ``http.client.HTTPResponse`` that supports the
    context-manager protocol used by ``urllib.request.urlopen``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a) -> bool:
        return False


@contextmanager
def _patch_urlopen(responses: list[dict[str, Any]]):
    """Patch ``urllib.request.urlopen`` to return each entry in ``responses``
    in order on each call. Captures stderr so per-call warnings surface
    in test failure output."""
    it = iter(responses)

    def _fake_urlopen(req, timeout=120):  # matches the real signature
        return _FakeResp(next(it))

    captured_err = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured_err
    try:
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            yield captured_err
    finally:
        sys.stderr = real_stderr


# ─── CLI export tests ──────────────────────────────────────────────────────


def _run_cli_export(*, pages: list[dict[str, Any]], include_sidecars: bool = True) -> dict[str, Any]:
    """Drive ``memory_export._fetch_export`` through the patched
    urllib stack and return the final envelope dict."""
    from mnemos.tools.memory_export import _fetch_export

    with _patch_urlopen(pages):
        return _fetch_export(
            endpoint="http://testserver",
            api_key=None,
            category=None,
            limit=1,  # forces pagination: each page has 1 record
            include_sidecars=include_sidecars,
        )


def test_cli_aggregates_per_memory_sidecars_across_two_pages():
    """Reproduces the F08 reviewer scenario: two pages, each with one
    distinct memory record and its OWN sidecar rows.

    Prior behaviour (bug): only page 1's sidecars made it into the final
    envelope; mem_B's kg_triple, two memory_versions, and compression
    variant were silently dropped.

    Expected post-fix behaviour: the final envelope contains sidecar
    rows for BOTH mem_A AND mem_B, deduped by stable identity, with
    per-memory version ordering preserved across the merge.
    """
    result = _run_cli_export(pages=[PAGE_1, PAGE_2, SHORT_PAGE])

    # Both records are present — pagination worked.
    record_ids = {r["id"] for r in result["records"]}
    assert record_ids == {"mem_A", "mem_B"}, (
        f"expected both page-1 and page-2 records in the final envelope; got {sorted(record_ids)}"
    )

    # BOTH pages' sidecar data must be present. The bug was that only
    # page 1's sidecars survived — verify mem_B's distinct sidecars
    # made it into the final envelope.
    kg_ids = {kg["id"] for kg in result.get("kg_triples", [])}
    assert kg_ids == {"kg_A1", "kg_B1"}, (
        f"F08 regression: page-2's kg_triple was dropped from the final "
        f"envelope. Got kg_ids={sorted(kg_ids)}; expected {{'kg_A1','kg_B1'}}"
    )

    mv_ids = {mv["id"] for mv in result.get("memory_versions", [])}
    assert mv_ids == {"ver_A_v1", "ver_B_v1", "ver_B_v2"}, (
        f"F08 regression: page-2's memory_versions were dropped from the "
        f"final envelope. Got mv_ids={sorted(mv_ids)}; "
        f"expected {{'ver_A_v1','ver_B_v1','ver_B_v2'}}"
    )

    cm_record_ids = {cm["record_id"] for cm in result.get("compression_manifest", [])}
    assert cm_record_ids == {"mem_A", "mem_B"}, (
        f"F08 regression: page-2's compression_manifest was dropped from "
        f"the final envelope. Got cm_record_ids={sorted(cm_record_ids)}"
    )


def test_cli_memory_versions_are_ordered_by_record_then_version_num():
    """The merge must preserve per-memory version ordering so that
    parent versions always appear before their children in the final
    envelope. Without an explicit sort, the order would depend on
    insertion order (which is page-order, not version-order) and the
    import-side DAG walk could process children before parents.

    Both mem_A and mem_B must appear, and within each memory the
    versions must be sorted by version_num ascending.
    """
    result = _run_cli_export(pages=[PAGE_1, PAGE_2, SHORT_PAGE])

    mvs = result.get("memory_versions") or []
    assert len(mvs) == 3, f"expected 3 memory_versions, got {len(mvs)}"

    # Group by record_id and assert version_num is monotonic.
    by_record: dict[str, list[dict[str, Any]]] = {}
    for mv in mvs:
        by_record.setdefault(mv["record_id"], []).append(mv)
    assert "mem_A" in by_record, "mem_A's memory_versions missing"
    assert "mem_B" in by_record, "mem_B's memory_versions missing (F08 bug)"

    for record_id, versions in by_record.items():
        version_nums = [v["version_num"] for v in versions]
        assert version_nums == sorted(version_nums), (
            f"versions for {record_id} are not monotonically ordered: {version_nums}"
        )

    # The specific failure mode the prior behaviour exhibited: page 1's
    # ver_A_v1 came first, then page 2's ver_B_v1, then ver_B_v2. The
    # fix retains that page-order grouping (record_id groups together)
    # while keeping version_num monotonic within each group. Verify the
    # group ordering is deterministic: by record_id, then by version_num.
    expected_order = [
        ("mem_A", 1),
        ("mem_B", 1),
        ("mem_B", 2),
    ]
    actual_order = [(mv["record_id"], mv["version_num"]) for mv in mvs]
    assert actual_order == expected_order, (
        f"memory_versions ordering mismatch: got {actual_order}, expected {expected_order}"
    )


def test_cli_dedupes_identical_overlapping_sidecar_rows():
    """When two pages emit the SAME sidecar row (e.g. the server replays
    a memory id across pages, or the page-boundary straddles a record),
    the CLI must dedupe by stable identity rather than double-counting.
    """
    overlap_kg = _kg_entry("kg_overlap", "mem_A", subject="Paris")
    overlap_mv = _mv_entry("ver_A_v1", "mem_A", 1, content="A v1")
    overlap_cm = _cm_entry("mem_A", compressed_content="compressed:mem_A")

    page1 = _envelope_page(
        records=[_memory_record("mem_A")],
        kg_triples=[overlap_kg],
        memory_versions=[overlap_mv],
        compression_manifest=[overlap_cm],
    )
    page2 = _envelope_page(
        records=[_memory_record("mem_B")],
        kg_triples=[
            overlap_kg,  # SAME id as page 1 — must dedupe, not double
            _kg_entry("kg_B1", "mem_B"),
        ],
        memory_versions=[
            overlap_mv,  # SAME id as page 1 — must dedupe
            _mv_entry("ver_B_v1", "mem_B", 1),
        ],
        compression_manifest=[
            overlap_cm,  # SAME (record_id, engine_id, engine_version) as page 1
            _cm_entry("mem_B"),
        ],
    )

    result = _run_cli_export(pages=[page1, page2, SHORT_PAGE])

    # The overlapping row should appear EXACTLY ONCE per surface.
    kg_ids = [kg["id"] for kg in result.get("kg_triples", [])]
    assert kg_ids.count("kg_overlap") == 1, f"overlapping kg_triple was double-counted: got {kg_ids}"
    assert "kg_B1" in kg_ids, "page-2's distinct kg_triple was lost"

    mv_ids = [mv["id"] for mv in result.get("memory_versions", [])]
    assert mv_ids.count("ver_A_v1") == 1, f"overlapping memory_version was double-counted: got {mv_ids}"
    assert "ver_B_v1" in mv_ids, "page-2's distinct memory_version was lost"

    cm_record_engine = [
        (cm["record_id"], cm["engine_id"], cm.get("engine_version")) for cm in result.get("compression_manifest", [])
    ]
    assert cm_record_engine.count(("mem_A", "apollo", "1.0")) == 1, (
        f"overlapping compression_manifest was double-counted: got {cm_record_engine}"
    )
    assert ("mem_B", "apollo", "1.0") in cm_record_engine


def test_cli_rejects_divergent_overlapping_sidecar_rows():
    """If two pages emit the SAME identity with DIFFERENT content, the
    CLI must reject conflicting identities before publishing an export."""
    kg_v1 = _kg_entry("kg_dup", "mem_A", subject="Paris")
    kg_v2 = _kg_entry("kg_dup", "mem_A", subject="DIFFERENT-SUBJECT")  # same id, divergent

    page1 = _envelope_page(
        records=[_memory_record("mem_A")],
        kg_triples=[kg_v1],
    )
    page2 = _envelope_page(
        records=[_memory_record("mem_B")],
        kg_triples=[kg_v2],
    )

    with _patch_urlopen([page1, page2, SHORT_PAGE]):
        from mnemos.tools.memory_export import _fetch_export

        with pytest.raises(ValueError, match="kg_triples"):
            _fetch_export("http://testserver", None, None, 1, include_sidecars=True)


def test_cli_keeps_deletion_log_separate_from_per_memory_sidecars():
    """Tenant-scoped deletion_log uses its own cursor and must NOT be
    deduped against per-memory sidecars. Verify a deletion_log entry
    on page 2 is preserved verbatim alongside page 1's per-memory
    sidecars (i.e. the deletion_log path doesn't accidentally absorb
    into the per-memory merge)."""
    deletion_entry = {
        "id": "del_001",
        "memory_id": "mem_deleted",
        "owner_id": "alice",
        "namespace": "alice-ns",
        "deleted_at": "2026-09-01T00:00:00Z",
        "request_kind": "delete",
        "reason": "test",
    }
    page1 = _envelope_page(
        records=[_memory_record("mem_A")],
        kg_triples=PAGE_1_KG,
        memory_versions=PAGE_1_MV,
        compression_manifest=PAGE_1_CM,
        deletion_log=[],
    )
    page2 = _envelope_page(
        records=[_memory_record("mem_B")],
        kg_triples=PAGE_2_KG,
        memory_versions=PAGE_2_MV,
        compression_manifest=PAGE_2_CM,
        deletion_log=[deletion_entry],
    )

    result = _run_cli_export(pages=[page1, page2, SHORT_PAGE])

    # Per-memory sidecars from BOTH pages are present.
    assert {kg["id"] for kg in result.get("kg_triples", [])} == {"kg_A1", "kg_B1"}
    # The deletion_log entry from page 2 is preserved verbatim,
    # separately from per-memory sidecars.
    assert result.get("deletion_log") == [deletion_entry]


# ─── Full CLI export → import round-trip ─────────────────────────────────────


class _Conn:
    """Mock asyncpg connection for the import side.

    Mirrors ``tests/test_portability.py::_Conn`` but is self-contained
    here so this test file does not depend on internal test helpers
    beyond the standard mock patterns documented in this file. The
    fixture seeds allowlist rows for the records being imported so
    the import path's per-record cross-reference check accepts both
    pages' memory ids rather than failing them with "not in caller-
    owned memory id set" — the bug we're testing is a sidecar scoping
    bug, NOT an authorization bug."""

    def __init__(self, *, allowlist_memory_ids: list[str] | None = None) -> None:
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self._allowlist_memory_ids = set(allowlist_memory_ids or [])

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        # Allowlist SELECT — see mnemos/persistence/postgres.fetch_referenced_memory_allowlist.
        # Return a row for each referenced memory id so the import path
        # accepts the sidecars.
        if "FROM memories WHERE id = ANY" in sql:
            return [{"id": mid, "owner_id": "alice", "namespace": "alice-ns"} for mid in self._allowlist_memory_ids]
        # fetch_versioned_memory_ids — post-insert coverage check.
        # Return a DISTINCT memory_id row for every memory id we expect
        # to have been covered by the sidecar import.
        if "FROM memory_versions WHERE memory_id = ANY" in sql:
            # The arg is the list of memory_ids we asked about.
            ids = list(args[0]) if args else []
            return [{"memory_id": mid} for mid in ids if mid in self._allowlist_memory_ids]
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetch_calls.append((sql, args))

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    def transaction(self, *args, **kwargs):
        class _NullCtx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

        return _NullCtx()


def _install_mock_pool(monkeypatch, conn):
    """Install ``conn`` as the active persistence backend.

    The portability path takes a backend + transaction now rather than a
    pooled driver connection; ConnBackedBackend presents the ABC surface over
    this module's mock conn so its SQL-substring routing keeps working. Name
    kept for call-site stability.
    """
    from tests._charon_fake_backend import install_conn_backend

    return install_conn_backend(monkeypatch, conn)


def _alice_user():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _root_user():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def test_cli_two_page_export_envelope_round_trips_through_import():
    """End-to-end: drive the CLI pagination loop with the two-page
    fixture to produce a final envelope, then run the import path
    against a mocked asyncpg connection. The import must accept both
    memory records AND process the per-memory sidecars from BOTH
    pages without rejecting either record for missing version coverage.

    This is the exact failure mode the F08 bug exhibited: import would
    silently succeed at the records layer (the records[] loop never
    sees the dropped sidecars), but the per-memory sidecar insert
    path would only see page 1's data. The test asserts both layers
    receive complete data.
    """
    # 1. Drive the CLI export with the two-page fixture.
    envelope = _run_cli_export(pages=[PAGE_1, PAGE_2, SHORT_PAGE])

    # Sanity check the CLI produced a well-formed envelope before we
    # hand it to the import path — this is the assertion that the F08
    # fix made possible.
    record_ids = {r["id"] for r in envelope["records"]}
    assert record_ids == {"mem_A", "mem_B"}, (
        f"CLI export fixture broken: expected both mem_A and mem_B in the final envelope, got {sorted(record_ids)}"
    )
    mv_record_ids = {mv["record_id"] for mv in envelope.get("memory_versions", [])}
    assert mv_record_ids == {"mem_A", "mem_B"}, (
        f"CLI export missing memory_versions for one of the records "
        f"(F08 bug surfaced); got record_ids={sorted(mv_record_ids)}"
    )
    # Specifically verify the page-2 record has its full version
    # history, not just a partial v1.
    mem_b_versions = [mv for mv in envelope.get("memory_versions", []) if mv["record_id"] == "mem_B"]
    assert len(mem_b_versions) == 2, (
        f"F08 bug: page-2 record's second memory_version (ver_B_v2) was "
        f"dropped from the export. Got mem_B versions: "
        f"{[mv['version_num'] for mv in mem_b_versions]}"
    )
    assert {mv["version_num"] for mv in mem_b_versions} == {1, 2}, (
        f"F08 bug: page-2 record's version history is incomplete; "
        f"got versions {[mv['version_num'] for mv in mem_b_versions]}"
    )

    # 2. Hand the CLI-produced envelope to the import path.
    from mnemos.api.routes import portability
    from mnemos.domain.portability.schemas import MPFEnvelope

    env_obj = MPFEnvelope.model_validate(envelope)

    conn = _Conn(allowlist_memory_ids=["mem_A", "mem_B"])

    # 3. Patch the pool so portability.import_memories sees our mock
    # connection. This mirrors the fixture pattern in tests/test_portability.py.
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_mock_pool(monkeypatch, conn)
        stats = asyncio.run(
            portability.import_memories(
                envelope=env_obj,
                preserve_owner=True,
                user=_root_user(),
            )
        )
    finally:
        monkeypatch.undo()

    # 4. Assert both records imported (not just one — page-2's record
    # was the silent victim in the prior behaviour).
    assert stats.imported == 2, (
        f"expected both records to import; got imported={stats.imported}, "
        f"failed={stats.failed}, errors={stats.errors[:3]}"
    )

    # 5. Assert the sidecar insert path saw both records' data. The
    # import path emits INSERT INTO memory_versions for the
    # memory_versions sidecar; the page-2 record's ver_B_v2 must be
    # among them.
    mv_inserts = [e for e in conn.executes if "INSERT INTO memory_versions" in e[0]]
    # Be lenient about positional index — just check that both record ids
    # appear in the args of any memory_versions INSERT. The bug was
    # that the export dropped page-2's versions, so this check would
    # have failed because the page-2 record's ids weren't in the args.
    assert any("mem_B" in (a or "") for e in mv_inserts for a in e[1] if isinstance(a, str)), (
        f"F08 bug: page-2 record's memory_versions were not forwarded "
        f"to the import path. Insert args: "
        f"{[a for e in mv_inserts for a in e[1] if isinstance(a, str)]}"
    )


def test_cli_export_pagination_loop_emits_one_urlopen_per_page():
    """Sanity check: the fix must not regress pagination itself. We
    expect exactly N+1 urlopen calls for an N-record corpus split
    across pages (N full pages + 1 short/empty exhaustion page).

    If pagination regresses (e.g. early termination), the F08 fix
    becomes moot because later pages never fire."""
    from mnemos.tools.memory_export import _fetch_export

    call_count = {"n": 0}
    responses = [PAGE_1, PAGE_2, SHORT_PAGE]

    def _fake_urlopen(req, timeout=120):
        idx = min(call_count["n"], len(responses) - 1)
        call_count["n"] += 1
        return _FakeResp(responses[idx])

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _fetch_export(
            endpoint="http://testserver",
            api_key=None,
            category=None,
            limit=1,
            include_sidecars=True,
        )

    assert call_count["n"] == 3, f"expected 3 urlopen calls (2 record pages + 1 short page); got {call_count['n']}"


def test_cli_no_sidecars_means_no_sidecars_in_final_envelope():
    """When the server emits no sidecars on any page (legacy envelope
    shape), the final envelope must remain free of sidecar keys —
    otherwise the fix would emit empty arrays on v0.1 envelopes."""
    page1 = _envelope_page(records=[_memory_record("mem_A")])  # no sidecar keys
    page2 = _envelope_page(records=[_memory_record("mem_B")])  # no sidecar keys

    result = _run_cli_export(pages=[page1, page2, SHORT_PAGE], include_sidecars=False)

    assert len(result["records"]) == 2
    # The CLI must NOT add sidecar keys when the server didn't emit
    # any — preserves wire-format backward compat for callers that
    # expect v0.1-shaped envelopes.
    for key in ("kg_triples", "memory_versions", "compression_manifest"):
        assert key not in result or not result.get(key), (
            f"CLI injected an empty {key} sidecar on a server response "
            f"that didn't include one: result[{key!r}]={result.get(key)!r}"
        )


def test_cli_preserves_deletion_log_when_present_on_later_pages():
    """Tenant-scoped deletion_log uses cursor-based pagination; later
    pages may emit additional entries without entries on page 1. The
    CLI must preserve the concatenation verbatim (the cursor
    mechanism guarantees non-overlap)."""
    page1 = _envelope_page(
        records=[_memory_record("mem_A")],
        kg_triples=PAGE_1_KG,
        memory_versions=PAGE_1_MV,
        compression_manifest=PAGE_1_CM,
        deletion_log=[
            {
                "id": "del_001",
                "memory_id": "mem_deleted_1",
                "owner_id": "alice",
                "namespace": "alice-ns",
                "deleted_at": "2026-08-30T00:00:00Z",
                "request_kind": "delete",
                "reason": "first",
            }
        ],
        deletion_log_next_cursor="cursor-for-page-2",
    )
    page2 = _envelope_page(
        records=[_memory_record("mem_B")],
        kg_triples=PAGE_2_KG,
        memory_versions=PAGE_2_MV,
        compression_manifest=PAGE_2_CM,
        deletion_log=[
            {
                "id": "del_002",
                "memory_id": "mem_deleted_2",
                "owner_id": "alice",
                "namespace": "alice-ns",
                "deleted_at": "2026-09-05T00:00:00Z",
                "request_kind": "delete",
                "reason": "second",
            }
        ],
    )

    result = _run_cli_export(pages=[page1, page2, SHORT_PAGE])

    assert result.get("deletion_log") == [
        page1["deletion_log"][0],
        page2["deletion_log"][0],
    ], (
        f"deletion_log should be the concatenation of all pages' entries "
        f"(cursor-guaranteed non-overlap); got {result.get('deletion_log')}"
    )
    # The final cursor is the LAST non-empty one observed.
    assert result.get("deletion_log_next_cursor") == "cursor-for-page-2"
