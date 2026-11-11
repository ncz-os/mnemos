"""Characterisation tests for ``mnemos/domain/portability/allowlist.py``.

Target: ``mnemos/domain/portability/allowlist.py`` (69 lines, 0 direct test
references on 2026-11-05). Two functions live in the module:

- ``_build_referenced_memory_allowlist`` — async; collects referenced
  memory/record ids from the three MPF sidecars (``kg_triples`` via
  ``memory_id``, ``memory_versions`` via ``record_id``,
  ``compression_manifest`` via ``record_id``), dedupes them, and when
  non-empty asks ``backend.memories.fetch_referenced_memory_allowlist``
  for their (owner, namespace) rows.
- ``_is_allowed_reference`` — sync; the per-reference cross-tenant gate
  used by the sidecar import phases.

These tests PIN the current behaviour of both functions — return shapes,
falsy-entry skipping, dedup into a ``set`` (the fetched id list is a
materialised set, so its order is unspecified and the pin compares
as a set), the early-exit on an empty reference set, keyword-only
pass-through of scope, the allowlist row mapping, and every refusal
message in ``_is_allowed_reference`` verbatim (including ``!r`` quoting
of the ids). If a value or message changes intentionally, update the
pin together with the change.

One suspected defect is pinned as ``xfail(strict=True)`` — see
``test_build_allowlist_should_not_include_backend_rows_for_unreferenced_ids``.

The backend is a plain fake object with a coroutine
``fetch_referenced_memory_allowlist`` — no database, no network, no
sleeps, fully deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.domain.portability.allowlist import (
    _build_referenced_memory_allowlist,
    _is_allowed_reference,
)
from mnemos.domain.portability.schemas import MPFEnvelope


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeMemoryRepo:
    """Captures the allowlist fetch; returns or raises per configuration.

    ``rows`` defaults to a single known row (alice / alice-ns, id taken
    from the requested id list) so that happy-path tests don't have to
    configure it; tests that pin the row mapping override it explicitly.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rows: list[dict] | None = None
        self.raise_exc: BaseException | None = None

    async def fetch_referenced_memory_allowlist(
        self,
        tx,
        *,
        referenced_ids,
        scope_owner=None,
        scope_namespace=None,
    ) -> list[dict]:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append(
            {
                "tx": tx,
                "referenced_ids": list(referenced_ids),
                "scope_owner": scope_owner,
                "scope_namespace": scope_namespace,
            }
        )
        if self.rows is None:
            return [
                {"id": mid, "owner_id": "alice", "namespace": "alice-ns"}
                for mid in referenced_ids
            ]
        return list(self.rows)

    def configure_raise(self, exc: BaseException) -> None:
        self.raise_exc = exc


def _backend(repo: _FakeMemoryRepo) -> SimpleNamespace:
    return SimpleNamespace(memories=repo)


_TX = object()  # sentinel standing in for a real Transaction


def _envelope(**kwargs) -> MPFEnvelope:
    return MPFEnvelope(**kwargs)


# ── _build_referenced_memory_allowlist ─────────────────────────────────


@pytest.mark.asyncio
async def test_build_allowlist_returns_empty_dict_when_all_sidecars_are_none():
    backend = _backend(_FakeMemoryRepo())
    assert (
        await _build_referenced_memory_allowlist(
            backend, _TX, _envelope()
        )
        == {}
    )


@pytest.mark.asyncio
async def test_build_allowlist_returns_empty_dict_when_sidecars_are_empty_lists():
    backend = _backend(_FakeMemoryRepo())
    envelope = _envelope(
        kg_triples=[], memory_versions=[], compression_manifest=[]
    )
    assert (
        await _build_referenced_memory_allowlist(backend, _TX, envelope) == {}
    )


