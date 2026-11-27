"""Characterisation: cross-reference allowlist (FB754).

Pins `_is_allowed_reference` and `_build_referenced_memory_allowlist` in
`mnemos/domain/portability/allowlist.py`. Fakes only: the builder is driven
against a fake backend so no database is involved.
"""

from __future__ import annotations

import asyncio

import pytest

from mnemos.domain.portability.allowlist import (
    _build_referenced_memory_allowlist,
    _is_allowed_reference,
)
from mnemos.domain.portability.schemas import MPFEnvelope, MPFRecord

REFUSAL = "cross-tenant attachment refused"


class _FakeMemories:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch_referenced_memory_allowlist(self, tx, *, referenced_ids, scope_owner=None, scope_namespace=None):
        self.calls.append(
            {
                "tx": tx,
                "referenced_ids": list(referenced_ids),
                "scope_owner": scope_owner,
                "scope_namespace": scope_namespace,
            }
        )
        return self.rows


class _FakeBackend:
    def __init__(self, rows=None):
        self.memories = _FakeMemories(rows)


def _record(record_id: str, kind: str = "memory") -> MPFRecord:
    return MPFRecord(
        id=record_id,
        kind=kind,
        payload_version="mnemos-3.1",
        payload={"content": "x"},
    )


def _allowlist(*memory_ids: str, owner="alice", namespace="default"):
    return {mid: (owner, namespace) for mid in memory_ids}


def _build(backend, envelope, **kwargs):
    return asyncio.run(_build_referenced_memory_allowlist(backend, object(), envelope, **kwargs))


# ── _is_allowed_reference: allow ────────────────────────────────────────


@pytest.mark.parametrize(("owner", "namespace"), [("alice", "default"), ("bob", "bob-ns")])
def test_reference_inside_the_export_is_allowed(owner, namespace):
    allowlist = {f"mem_{owner}": (owner, namespace)}
    assert _is_allowed_reference(
        f"mem_{owner}",
        effective_owner=owner,
        effective_namespace=namespace,
        allowlist=allowlist,
    ) == (True, "")


def test_namespace_mismatch_allowed_when_the_check_is_disabled():
    allowlist = {"mem_1": ("alice", "other-ns")}
    assert _is_allowed_reference(
        "mem_1",
        effective_owner="alice",
        effective_namespace="default",
        allowlist=allowlist,
        require_namespace_match=False,
    ) == (True, "")


def test_none_reference_is_always_allowed():
    for allowlist in ({}, _allowlist("mem_1")):
        assert _is_allowed_reference(
            None,
            effective_owner="alice",
            effective_namespace="default",
            allowlist=allowlist,
        ) == (True, "")


def test_allowlist_values_only_need_to_unpack():
    assert _is_allowed_reference(
        "mem_1",
        effective_owner="alice",
        effective_namespace="default",
        allowlist={"mem_1": ["alice", "default"]},
    ) == (True, "")


# ── _is_allowed_reference: refuse ───────────────────────────────────────


def test_reference_outside_the_export_is_refused():
    allowed, reason = _is_allowed_reference(
        "mem_other",
        effective_owner="alice",
        effective_namespace="default",
        allowlist=_allowlist("mem_1"),
    )
    assert allowed is False
    assert "not in caller-owned memory id set" in reason
    assert REFUSAL in reason
    assert "mem_other" in reason


def test_other_owner_is_refused_naming_both_owners():
    allowed, reason = _is_allowed_reference(
        "mem_1",
        effective_owner="alice",
        effective_namespace="default",
        allowlist={"mem_1": ("bob", "default")},
    )
    assert allowed is False
    assert "'bob'" in reason and "'alice'" in reason
    assert REFUSAL in reason


def test_other_namespace_is_refused_naming_both_namespaces():
    allowed, reason = _is_allowed_reference(
        "mem_1",
        effective_owner="alice",
        effective_namespace="default",
        allowlist={"mem_1": ("alice", "bob-ns")},
    )
    assert allowed is False
    assert "'bob-ns'" in reason and "'default'" in reason
    assert REFUSAL in reason


def test_owner_mismatch_still_refused_with_namespace_check_off():
    allowed, reason = _is_allowed_reference(
        "mem_1",
        effective_owner="alice",
        effective_namespace="default",
        allowlist={"mem_1": ("bob", "default")},
        require_namespace_match=False,
    )
    assert allowed is False
    assert "'bob'" in reason


