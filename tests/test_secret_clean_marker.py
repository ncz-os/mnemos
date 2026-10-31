"""M6 (code-health decisions 2026-10-31): skip the read-time recompute when a row
is stored as CLEAN under the current classifier, and keep large redaction
batches off the event loop.

Measured on the base: ``classify`` costs about 0.47 ms per KB of text
(0.64 ms at 1.3 KB, 6.37 ms at 13.4 KB) and ``redact_field_with_stored``
recomputes it for every read that carries no stored spans -- which is every
CLEAN row. A 200-row deep search over 2 KB rows is roughly 0.25 s of
synchronous CPU inside the asyncio event loop.

Contract enforced here:

1. Ingest stamps ``metadata["secret_field_classifiers"] = {field: version}``
   for every field the classifier examined and found CLEAN, and *only* for
   those fields.
2. ``redact_field_with_stored`` skips ``classify`` when that marker matches
   the current ``SECRET_CLASSIFIER_VERSION``; it still recomputes when the
   marker is missing, malformed, or written by an older classifier, so
   bumping the version resumes the recompute.
3. The marker is server-only: a caller-supplied ``secret_field_classifiers``
   (metadata create / PATCH) is stripped and can never reach the store.
4. ``rows_to_memories`` moves a large redaction batch to a worker thread and
   returns exactly what ``row_to_memory`` would.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Response

import mnemos.api.routes.memories as memories
import mnemos.core.lifecycle as lc
from mnemos.api.dependencies import UserContext
from mnemos.core import secret_detection as sd
from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.domain.models import (
    MemoryCreateRequest,
    MemoryUpdateRequest,
    row_to_memory,
    rows_to_memories,
)

# Synthetic, obviously-fake secret-shaped value (a 64-char hex digest — the
# REDACT tier, so the row is never vaulted). Never a real credential.
SECRET = "a3f9b2c1d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d"
CLEAN_TEXT = "deployment finished; see the status page at https://status.example.com/42"
SECRET_TEXT = f"deploy note: the artifact fingerprint is {SECRET} for build 17"


def _clean_marker(metadata):
    """Return the decoded ``secret_field_classifiers`` marker (or None)."""
    meta = metadata
    if isinstance(meta, str):
        meta = json.loads(meta)
    if not isinstance(meta, dict):
        return None
    return meta.get(sd.SECRET_CLEAN_MARKER_KEY)


# ── 1. ingest stamps the marker ────────────────────────────────────────────


def test_clean_ingest_stamps_the_current_classifier_version():
    classified = classify_persisted_text_fields(
        content=CLEAN_TEXT,
        verbatim_content=CLEAN_TEXT,
        namespace="team",
        classified_at="ingest",
        memory_id="mem_clean_marker",
    )
    assert classified.namespace == "team"
    assert _clean_marker(classified.metadata) == {
        "content": sd.SECRET_CLASSIFIER_VERSION,
        "verbatim_content": sd.SECRET_CLASSIFIER_VERSION,
    }
    # No spans, no reasons: the row is clean.
    assert "secret_redact_fields" not in classified.metadata
    assert "secret_reasons" not in classified.metadata


def test_marker_lists_only_fields_the_classifier_examined():
    classified = classify_persisted_text_fields(
        content=CLEAN_TEXT,
        # compressed/verbatim omitted: NOT a CLEAN verdict, so not listed.
        namespace="team",
        classified_at="ingest",
    )
    assert _clean_marker(classified.metadata) == {"content": sd.SECRET_CLASSIFIER_VERSION}


def test_secret_field_is_never_marked_clean():
    classified = classify_persisted_text_fields(
        content=SECRET_TEXT,
        namespace="team",
        classified_at="ingest",
    )
    marker = _clean_marker(classified.metadata) or {}
    assert "content" not in marker
    assert classified.metadata["secret_redact_fields"]["content"]


# ── 2. read path: skip the recompute only while the version matches ────────


@pytest.fixture
def classify_calls(monkeypatch):
    """Count ``classify`` invocations made through the module under test."""
    calls: list[str] = []
    real = sd.classify

    def counting(content):
        calls.append(content or "")
        return real(content)

    monkeypatch.setattr(sd, "classify", counting)
    return calls


def test_current_marker_skips_the_recompute(classify_calls):
    meta = {sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}}
    assert sd.redact_field_with_stored(CLEAN_TEXT, meta, "content") == CLEAN_TEXT
    assert classify_calls == []


def test_missing_marker_still_recomputes(classify_calls):
    assert sd.redact_field_with_stored(CLEAN_TEXT, {}, "content") == CLEAN_TEXT
    assert classify_calls == [CLEAN_TEXT]


def test_bumping_the_classifier_version_resumes_the_recompute(classify_calls, monkeypatch):
    """The regression test for the marker: stale versions must re-classify.

    A row written clean by classifier v1 and read by classifier v2 must go
    back through the classifier — otherwise a detector added in v2 would
    never see the row, and a text that v1 called CLEAN would stay unmasked.
    """
    meta = {sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}}
    # Sanity: under the recorded version this row is skipped.
    assert sd.redact_field_with_stored(SECRET_TEXT, meta, "content") == SECRET_TEXT
    assert classify_calls == []

    # Now pretend a NEW classifier shipped: version bumped, and the new
    # detector recognises the row's text as a credential.
    def v2_classify(content):
        finding = sd.SecretFinding()
        finding.cls = sd.SecretClass.REDACT
        finding.reasons.append("v2_detector")
        start = content.index(SECRET) if SECRET in content else 0
        finding.spans.append((start, start + len(SECRET)))
        return finding

    monkeypatch.setattr(sd, "classify", v2_classify)
    monkeypatch.setattr(sd, "SECRET_CLASSIFIER_VERSION", sd.SECRET_CLASSIFIER_VERSION + 1)

    masked = sd.redact_field_with_stored(SECRET_TEXT, meta, "content")
    assert SECRET not in masked
    assert "[REDACTED]" in masked


def test_bump_also_recomputes_for_genuinely_clean_rows(classify_calls, monkeypatch):
    meta = {sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}}
    monkeypatch.setattr(sd, "SECRET_CLASSIFIER_VERSION", sd.SECRET_CLASSIFIER_VERSION + 1)
    assert sd.redact_field_with_stored(CLEAN_TEXT, meta, "content") == CLEAN_TEXT
    assert classify_calls == [CLEAN_TEXT]


def test_stored_spans_win_over_a_clean_marker(classify_calls):
    meta = {
        sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION},
        "secret_redact_fields": {"content": [[15, 37]]},
    }
    content = "the magic word is flibbertigibbet and it is harmless"
    masked = sd.redact_field_with_stored(content, meta, "content")
    assert "flibbertigibbet" not in masked
    assert classify_calls == []


@pytest.mark.parametrize(
    "marker",
    [
        {"content": sd.SECRET_CLASSIFIER_VERSION + 1},
        {"content": True},  # bool is an int subclass — must not read as v1
        {"content": "1"},
        {"content": None},
        {"other_field": sd.SECRET_CLASSIFIER_VERSION},
        [sd.SECRET_CLASSIFIER_VERSION],
        sd.SECRET_CLASSIFIER_VERSION,
    ],
)
def test_malformed_or_foreign_markers_fail_closed(classify_calls, marker):
    meta = {sd.SECRET_CLEAN_MARKER_KEY: marker}
    sd.redact_field_with_stored(CLEAN_TEXT, meta, "content")
    assert classify_calls == [CLEAN_TEXT], "a marker that is not this classifier's must not skip the recompute"


def test_marker_tolerates_json_string_metadata(classify_calls):
    meta = json.dumps({sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}})
    assert sd.redact_field_with_stored(CLEAN_TEXT, meta, "content") == CLEAN_TEXT
    assert classify_calls == []


# ── 3. server-only: a caller cannot set the marker ─────────────────────────


def test_caller_supplied_marker_is_stripped_at_ingest():
    forged = {
        sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION},
        "team": "infra",
    }
    classified = classify_persisted_text_fields(
        content=SECRET_TEXT,
        metadata=forged,
        namespace="team",
        classified_at="ingest",
    )
    marker = _clean_marker(classified.metadata) or {}
    # The forged marker did not survive: the SEcret text is not marked clean.
    assert "content" not in marker
    assert classified.metadata["team"] == "infra"


def test_caller_supplied_marker_does_not_survive_a_clean_ingest():
    """Even a "plausible" forgery is rebuilt from the classifier's own verdict.

    The caller's marker here claims the *clean* text is clean under the
    current version. Stripping is observable because the server would write
    exactly that — so instead claim it for a field the server never examined
    (``compressed_content``) and assert the server's map does not include it.
    """
    forged = {sd.SECRET_CLEAN_MARKER_KEY: {"compressed_content": sd.SECRET_CLASSIFIER_VERSION}}
    classified = classify_persisted_text_fields(
        content=CLEAN_TEXT,
        metadata=forged,
        namespace="team",
        classified_at="ingest",
    )
    assert _clean_marker(classified.metadata) == {"content": sd.SECRET_CLASSIFIER_VERSION}


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=["g-team"],
        role="user",
        namespace="team",
        authenticated=True,
    )


async def _noop_async(*_args, **_kwargs):
    return None


async def _no_embedding(_text):
    return None


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    from mnemos.persistence.sqlite import SqliteBackend

    be = SqliteBackend(tmp_path / "clean_marker.sqlite3", SimpleNamespace())
    await be.open()
    monkeypatch.setattr(lc, "_persistence_backend", be, raising=False)
    monkeypatch.setattr(memories, "_backend_or_503", lambda: be)
    monkeypatch.setattr(memories, "_get_embedding", _no_embedding)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_async)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_async)
    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda _ids: None)
    try:
        yield be
    finally:
        await be.close()


def _root() -> UserContext:
    """Root caller: only used to inspect the STORE, never for a read path."""
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


async def _stored_metadata(be, memory_id: str) -> dict:
    from mnemos.persistence.visibility import VisibilityFilter

    async with be.transactional() as tx:
        row = await be.memories.get_memory(
            tx,
            memory_id,
            visibility=VisibilityFilter.for_read(_root(), namespace=None, include_vault=True),
            include_archived=True,
        )
    assert row is not None
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return dict(meta or {})


@pytest.mark.asyncio
async def test_caller_cannot_forge_the_marker_through_metadata_writes(backend):
    """A plant through create and PATCH must never disable read-time masking.

    The forgery claims the secret-bearing ``content`` is CLEAN (and, in the
    second case, that a field the server never examined is). After the M1
    strip the server's own verdict is the only marker that reaches the store.
    """
    forged = {sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}}
    created = await memories.create_memory(
        MemoryCreateRequest(content=SECRET_TEXT, namespace="team", metadata=dict(forged)),
        Response(),
        user=_alice(),
    )
    meta = await _stored_metadata(backend, created.id)
    assert "content" not in (_clean_marker(meta) or {}), "create persisted a caller-supplied marker"
    assert meta["secret_redact_fields"]["content"], "the real spans must still be recorded"

    # A metadata-only PATCH re-classifies and re-stamps from the store.
    fresh = await memories.create_memory(
        MemoryCreateRequest(content=CLEAN_TEXT, namespace="team"),
        Response(),
        user=_alice(),
    )
    await memories.update_memory(
        fresh.id,
        MemoryUpdateRequest(metadata={"secret_field_classifiers": {"compressed_content": 1}}),
        user=_alice(),
    )
    meta = await _stored_metadata(backend, fresh.id)
    # Re-stamped from the server's own verdict: the forged
    # ``compressed_content`` entry is gone, and the fields the server
    # actually examined are the ones listed.
    marker = meta.get(sd.SECRET_CLEAN_MARKER_KEY) or {}
    assert "compressed_content" not in marker
    assert marker.get("content") == sd.SECRET_CLASSIFIER_VERSION


# ── 4. large batches are offloaded to a thread ─────────────────────────────


def _row(content: str, **extra) -> dict:
    row = {
        "id": "m1",
        "content": content,
        "category": "note",
        "subcategory": None,
        "tags": [],
        "created": "2026-10-31T00:00:00Z",
        "updated": "2026-10-31T00:00:00Z",
        "metadata": None,
        "quality_rating": None,
        "verbatim_content": None,
        "owner_id": "alice",
        "group_id": "g-team",
        "namespace": "team",
        "recall_count": 0,
        "source_provider": None,
        "source_model": None,
        "source_agent": None,
    }
    row.update(extra)
    return row


@pytest.mark.asyncio
async def test_large_batch_redacts_on_a_worker_thread(monkeypatch):
    rows = [_row(SECRET_TEXT, id=f"m{i}") for i in range(40)]
    threads: list[str] = []

    import mnemos.domain.models as models

    real_redact = models._redact_row_fields_batch

    def recording(pieces):
        import threading

        threads.append(threading.current_thread().name)
        return real_redact(pieces)

    monkeypatch.setattr(models, "_redact_row_fields_batch", recording)
    items = await rows_to_memories(rows, redact_secrets=True, thread_threshold_bytes=1024)

    assert threads and threads[0] != "MainThread"
    assert len(items) == len(rows)
    for item in items:
        assert SECRET not in (item.content or "")
        assert "[REDACTED]" in (item.content or "")


@pytest.mark.asyncio
async def test_small_sets_stay_in_place_and_match_row_to_memory():
    rows = [_row(SECRET_TEXT), _row(CLEAN_TEXT, id="m2")]
    items = await rows_to_memories(rows, redact_secrets=True)
    expected = [row_to_memory(r, redact_secrets=True) for r in rows]
    assert [i.model_dump() for i in items] == [e.model_dump() for e in expected]


@pytest.mark.asyncio
async def test_marked_clean_rows_are_cheap_on_the_read_path(monkeypatch):
    """The point of the marker: a clean row costs no classifier work."""
    meta = {
        sd.SECRET_CLEAN_MARKER_KEY: {
            "content": sd.SECRET_CLASSIFIER_VERSION,
            "verbatim_content": sd.SECRET_CLASSIFIER_VERSION,
        }
    }
    rows = [_row(CLEAN_TEXT, metadata=meta) for _ in range(5)]
    calls: list[str] = []
    real = sd.classify
    monkeypatch.setattr(sd, "classify", lambda text: calls.append(text) or real(text))

    items = await rows_to_memories(rows, redact_secrets=True)
    assert [i.content for i in items] == [CLEAN_TEXT] * 5
    assert calls == []


# ── 5. latency microbenchmark ──────────────────────────────────────────────

_BENCH_BASE = (
    "The quarterly deployment retrospective notes that the release train moved to two-week cycles. "
    "Owners agreed to keep the rollback drill on the first Tuesday, and the status page link stays pinned. "
)


def _text_at(size: int) -> str:
    return _BENCH_BASE * max(1, size // len(_BENCH_BASE))


def test_latency_microbenchmark_marker_skips_the_classifier():
    """Clean-row read cost with and without the stored CLEAN marker.

    Before M6 (no marker anywhere): ``redact_field_with_stored`` recomputed
    the classifier for every row -- about 0.64 ms at 1.3 KB and 6.37 ms at
    13.4 KB on the reference machine (roughly 0.47 ms per KB). Those figures
    are the fix's motivation; the assertion below uses ratios and absolute
    bounds generous enough for a loaded CI box, and every number is printed
    so a regression shows up in the log.
    """
    sizes = (1_300, 13_400)
    report: list[str] = []
    for size in sizes:
        text = _text_at(size)
        unmarked = {}
        marked = {sd.SECRET_CLEAN_MARKER_KEY: {"content": sd.SECRET_CLASSIFIER_VERSION}}

        def measure(meta) -> float:
            rounds = 5
            best = float("inf")
            for _ in range(rounds):
                started = time.perf_counter()
                for _ in range(3):
                    sd.redact_field_with_stored(text, meta, "content")
                best = min(best, (time.perf_counter() - started) / 3)
            return best * 1000.0

        classify_ms = None
        started = time.perf_counter()
        for _ in range(3):
            sd.classify(text)
        classify_ms = (time.perf_counter() - started) / 3 * 1000.0
        before_ms = measure(unmarked)
        after_ms = measure(marked)
        report.append(f"{size} B: classify={classify_ms:.3f} ms recompute={before_ms:.3f} ms marker={after_ms:.4f} ms")

        # The unmarked path still recomputes: it must cost at least as much
        # as one classify() call (the recompute is classify + redact).
        # ``best of N`` timings keep a loaded host from tripping this.
        assert before_ms >= classify_ms * 0.5, report[-1]
        # The marked path does no classification at all: it is far cheaper
        # and, unlike the recompute, does not scale with the text length.
        assert after_ms < before_ms / 5, report[-1]
        # Absolute bound: the marker path is a dict lookup, not a scan. The
        # generous 5 ms ceiling still fails by a wide margin on the base,
        # where this same call costs the full recompute (6 ms at 13.4 KB).
        assert after_ms < 5.0, report[-1]
        # Scaling: the marker path must not grow with the text size.
        assert after_ms < classify_ms, report[-1]

    print("\n".join(report))  # visible with -s / on failure