@pytest.mark.asyncio
async def test_build_allowlist_skips_entries_with_falsy_ids_without_backend_call():
    # memory_id / record_id of None, "" and a missing key are all skipped.
    repo = _FakeMemoryRepo()
    backend = _backend(repo)
    envelope = _envelope(
        kg_triples=[
            {"memory_id": None},
            {"memory_id": ""},
            {"other_key": "mem_noid"},
            {"memory_id": "mem_real"},
        ],
        memory_versions=[{"record_id": ""}, {"record_id": None}],
        compression_manifest=[{"engine_id": "apollo"}],
    )
    result = await _build_referenced_memory_allowlist(
        backend, _TX, envelope
    )
    assert result == {"mem_real": ("alice", "alice-ns")}
    assert [c["referenced_ids"] for c in repo.calls] == [["mem_real"]]


@pytest.mark.asyncio
async def test_build_allowlist_returns_empty_dict_when_all_ids_are_falsy():
    repo = _FakeMemoryRepo()
    backend = _backend(repo)
    envelope = _envelope(
        kg_triples=[{"memory_id": None}, {"memory_id": ""}],
        memory_versions=[{"record_id": ""}],
    )
    assert (
        await _build_referenced_memory_allowlist(backend, _TX, envelope) == {}
    )
    assert repo.calls == []


@pytest.mark.asyncio
async def test_build_allowlist_collects_ids_from_all_three_sidecars_and_dedupes():
    repo = _FakeMemoryRepo()
    repo.rows = [
        {"id": "m1", "owner_id": "alice", "namespace": "ns1"},
        {"id": "m2", "owner_id": "alice", "namespace": "ns1"},
        {"id": "m3", "owner_id": "alice", "namespace": "ns1"},
    ]
    backend = _backend(repo)
    envelope = _envelope(
        kg_triples=[{"memory_id": "m1"}, {"memory_id": "m2"}],
        memory_versions=[{"record_id": "m2"}, {"record_id": "m3"}],
        compression_manifest=[{"record_id": "m1"}],
    )
    result = await _build_referenced_memory_allowlist(
        backend, _TX, envelope
    )
    assert result == {
        "m1": ("alice", "ns1"),
        "m2": ("alice", "ns1"),
        "m3": ("alice", "ns1"),
    }
    # Ids are deduped through a set, so the list order is unspecified —
    # pin the set equality, not the order.
    assert set(repo.calls[0]["referenced_ids"]) == {"m1", "m2", "m3"}


@pytest.mark.asyncio
async def test_build_allowlist_passes_tx_and_scope_args_as_keywords():
    repo = _FakeMemoryRepo()
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    await _build_referenced_memory_allowlist(
        backend,
        _TX,
        envelope,
        scope_owner="alice",
        scope_namespace="alice-ns",
    )
    call = repo.calls[0]
    assert call["tx"] is _TX
    assert call["scope_owner"] == "alice"
    assert call["scope_namespace"] == "alice-ns"


@pytest.mark.asyncio
async def test_build_allowlist_defaults_scope_args_to_none_when_omitted():
    repo = _FakeMemoryRepo()
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    await _build_referenced_memory_allowlist(backend, _TX, envelope)
    assert repo.calls[0]["scope_owner"] is None
    assert repo.calls[0]["scope_namespace"] is None


@pytest.mark.asyncio
async def test_build_allowlist_uses_backend_rows_verbatim_including_missing_ids():
    # The result dict is built from what the backend returned, NOT from
    # the referenced set: an id that was referenced but absent from the
    # rows is simply not in the dict, and a row for an id the envelope
    # never referenced would also be included verbatim. Today this is
    # harmless by construction — the three call sites in ``phases.py``
    # only gate ids that appear in the same sidecars the builder scans —
    # but it is an unfiltered trust in the backend row set (see the
    # companion xfail pinning the safe filtering behaviour).
    repo = _FakeMemoryRepo()
    repo.rows = [
        {"id": "m2", "owner_id": "alice", "namespace": "ns1"},
        {"id": "m3", "owner_id": "bob", "namespace": "ns2"},
    ]
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    result = await _build_referenced_memory_allowlist(backend, _TX, envelope)
    assert result == {
        "m2": ("alice", "ns1"),
        "m3": ("bob", "ns2"),
    }


@pytest.mark.asyncio
async def test_build_allowlist_empty_backend_result_maps_to_empty_dict():
    repo = _FakeMemoryRepo()
    repo.rows = []
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    result = await _build_referenced_memory_allowlist(backend, _TX, envelope)
    assert result == {}
    assert repo.calls[0]["referenced_ids"] == ["m1"]


