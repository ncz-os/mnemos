"""M1 (code-health decisions 2026-10-31): ``secret_*`` metadata is server-only.

The read path prefers the spans recorded at ingest
(``secret_detection.redact_field_with_stored``) over recomputing the
classifier, so the stored-span marker is authoritative. It was also
CALLER-WRITABLE: ``PATCH /v1/memories/{id}`` stored ``request.metadata``
exactly as sent and only re-classified when content/verbatim changed, so a
PATCH of metadata alone could plant

    {"metadata": {"secret_redact_fields": {"content": [[0, 0]]}}}

— a stored span list that masks nothing — and permanently switch OFF
read-time masking for that row (a stored ``[[0, 0]]`` list is "truthy",
so ``redact_field_with_stored`` never falls back to the recompute
backstop). The same hole applied to create, bulk create and to federated
metadata (``classify_persisted_text_fields`` carried the caller's keys
straight through).

Contract enforced here:

1. Every caller-/peer-supplied ``secret_*`` key is stripped on every write
   path (create, bulk create, PATCH, import, federation ingest) — the
   stored-span marker and friends are stamped by the server only.
2. A PATCH that changes metadata re-derives classification from the
   persisted text, so the marker can never go stale or disappear.

Test: plant ``[[0, 0]]`` via PATCH, then GET as a group reader still shows
``[REDACTED]``.

The fixture secret is a 64-char hex digest: the classifier's REDACT tier
(an incidental span — the row is NOT moved to the vault namespace), so the
row stays readable to a same-group reader and the masking is observable on
an ordinary read. It is a synthetic value, not a real credential.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Response

import mnemos.api.routes.memories as memories
import mnemos.core.lifecycle as lc
from mnemos.api.dependencies import UserContext
from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest, MemoryUpdateRequest

# Synthetic, obviously-fake secret-shaped value (these tests ship in a
# published sdist, so they must never carry a real credential).
SECRET = "a3f9b2c1d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d"
CLEAN_TEXT = "deployment finished; see the status page at https://status.example.com/42"


def _str(text: str) -> str:
    return f"deploy note: the artifact fingerprint is {text} for build 17"


SECRET_TEXT = _str(SECRET)


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=["g-team"],
        role="user",
        namespace="team",
        authenticated=True,
    )


def _root() -> UserContext:
    """Root caller: only used by the test helper that inspects the STORE."""
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _group_reader() -> UserContext:
    """A different user in the same group: the group-readable test reader."""
    return UserContext(
        user_id="bob",
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

    be = SqliteBackend(tmp_path / "secret_metadata.sqlite3", SimpleNamespace())
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


def _create_request(content: str = SECRET_TEXT, **overrides) -> MemoryCreateRequest:
    kwargs = {
        "content": content,
        "category": "notes",
        "namespace": "team",
        "permission_mode": 644,  # group-readable
    }
    kwargs.update(overrides)
    return MemoryCreateRequest(**kwargs)


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


async def _get_as(memory_id: str, user: UserContext) -> dict:
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v1/memories/{memory_id}",
            "headers": [],
            "query_string": b"",
        }
    )
    resp = await memories.get_memory(memory_id, request, user=user)
    return json.loads(resp.body)


# ── M1 core repro ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_planted_empty_span_does_not_disarm_redaction(backend):
    """The exact M1 exploit: PATCH metadata-only with ``[[0, 0]]``."""
    created = await memories.create_memory(_create_request(), Response(), user=_alice())
    assert created.namespace == "team", "fixture must stay out of the vault to be group-readable"

    # Before the PATCH, a group reader sees masked text.
    before = await _get_as(created.id, _group_reader())
    assert SECRET not in before["content"]
    assert "[REDACTED]" in before["content"]

    # Plant the empty span list through a metadata-only PATCH.
    await memories.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"secret_redact_fields": {"content": [[0, 0]]}}),
        user=_alice(),
    )

    after = await _get_as(created.id, _group_reader())
    assert SECRET not in after["content"], "planted stored span disarmed read-time masking (M1)"
    assert "[REDACTED]" in after["content"]


@pytest.mark.asyncio
async def test_patch_planted_empty_span_does_not_disarm_owner_read(backend):
    """The owner's own read stays masked too: no caller may switch the marker off."""
    created = await memories.create_memory(_create_request(), Response(), user=_alice())

    await memories.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"secret_redact_fields": {"content": [[0, 0]]}}),
        user=_alice(),
    )

    as_owner = await _get_as(created.id, _alice())
    assert SECRET not in as_owner["content"]
    assert "[REDACTED]" in as_owner["content"]


@pytest.mark.asyncio
async def test_patch_planted_legacy_spans_do_not_disarm_redaction(backend):
    """The legacy content-only ``secret_redact_spans`` key is caller-writable too."""
    created = await memories.create_memory(_create_request(), Response(), user=_alice())
    await memories.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"secret_redact_spans": [[0, 0]]}),
        user=_alice(),
    )

    after = await _get_as(created.id, _group_reader())
    assert SECRET not in after["content"]
    assert "[REDACTED]" in after["content"]