def test_empty_allowlist_refuses_every_id_but_none():
    for memory_id in ("mem_1", "", " "):
        allowed, _ = _is_allowed_reference(
            memory_id,
            effective_owner="alice",
            effective_namespace="default",
            allowlist={},
        )
        assert allowed is False
    assert _is_allowed_reference(
        None,
        effective_owner="alice",
        effective_namespace="default",
        allowlist={},
    ) == (True, "")


# ── _build_referenced_memory_allowlist ─────────────────────────────────


def test_allowlist_is_built_from_all_three_sidecars():
    envelope = MPFEnvelope(
        mpf_version="0.2.0",
        records=[_record("mem_1")],
        kg_triples=[{"id": "kg_1", "memory_id": "mem_from_kg"}],
        memory_versions=[{"record_id": "mem_from_versions"}],
        compression_manifest=[{"record_id": "mem_from_compression"}],
    )
    backend = _FakeBackend(rows=[])
    _build(backend, envelope)
    assert sorted(backend.memories.calls[0]["referenced_ids"]) == [
        "mem_from_compression",
        "mem_from_kg",
        "mem_from_versions",
    ]


def test_allowlist_deduplicates_and_skips_empty_ids():
    envelope = MPFEnvelope(
        mpf_version="0.2.0",
        records=[_record("mem_1")],
        kg_triples=[
            {"id": "kg_1", "memory_id": "mem_a"},
            {"id": "kg_2", "memory_id": "mem_a"},
            {"id": "kg_3", "memory_id": ""},
            {"id": "kg_4"},
        ],
        compression_manifest=[{"record_id": "mem_a"}, {"record_id": None}],
    )
    backend = _FakeBackend(rows=[])
    _build(backend, envelope)
    assert backend.memories.calls[0]["referenced_ids"] == ["mem_a"]


def test_empty_export_builds_nothing_and_skips_the_query():
    backend = _FakeBackend(rows=[{"id": "mem_1", "owner_id": "alice", "namespace": "default"}])
    assert _build(backend, MPFEnvelope(mpf_version="0.2.0", records=[_record("mem_1")])) == {}
    assert backend.memories.calls == []


def test_allowlist_maps_rows_to_owner_and_namespace():
    envelope = MPFEnvelope(
        mpf_version="0.2.0",
        records=[_record("mem_1")],
        kg_triples=[{"id": "kg_1", "memory_id": "mem_a"}],
    )
    backend = _FakeBackend(
        rows=[
            {"id": "mem_a", "owner_id": "alice", "namespace": "default"},
            {"id": "mem_b", "owner_id": "bob", "namespace": "bob-ns"},
        ]
    )
    assert _build(backend, envelope) == {
        "mem_a": ("alice", "default"),
        "mem_b": ("bob", "bob-ns"),
    }


def test_allowlist_drops_referenced_ids_the_backend_did_not_return():
    envelope = MPFEnvelope(
        mpf_version="0.2.0",
        records=[_record("mem_1")],
        kg_triples=[
            {"id": "kg_1", "memory_id": "mem_a"},
            {"id": "kg_2", "memory_id": "mem_gone"},
        ],
    )
    backend = _FakeBackend(rows=[{"id": "mem_a", "owner_id": "alice", "namespace": "default"}])
    allowlist = _build(backend, envelope)
    assert "mem_gone" not in allowlist
    allowed, reason = _is_allowed_reference(
        "mem_gone",
        effective_owner="alice",
        effective_namespace="default",
        allowlist=allowlist,
    )
    assert allowed is False
    assert REFUSAL in reason


def test_scope_is_forwarded_to_the_backend():
    envelope = MPFEnvelope(
        mpf_version="0.2.0",
        records=[_record("mem_1")],
        kg_triples=[{"id": "kg_1", "memory_id": "mem_a"}],
    )
    backend = _FakeBackend(rows=[])
    _build(backend, envelope, scope_owner="alice", scope_namespace="alice-ns")
    call = backend.memories.calls[0]
    assert call["scope_owner"] == "alice"
    assert call["scope_namespace"] == "alice-ns"

    unscoped = _FakeBackend(rows=[])
    _build(unscoped, envelope)
    assert unscoped.memories.calls[0]["scope_owner"] is None
    assert unscoped.memories.calls[0]["scope_namespace"] is None


def test_empty_export_allows_nothing_end_to_end():
    envelope = MPFEnvelope(mpf_version="0.2.0", records=[_record("mem_1")])
    backend = _FakeBackend(rows=[])
    allowlist = _build(backend, envelope)
    assert allowlist == {}
    assert (
        _is_allowed_reference(
            "mem_1",
            effective_owner="alice",
            effective_namespace="default",
            allowlist=allowlist,
        )[0]
        is False
    )