@pytest.mark.asyncio
async def test_build_allowlist_propagates_backend_exceptions():
    repo = _FakeMemoryRepo()
    repo.configure_raise(RuntimeError("db exploded"))
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    with pytest.raises(RuntimeError, match="db exploded"):
        await _build_referenced_memory_allowlist(backend, _TX, envelope)


@pytest.mark.asyncio
async def test_build_allowlist_backend_fetch_receives_keyword_only_ids_and_scope():
    """The ABC signature is keyword-only; the call must use keywords.

    Pins the call shape against the MemoryRepository ABC contract
    (``fetch_referenced_memory_allowlist(tx, *, referenced_ids, ...)``)
    so a positional refactor of the call site is caught.
    """
    repo = _FakeMemoryRepo()
    repo.rows = [{"id": "m1", "owner_id": "alice", "namespace": "ns"}]
    backend = _backend(repo)
    envelope = _envelope(
        kg_triples=[{"memory_id": "m1"}],
        memory_versions=[{"record_id": "m2"}],
    )
    await _build_referenced_memory_allowlist(
        backend, _TX, envelope, scope_owner="alice"
    )
    call = repo.calls[0]
    assert call["scope_owner"] == "alice"
    assert call["scope_namespace"] is None
    assert set(call["referenced_ids"]) == {"m1", "m2"}


# ── _is_allowed_reference ──────────────────────────────────────────────


def _allowed_ref(memory_id, **kwargs):
    base = {
        "effective_owner": "alice",
        "effective_namespace": "alice-ns",
        "allowlist": {"mem_a": ("alice", "alice-ns")},
    }
    base.update(kwargs)
    return _is_allowed_reference(memory_id, **base)


def test_is_allowed_reference_none_memory_id_is_unconditionally_allowed():
    # A None id short-circuits to (True, "") BEFORE any allowlist lookup
    # — the short-circuit lives in the helper itself. All three call
    # sites in ``phases.py`` (kg_triples: memory_id, memory_versions and
    # compression_manifest: record_id) pass ``entry.get(...)`` and
    # therefore rely on this for entries with no parent reference.
    ok, reason = _allowed_ref(None, allowlist={})
    assert (ok, reason) == (True, "")


def test_is_allowed_reference_none_memory_id_is_allowed_even_if_allowlist_has_other_ids():
    ok, reason = _allowed_ref(None)
    assert (ok, reason) == (True, "")


def test_is_allowed_reference_known_id_matching_owner_and_namespace_is_allowed():
    ok, reason = _allowed_ref("mem_a")
    assert (ok, reason) == (True, "")


def test_is_allowed_reference_id_not_in_allowlist_is_refused_with_message():
    ok, reason = _allowed_ref("mem_b")
    assert ok is False
    assert reason == (
        "record_id 'mem_b' not in caller-owned memory id set; "
        "skipped (cross-tenant attachment refused)"
    )


def test_is_allowed_reference_owner_mismatch_is_refused_with_message():
    allowlist = {"mem_b": ("bob", "alice-ns")}
    ok, reason = _allowed_ref("mem_b", allowlist=allowlist)
    assert ok is False
    assert reason == (
        "record_id 'mem_b' belongs to owner 'bob', not the sidecar's "
        "effective owner 'alice'; skipped (cross-tenant attachment refused)"
    )


def test_is_allowed_reference_namespace_mismatch_is_refused_with_message():
    allowlist = {"mem_b": ("alice", "other-ns")}
    ok, reason = _allowed_ref("mem_b", allowlist=allowlist)
    assert ok is False
    assert reason == (
        "record_id 'mem_b' is in namespace 'other-ns', not the sidecar's "
        "effective namespace 'alice-ns'; skipped (cross-tenant attachment refused)"
    )


def test_is_allowed_reference_require_namespace_match_false_allows_namespace_drift():
    allowlist = {"mem_b": ("alice", "other-ns")}
    ok, reason = _allowed_ref(
        "mem_b",
        allowlist=allowlist,
        require_namespace_match=False,
    )
    assert (ok, reason) == (True, "")