# ── caller-supplied secret_* keys never reach the store ─────────────────────


@pytest.mark.asyncio
async def test_create_strips_caller_supplied_secret_keys(backend):
    created = await memories.create_memory(
        _create_request(
            metadata={
                "team": "infra",
                "secret_redact_fields": {"content": [[0, 0]]},
                "secret_redact_spans": [[0, 0]],
                "secret_vaulted": False,
                "secret_original_namespace": "elsewhere",
                "secret_classified_at": "forged",
                "secret_reasons": ["forged"],
                "secret_classification_error": False,
            }
        ),
        Response(),
        user=_alice(),
    )
    meta = await _stored_metadata(backend, created.id)
    assert meta.get("team") == "infra"
    assert meta.get("secret_redact_fields") != {"content": [[0, 0]]}
    # Spans are persisted as JSON, so compare against the JSON-normalised form.
    assert [list(span) for span in meta.get("secret_redact_fields", {}).get("content", [])] == [[41, 105]], meta
    assert meta.get("secret_redact_spans") != [[0, 0]]
    assert meta.get("secret_vaulted") is not False
    assert meta.get("secret_classified_at") == "ingest"
    assert meta.get("secret_original_namespace") != "elsewhere"
    assert meta.get("secret_reasons") == ["content:hex64", "verbatim_content:hex64"]

    viewed = await _get_as(created.id, _group_reader())
    assert SECRET not in viewed["content"]


@pytest.mark.asyncio
async def test_create_forged_marker_does_not_mask_the_row(backend):
    """A forged marker cannot be used to rewrite what the reader sees either."""
    created = await memories.create_memory(
        _create_request(content=CLEAN_TEXT, metadata={"secret_redact_fields": {"content": [[0, 12]]}}),
        Response(),
        user=_alice(),
    )
    meta = await _stored_metadata(backend, created.id)
    assert "secret_redact_fields" not in meta

    viewed = await _get_as(created.id, _group_reader())
    # The read path may wrap content in the untrusted-data frame; what
    # matters is that the forged marker did not rewrite the text.
    assert CLEAN_TEXT in viewed["content"]


@pytest.mark.asyncio
async def test_bulk_create_strips_caller_supplied_secret_keys(backend):
    resp = await memories.bulk_create_memories(
        BulkCreateRequest(
            memories=[
                _create_request(
                    metadata={
                        "secret_redact_fields": {"content": [[0, 0]]},
                        "secret_redact_spans": [[0, 0]],
                        "secret_classified_at": "forged",
                        "room": "ops",
                    }
                )
            ]
        ),
        user=_alice(),
    )
    assert resp.errors == []
    meta = await _stored_metadata(backend, resp.memory_ids[0])
    assert meta.get("room") == "ops"
    assert meta.get("secret_redact_fields") != {"content": [[0, 0]]}
    assert meta.get("secret_redact_spans") != [[0, 0]]
    assert meta.get("secret_classified_at") == "bulk_ingest"

    viewed = await _get_as(resp.memory_ids[0], _group_reader())
    assert SECRET not in viewed["content"]


@pytest.mark.asyncio
async def test_patch_strips_caller_supplied_secret_keys(backend):
    created = await memories.create_memory(_create_request(), Response(), user=_alice())
    await memories.update_memory(
        created.id,
        MemoryUpdateRequest(
            metadata={
                "secret_redact_fields": {"content": [[0, 0]]},
                "secret_vaulted": False,
                "secret_classified_at": "forged",
                "note": "hand-off",
            }
        ),
        user=_alice(),
    )
    meta = await _stored_metadata(backend, created.id)
    assert meta.get("note") == "hand-off"
    assert meta.get("secret_redact_fields") != {"content": [[0, 0]]}
    assert meta.get("secret_classified_at") == "patch"

    viewed = await _get_as(created.id, _group_reader())
    assert SECRET not in viewed["content"]


# ── metadata change re-classifies (marker cannot go stale) ──────────────────


@pytest.mark.asyncio
async def test_metadata_only_patch_restamps_classification(backend):
    """A metadata PATCH re-runs classification instead of trusting the store."""
    created = await memories.create_memory(_create_request(), Response(), user=_alice())
    before = await _stored_metadata(backend, created.id)
    assert before.get("secret_redact_fields")

    await memories.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"note": "classification must be refreshed"}),
        user=_alice(),
    )
    after = await _stored_metadata(backend, created.id)
    assert after.get("note") == "classification must be refreshed"
    assert after.get("secret_redact_fields") == before.get("secret_redact_fields")
    assert after.get("secret_classified_at") == "patch"


@pytest.mark.asyncio
async def test_clean_metadata_patch_does_not_fabricate_a_vault(backend):
    """Re-classification on a metadata PATCH leaves clean rows where they are."""
    created = await memories.create_memory(_create_request(content=CLEAN_TEXT), Response(), user=_alice())
    updated = await memories.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"team": "infra"}),
        user=_alice(),
    )
    assert updated.namespace == "team"
    meta = await _stored_metadata(backend, created.id)
    assert not meta.get("secret_vaulted")
    assert meta.get("team") == "infra"
    assert "secret_redact_fields" not in meta