def test_is_allowed_reference_requires_both_owner_and_namespace_by_default():
    # require_namespace_match defaults to True; both checks must pass.
    allowlist = {"mem_b": ("alice", "other-ns")}
    ok, _ = _allowed_ref("mem_b", allowlist=allowlist)
    assert ok is False


def test_is_allowed_reference_empty_allowlist_refuses_every_known_id():
    ok, reason = _allowed_ref("mem_a", allowlist={})
    assert ok is False
    assert "not in caller-owned memory id set" in reason


def test_is_allowed_reference_id_and_owner_are_rendered_with_repr():
    # The message uses {id!r} / {owner!r} — Python repr quoting, which
    # flips the quote character when the literal contains one.
    allowlist = {"we\"ird": ("evil'owner", "ns")}
    ok, reason = _allowed_ref("we\"ird", allowlist=allowlist)
    assert ok is False
    assert "we\"ird" in reason
    assert "evil'owner" in reason


def test_is_allowed_reference_none_namespaces_match_when_ns_match_enabled():
    # A row whose namespace column is None only matches a sidecar whose
    # effective namespace is also None.
    allowlist = {"mem_none": ("alice", None)}
    ok, reason = _allowed_ref(
        "mem_none", effective_namespace=None, allowlist=allowlist
    )
    assert (ok, reason) == (True, "")


def test_is_allowed_reference_none_namespace_in_allowlist_is_refused_with_repr_none():
    # The refusal message renders the row's None namespace via !r as the
    # bare literal None.
    allowlist = {"mem_none": ("alice", None)}
    ok, reason = _allowed_ref(
        "mem_none", effective_namespace="alice-ns", allowlist=allowlist
    )
    assert ok is False
    assert reason == (
        "record_id 'mem_none' is in namespace None, not the sidecar's "
        "effective namespace 'alice-ns'; skipped (cross-tenant attachment refused)"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defensive-filtering gap (no live exploit identified): "
        "_build_referenced_memory_allowlist maps the backend rows "
        "verbatim — {row.id: (owner, ns) for row in rows} — without "
        "filtering them to the referenced_ids it just requested. If a "
        "backend implementation ever returns a row outside the "
        "requested id set (driver bug, scope leak), that id silently "
        "enters the allowlist and _is_allowed_reference will grant it. "
        "Fix: intersect rows with referenced_ids, or document on the "
        "MemoryRepository ABC that implementations guarantee the row "
        "set is a subset of referenced_ids. The test asserts the safe "
        "filtering behaviour (unreferenced row NOT in result), so it "
        "xfails while the filter is absent."
    ),
)
@pytest.mark.asyncio
async def test_build_allowlist_should_not_include_backend_rows_for_unreferenced_ids():
    """A backend row whose id was never referenced should not enter the allowlist.

    The current implementation maps every backend row verbatim with no
    intersection against ``referenced``. Today this is latent, not
    exploitable: the three ``phases.py`` call sites only gate ids that
    appear in the same sidecars the builder scans, so an extra row has
    no corresponding attachment to ride on. The pin protects the
    builder's implied contract (row set ⊆ referenced set) against
    future callers that gate ids from a different surface.
    """
    repo = _FakeMemoryRepo()
    # The fake returns a row for 'm9' that the envelope never references.
    repo.rows = [
        {"id": "m9", "owner_id": "alice", "namespace": "alice-ns"},
    ]
    backend = _backend(repo)
    envelope = _envelope(kg_triples=[{"memory_id": "m1"}])
    result = await _build_referenced_memory_allowlist(
        backend, _TX, envelope
    )
    # SAFE behaviour: 'm9' must not be in the allowlist.
    assert result == {}


def test_is_allowed_reference_owner_and_namespace_mismatch_reports_owner_first():
    allowlist = {"mem_c": ("bob", "other-ns")}
    ok, reason = _allowed_ref("mem_c", allowlist=allowlist)
    assert ok is False
    assert reason.startswith("record_id 'mem_c' belongs to owner 'bob'")