# ── federation / import peers cannot smuggle the marker ─────────────────────


def test_federation_nats_event_strips_peer_supplied_secret_keys():
    from mnemos.federation.nats_consumer import _memory_from_event

    mem = _memory_from_event(
        {
            "content": SECRET_TEXT,
            "namespace": "default",
            "metadata": {
                "secret_redact_fields": {"content": [[0, 0]]},
                "secret_redact_spans": [[0, 0]],
                "peer_tag": "p1",
            },
        },
        peer_name="peer-a",
        memory_id="mem_fed_m1",
    )
    assert mem["metadata"].get("peer_tag") == "p1"
    assert mem["metadata"].get("secret_redact_fields") != {"content": [[0, 0]]}
    assert [list(span) for span in mem["metadata"].get("secret_redact_fields", {}).get("content", [])] == [[41, 105]]
    assert mem["metadata"].get("secret_redact_spans") != [[0, 0]]


def test_federation_worker_event_strips_peer_supplied_secret_keys():
    from mnemos.workers.federation_memory_nats_consumer import _memory_from_event

    peer = SimpleNamespace(name="peer-b")
    mem = _memory_from_event(
        {
            "memory_id": "mem_fed_worker_m1",
            "content": SECRET_TEXT,
            "namespace": "default",
            "metadata": {
                "secret_redact_fields": {"content": [[0, 0]]},
                "peer_tag": "p2",
            },
        },
        peer,
    )
    assert mem["metadata"].get("secret_redact_fields") != {"content": [[0, 0]]}
    assert mem["metadata"].get("peer_tag") == "p2"


def test_classify_persisted_text_fields_ignores_caller_secret_keys():
    """The choke point: the classifier rebuilds secret_* from scratch."""
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields

    classified = classify_persisted_text_fields(
        content=CLEAN_TEXT,
        metadata={
            "secret_redact_fields": {"content": [[0, 0]]},
            "secret_redact_spans": [[0, 0]],
            "secret_vaulted": True,
            "secret_original_namespace": "team",
            "host": "web-1",
        },
        namespace="team",
        classified_at="ingest",
        memory_id="mem_m1_unit",
    )
    meta = classified.metadata
    # The server's own M6 CLEAN stamp for the field it just classified is the
    # only extra key: every caller-supplied ``secret_*`` key is gone.
    assert meta == {"host": "web-1", "secret_field_classifiers": {"content": 1}}
    assert classified.namespace == "team"


def test_classify_persisted_text_fields_restamps_over_a_stale_server_marker():
    """A stale server marker on the row does not survive a re-classification."""
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields

    classified = classify_persisted_text_fields(
        content=CLEAN_TEXT,
        metadata={"secret_redact_fields": {"content": [[0, 12]]}, "secret_classified_at": "ingest"},
        namespace="team",
        classified_at="patch",
        memory_id="mem_m1_unit2",
    )
    assert "secret_redact_fields" not in classified.metadata
    assert "secret_classified_at" not in classified.metadata


# ── read-path unit guards (already-persisted rows) ─────────────────────────


def test_redact_field_with_stored_ignores_degenerate_spans():
    """A stored span list that masks nothing must not disable the recompute."""
    from mnemos.core.secret_detection import redact_field_with_stored

    for planted in ([[0, 0]], [[5, 5]], [[10, 3]], [[-4, -1]]):
        masked = redact_field_with_stored(SECRET_TEXT, {"secret_redact_fields": {"content": planted}}, "content")
        assert SECRET not in masked, f"planted span {planted} disabled the recompute backstop"
        assert "[REDACTED]" in masked


def test_redact_field_with_stored_legacy_degenerate_spans():
    from mnemos.core.secret_detection import redact_field_with_stored

    masked = redact_field_with_stored(SECRET_TEXT, {"secret_redact_spans": [[0, 0]]}, "content")
    assert SECRET not in masked


def test_redact_field_with_stored_real_stored_spans_still_win():
    """F2b is unchanged: a real stored span masks even when recompute would not."""
    from mnemos.core.secret_detection import redact_field_with_stored

    content = "the magic word is flibbertigibbet and it is harmless"
    stored = {"secret_redact_fields": {"content": [[15, 37]]}}
    via_stored = redact_field_with_stored(content, stored, "content")
    assert "flibbertigibbet" not in via_stored
    assert "[REDACTED]" in via_stored


def test_strip_caller_secret_metadata_helper():
    from mnemos.core.persisted_text_classification import strip_caller_secret_metadata

    stripped = strip_caller_secret_metadata(
        {
            "secret_redact_fields": {"content": [[0, 0]]},
            "secret_redact_spans": [[0, 0]],
            "secret_vaulted": True,
            "secret_original_namespace": "x",
            "secret_reasons": ["forged"],
            "secret_classified_at": "forged",
            "secret_classification_error": True,
            "secret_future_flag": True,
            "team": "infra",
        }
    )
    assert stripped == {"team": "infra"}
    assert strip_caller_secret_metadata(None) == {}
    assert strip_caller_secret_metadata({"ok": 1}) == {"ok": 1}
