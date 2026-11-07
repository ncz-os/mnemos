"""Characterisation tests for ``mnemos/domain/portability/version_topology.py``.

Target: ``mnemos/domain/portability/version_topology.py`` (130 lines, 0 direct
test references on 2026-11-05). Two functions live in the module:

- ``_validate_version_parents`` — async; per-version parent validation used by
  the ``memory_versions`` sidecar import phase (``phases.py``). Consults
  database truth first (or a pre-fetched ``db_truth`` dict), then the
  in-envelope sidecar index, and refuses parents that resolve to a different
  record, owner or namespace. Optionally enforces ``require_in_envelope`` so a
  parent of a freshly-imported record must itself have been freshly inserted.
- ``_topo_sort_versions`` — sync; Kahn-style topological sort of the sidecar
  version DAG. Orders parents before children, breaks ties by
  ``(int(version_num), id)``, rejects duplicate ids with a ``ValueError``,
  tolerates cycles by appending their leftovers in key order, and appends
  id-less entries last.

These tests PIN the current behaviour of both functions — return shapes,
the duplicate-id rejection message verbatim, falsy-id and falsy-parent
handling, cycle tolerance, the DB-truth-over-envelope precedence, the
``preserve_owner`` scope switch, the ``require_in_envelope`` gate, and every
error path (``ValueError``, ``TypeError``, ``KeyError``, ``AttributeError``).
If a value or message changes intentionally, update the pin together with
the change.

Two suspected defects are pinned as ``xfail(strict=True)`` — see
``test_validate_db_truth_parent_ignores_preserve_owner_scope`` and
``test_validate_require_in_envelope_short_circuits_before_db_fetch`` — and
are listed in the delivery report. No production code is changed.

Everything external is faked: the backend is a plain object exposing a
coroutine ``fetch_memory_versions_by_ids`` (no database, no network), and
``_topo_sort_versions`` is pure in-memory. Every test is deterministic and
sleep-free. No new test dependency is introduced, so no ``pyproject.toml``
change is needed and nothing needs to skip.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.domain.portability.version_topology import (
    _topo_sort_versions,
    _validate_version_parents,
)


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeVersionRepo:
    """Captures ``fetch_memory_versions_by_ids`` calls; returns ``rows``.

    ``rows`` defaults to empty so a test that supplies ``db_truth`` never
    triggers a fetch and a test that does trigger one sees the configured
    rows (or none).
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = rows or []
        self.calls: list[list] = []
        self.raise_exc: BaseException | None = None

    async def fetch_memory_versions_by_ids(self, tx, ids) -> list[dict]:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append(list(ids))
        wanted = {str(i) for i in ids}
        return [r for r in self.rows if str(r.get("id")) in wanted]

    def configure_raise(self, exc: BaseException) -> None:
        self.raise_exc = exc


def _backend(rows: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(memory_versions=_FakeVersionRepo(rows))


_TX = object()  # sentinel standing in for a real transaction


def _row(
    id: str = "p1",
    memory_id: str = "rec1",
    owner_id: str = "owner1",
    namespace: str = "ns1",
) -> dict:
    return {"id": id, "memory_id": memory_id, "owner_id": owner_id, "namespace": namespace}


async def _validate(
    backend=None,
    *,
    parent_uuids=None,
    expected_record_id: str = "rec1",
    effective_owner: str = "owner1",
    effective_ns: str | None = "ns1",
    in_envelope_index=None,
    preserve_owner: bool = True,
    require_in_envelope: bool = False,
    freshly_inserted_uuids=None,
    db_truth=None,
) -> tuple:
    return await _validate_version_parents(
        backend if backend is not None else _backend(),
        _TX,
        ["p1"] if parent_uuids is None else parent_uuids,
        expected_record_id=expected_record_id,
        effective_owner=effective_owner,
        effective_ns=effective_ns,
        in_envelope_index={} if in_envelope_index is None else in_envelope_index,
        preserve_owner=preserve_owner,
        require_in_envelope=require_in_envelope,
        freshly_inserted_uuids=freshly_inserted_uuids,
        db_truth=db_truth,
    )


def _ids(sidecar: list[dict]) -> list:
    """Extract the ``id`` of each output entry (``None`` for id-less ones)."""
    return [e.get("id") for e in sidecar]


# ══════════════════════════════════════════════════════════════════════
# _topo_sort_versions — DAG ordering
# ══════════════════════════════════════════════════════════════════════


def test_empty_sidecar_returns_empty_list():
    assert _topo_sort_versions([]) == []


def test_sidecar_of_only_idless_entries_returns_them_in_input_order():
    out = _topo_sort_versions([{"content": "a"}, {"content": "b"}])
    assert _ids(out) == [None, None]
    assert [e["content"] for e in out] == ["a", "b"]


def test_linear_chain_orders_parents_before_children():
    sidecar = [
        {"id": "v2", "version_num": 2, "parent_version_id": "v1"},
        {"id": "v1", "version_num": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["v1", "v2"]


def test_chain_is_ordered_regardless_of_input_order():
    entries = [{"id": f"v{i}", "version_num": i} for i in range(5)]
    for i in range(1, 5):
        entries[i]["parent_version_id"] = f"v{i - 1}"
    import random

    shuffled = entries[:]
    random.Random(7).shuffle(shuffled)
    assert _ids(_topo_sort_versions(shuffled)) == ["v0", "v1", "v2", "v3", "v4"]


def test_ready_queue_is_sorted_by_version_num_then_id():
    sidecar = [
        {"id": "m", "version_num": 2},
        {"id": "a", "version_num": 1},
        {"id": "z", "version_num": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "z", "m"]


def test_siblings_tie_break_on_id():
    sidecar = [
        {"id": "b", "version_num": 1},
        {"id": "a", "version_num": 1},
        {"id": "c", "version_num": 0},
        {"id": "d", "version_num": 2},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["c", "a", "b", "d"]


def test_missing_or_none_version_num_treated_as_zero():
    sidecar = [
        {"id": "z", "version_num": 5},
        {"id": "y"},  # key missing -> 0
        {"id": "x", "version_num": None},  # None -> 0
        {"id": "w", "version_num": "3"},  # numeric string -> 3
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["x", "y", "w", "z"]


def test_empty_string_version_num_treated_as_zero():
    sidecar = [
        {"id": "a", "version_num": ""},
        {"id": "b", "version_num": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b"]


def test_boolean_version_num_int_coerces_true_to_one_false_to_zero():
    sidecar = [
        {"id": "t", "version_num": True},
        {"id": "f", "version_num": False},
        {"id": "n", "version_num": 1},
    ]
    # False -> 0, True and 1 both -> 1, tie broken by id: f, n, t
    assert _ids(_topo_sort_versions(sidecar)) == ["f", "n", "t"]


def test_numeric_string_version_num_int_coerces():
    sidecar = [
        {"id": "a", "version_num": "10"},
        {"id": "b", "version_num": 9},
        {"id": "c", "version_num": 2},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["c", "b", "a"]


def test_float_version_num_truncates_toward_zero():
    sidecar = [
        {"id": "a", "version_num": 2.9},
        {"id": "b", "version_num": 2.1},
        {"id": "c", "version_num": 3},
    ]
    # int(2.9) == int(2.1) == 2 -> tie broken by id: a before b
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "c"]


def test_negative_version_num_sorts_before_zero():
    sidecar = [
        {"id": "a", "version_num": -1},
        {"id": "b", "version_num": 0},
        {"id": "c", "version_num": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "c"]


def test_parent_version_id_is_stringified_before_lookup():
    # parent_version_id is an int 1, child id is the string "1" -> linked.
    sidecar = [
        {"id": "v1", "version_num": 1},
        {"id": "v2", "version_num": 2, "parent_version_id": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["v1", "v2"]


def test_int_entry_id_is_stringified_and_parent_string_matches():
    # entry id is int 5 -> normalised to "5" in the index; a string parent "5" matches.
    sidecar = [
        {"id": 5, "version_num": 1},
        {"id": "c", "version_num": 2, "parent_version_id": "5"},
    ]
    out = _topo_sort_versions(sidecar)
    # the entry keeps its original (int) id in the output
    assert [e.get("id") for e in out] == [5, "c"]


def test_float_entry_id_is_stringified_and_parent_float_matches():
    sidecar = [
        {"id": 1.5, "version_num": 1},
        {"id": "c", "version_num": 2, "parent_version_id": 1.5},
    ]
    out = _topo_sort_versions(sidecar)
    assert [e.get("id") for e in out] == [1.5, "c"]


def test_truthy_bool_entry_id_is_stringified():
    sidecar = [{"id": True, "version_num": 1}]
    out = _topo_sort_versions(sidecar)
    assert [e.get("id") for e in out] == [True]


def test_dangling_parent_reference_is_ignored():
    sidecar = [{"id": "v1", "version_num": 1, "parent_version_id": "ghost"}]
    assert _ids(_topo_sort_versions(sidecar)) == ["v1"]


def test_falsy_parent_version_id_builds_no_edge():
    sidecar = [
        {"id": "a", "parent_version_id": 0},
        {"id": "b", "parent_version_id": ""},
        {"id": "c", "parent_version_id": None},
    ]
    # all roots -> sorted by (version_num=0, id)
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "c"]


def test_merge_parents_create_edges_and_ghost_entries_are_ignored():
    sidecar = [
        {"id": "m", "version_num": 3, "parent_version_id": "a", "merge_parents": ["b", None, "ghost"]},
        {"id": "a", "version_num": 1},
        {"id": "b", "version_num": 2},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "m"]


def test_merge_parents_alone_order_children_after_both_parents():
    sidecar = [
        {"id": "p1", "version_num": 1},
        {"id": "p2", "version_num": 2},
        {"id": "c", "version_num": 3, "merge_parents": ["p1", "p2"]},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["p1", "p2", "c"]


def test_merge_parents_falsey_entries_build_no_edges():
    sidecar = [
        {"id": "a", "merge_parents": [None, ""]},
        {"id": "b"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b"]


def test_duplicate_parent_and_merge_parent_target_counts_the_edge_once_each():
    # parent_version_id and merge_parents both name "root": in_degree 2, but
    # both decrement when root is processed, so the child still emits once.
    sidecar = [
        {"id": "root", "version_num": 1},
        {"id": "child", "version_num": 2, "parent_version_id": "root", "merge_parents": ["root"]},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["root", "child"]


def test_duplicate_merge_parent_names_are_processed_without_loss():
    sidecar = [
        {"id": "a", "version_num": 1},
        {"id": "b", "version_num": 2, "merge_parents": ["a", "a"]},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b"]


def test_diamond_orders_root_then_both_parents_then_join():
    sidecar = [
        {"id": "j", "version_num": 4, "parent_version_id": "l", "merge_parents": ["r"]},
        {"id": "r", "version_num": 3, "parent_version_id": "root"},
        {"id": "l", "version_num": 2, "parent_version_id": "root"},
        {"id": "root", "version_num": 1},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["root", "l", "r", "j"]


def test_binary_insertion_keeps_ready_sorted_when_children_are_released_out_of_order():
    # root's children are released in edge-insertion order (later, sooner),
    # but the binary insert re-sorts by key so the output is (version_num, id).
    sidecar = [
        {"id": "root", "version_num": 0},
        {"id": "later", "version_num": 9, "parent_version_id": "root"},
        {"id": "sooner", "version_num": 1, "parent_version_id": "root"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["root", "sooner", "later"]


def test_nested_release_reorders_ready_queue_by_key():
    # After root, A(5) is released first (ready=[A]); then B(3) is released
    # and binary-inserted before A; then C(4) is released from B and
    # binary-inserted before A. Output: root, B, C, A.
    sidecar = [
        {"id": "root", "version_num": 0},
        {"id": "A", "version_num": 5, "parent_version_id": "root"},
        {"id": "B", "version_num": 3, "parent_version_id": "root"},
        {"id": "C", "version_num": 4, "parent_version_id": "B"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["root", "B", "C", "A"]


def test_self_cycle_node_is_appended_as_its_own_leftover():
    sidecar = [{"id": "s", "parent_version_id": "s"}]
    assert _ids(_topo_sort_versions(sidecar)) == ["s"]


def test_two_node_cycle_is_appended_in_version_num_then_id_order():
    sidecar = [
        {"id": "c1", "version_num": 2, "parent_version_id": "c2"},
        {"id": "c2", "version_num": 1, "parent_version_id": "c1"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["c2", "c1"]


def test_cycle_leftovers_are_appended_after_the_acyclic_prefix_in_key_order():
    sidecar = [
        {"id": "ok1", "version_num": 1},
        {"id": "ok2", "version_num": 2, "parent_version_id": "ok1"},
        {"id": "x1", "version_num": 5, "parent_version_id": "x2"},
        {"id": "x2", "version_num": 4, "parent_version_id": "x1"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["ok1", "ok2", "x2", "x1"]


def test_two_disjoint_cycles_append_leftovers_sorted_by_key():
    sidecar = [
        {"id": "y2", "version_num": 9, "parent_version_id": "y1"},
        {"id": "y1", "version_num": 8, "parent_version_id": "y2"},
        {"id": "z2", "version_num": 1, "parent_version_id": "z1"},
        {"id": "z1", "version_num": 2, "parent_version_id": "z2"},
        {"id": "ok", "version_num": 0},
    ]
    # acyclic prefix: ok(0); leftovers sorted by (version_num, id): z2(1), z1(2), y1(8), y2(9)
    assert _ids(_topo_sort_versions(sidecar)) == ["ok", "z2", "z1", "y1", "y2"]


def test_cycle_leftovers_come_before_idless_entries():
    sidecar = [
        {"id": "x1", "parent_version_id": "x2"},
        {"id": "x2", "parent_version_id": "x1"},
        {"content": "orphan"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["x1", "x2", None]


def test_idless_entries_are_appended_after_sorted_id_entries():
    sidecar = [
        {"content": "n1"},
        {"id": "a", "version_num": 1},
        {"content": "n2"},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", None, None]


def test_falsy_entry_ids_bypass_the_index_and_are_appended_last():
    # 0 and "" are falsy -> treated as id-less, appended after the sorted output.
    sidecar = [{"id": "keep"}, {"id": 0}, {"id": ""}]
    assert _ids(_topo_sort_versions(sidecar)) == ["keep", 0, ""]


def test_falsy_entry_ids_are_not_deduplicated():
    # Three entries with id 0: none is indexed, so no duplicate is detected.
    sidecar = [{"id": 0}, {"id": 0}, {"id": 0}]
    assert _ids(_topo_sort_versions(sidecar)) == [0, 0, 0]


def test_idless_entries_are_never_deduplicated():
    sidecar = [{"content": "x"}, {"content": "x"}]
    assert _ids(_topo_sort_versions(sidecar)) == [None, None]


def test_non_string_entry_id_is_indexed_via_str_but_output_keeps_original():
    sidecar = [{"id": {"x": 1}, "version_num": 1}]
    out = _topo_sort_versions(sidecar)
    assert [e.get("id") for e in out] == [{"x": 1}]


# ── duplicate-id rejection ─────────────────────────────────────────────


def test_duplicate_ids_raise_valueerror_with_verbatim_message():
    with pytest.raises(ValueError) as ei:
        _topo_sort_versions([{"id": "v1"}, {"id": "v1"}])
    assert str(ei.value) == (
        "memory_versions sidecar contains duplicate id(s): v1. "
        "Dedupe the sidecar before re-running; silent overwrite "
        "would drop an authoritative history entry."
    )


def test_duplicate_id_int_and_str_are_the_same_key_and_report_the_str_form():
    # str(1) == "1", so id=1 and id="1" collide; the message renders the str.
    with pytest.raises(ValueError) as ei:
        _topo_sort_versions([{"id": 1}, {"id": "1"}])
    assert "duplicate id(s): 1." in str(ei.value)


def test_duplicate_detection_is_reported_in_sorted_unique_order():
    # message lists sorted(set(duplicates)) — not first-seen order.
    with pytest.raises(ValueError) as ei:
        _topo_sort_versions([{"id": "z"}, {"id": "a"}, {"id": "m"}, {"id": "z"}, {"id": "a"}])
    assert "duplicate id(s): a, z." in str(ei.value)


def test_three_way_duplicate_reports_each_distinct_id_once():
    with pytest.raises(ValueError) as ei:
        _topo_sort_versions([{"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": "a"}])
    assert "duplicate id(s): a, b." in str(ei.value)


def test_duplicate_raises_before_any_sorting_or_indexing():
    # The check runs during the first pass, so a duplicate alongside an
    # otherwise valid DAG still raises rather than partially sorting.
    with pytest.raises(ValueError):
        _topo_sort_versions(
            [
                {"id": "root", "version_num": 1},
                {"id": "child", "version_num": 2, "parent_version_id": "root"},
                {"id": "root", "version_num": 9},
            ]
        )


# ── error paths ────────────────────────────────────────────────────────


def test_non_numeric_version_num_raises_valueerror_from_int_coercion():
    with pytest.raises(ValueError, match="invalid literal for int"):
        _topo_sort_versions([{"id": "a", "version_num": "abc"}])


def test_non_numeric_version_num_raises_during_the_initial_ready_sort():
    # The sort key is evaluated when the ready queue is first built, so the
    # failure happens before any output is produced.
    with pytest.raises(ValueError):
        _topo_sort_versions([{"id": "a", "version_num": "abc"}, {"id": "b", "version_num": 1}])


def test_list_version_num_raises_typeerror_from_int_coercion():
    with pytest.raises(TypeError, match="int\\(\\) argument must be a string"):
        _topo_sort_versions([{"id": "a", "version_num": [1]}])


def test_non_iterable_merge_parents_raises_typeerror():
    with pytest.raises(TypeError, match="'int' object is not iterable"):
        _topo_sort_versions([{"id": "a", "merge_parents": 5}])


def test_float_merge_parents_raises_typeerror():
    with pytest.raises(TypeError, match="'float' object is not iterable"):
        _topo_sort_versions([{"id": "a", "merge_parents": 1.0}])


def test_string_merge_parents_iterates_characters_and_builds_no_real_edges():
    # A string is iterable, so the loop walks its characters; "a" and "b"
    # happen to be ids here, but "ab" as a single value builds no edge.
    sidecar = [
        {"id": "a", "version_num": 1},
        {"id": "b", "version_num": 2},
        {"id": "c", "merge_parents": "ab"},
    ]
    # "ab" iterates as "a","b" -> c becomes a child of both a and b.
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "c"]


def test_dict_merge_parents_iterates_keys():
    sidecar = [
        {"id": "a", "version_num": 1},
        {"id": "b", "version_num": 2},
        {"id": "c", "merge_parents": {"b": 1}},
    ]
    assert _ids(_topo_sort_versions(sidecar)) == ["a", "b", "c"]


def test_non_mapping_sidecar_entry_raises_attributeerror():
    with pytest.raises(AttributeError, match="'list' object has no attribute 'get'"):
        _topo_sort_versions([["not-a-dict"]])


def test_integer_sidecar_entry_raises_attributeerror():
    with pytest.raises(AttributeError, match="'int' object has no attribute 'get'"):
        _topo_sort_versions([5])


# ══════════════════════════════════════════════════════════════════════
# _validate_version_parents — parent scope validation
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_empty_parent_list_short_circuits_without_backend_fetch():
    repo = _FakeVersionRepo()
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(backend, parent_uuids=[])
    assert (ok, bad) == (True, [])
    assert repo.calls == []


@pytest.mark.asyncio
async def test_db_truth_parent_matching_record_owner_and_namespace_is_accepted():
    ok, bad = await _validate(db_truth={"p1": ("rec1", "owner1", "ns1")})
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_db_truth_parent_with_wrong_record_is_rejected():
    ok, bad = await _validate(db_truth={"p1": ("rec2", "owner1", "ns1")})
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_db_truth_parent_with_wrong_owner_is_rejected():
    ok, bad = await _validate(db_truth={"p1": ("rec1", "ownerX", "ns1")})
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_db_truth_parent_with_wrong_namespace_is_rejected():
    ok, bad = await _validate(db_truth={"p1": ("rec1", "owner1", "nsX")})
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_db_truth_none_owner_matches_only_none_effective_owner():
    ok, bad = await _validate(db_truth={"p1": ("rec1", None, "ns1")})
    assert (ok, bad) == (False, ["p1"])  # effective_owner is "owner1", not None

    ok, bad = await _validate(effective_owner=None, db_truth={"p1": ("rec1", None, "ns1")})
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_db_truth_none_namespace_matches_only_none_effective_namespace():
    ok, bad = await _validate(db_truth={"p1": ("rec1", "owner1", None)})
    assert (ok, bad) == (False, ["p1"])

    ok, bad = await _validate(effective_ns=None, db_truth={"p1": ("rec1", "owner1", None)})
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_db_truth_wins_over_a_contradictory_envelope_entry():
    # DB truth is consulted first and the branch `continue`s, so an envelope
    # entry that would allow the parent cannot rescue a DB-rejected one.
    ok, bad = await _validate(
        db_truth={"p1": ("rec2", "owner1", "ns1")},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": "owner1", "namespace": "ns1"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_envelope_parent_matching_record_owner_and_namespace_is_accepted():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": "owner1", "namespace": "ns1"}},
    )
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_envelope_parent_with_none_owner_or_namespace_is_treated_as_a_match():
    # `same_owner = (ref_owner is None) or (...)` — a None field is a wildcard.
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": None, "namespace": None}},
    )
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_envelope_parent_with_wrong_record_is_rejected():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "recX", "owner_id": "owner1", "namespace": "ns1"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_envelope_parent_with_wrong_owner_is_rejected():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": "ownerX", "namespace": "ns1"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_envelope_parent_with_wrong_namespace_is_rejected():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": "owner1", "namespace": "nsX"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_envelope_parent_missing_record_id_key_is_rejected():
    # ref.get("record_id") is None -> same_record False -> rejected.
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"owner_id": "owner1", "namespace": "ns1"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_envelope_parent_with_explicit_none_record_id_is_rejected():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": None, "owner_id": "owner1", "namespace": "ns1"}},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_preserve_owner_false_skips_owner_and_namespace_for_envelope_parents():
    # With preserve_owner=False both same_owner and same_ns are hard-coded True,
    # so only the record id is compared for an envelope parent.
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "rec1", "owner_id": "ownerX", "namespace": "nsX"}},
        preserve_owner=False,
    )
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_preserve_owner_false_still_rejects_envelope_record_mismatch():
    ok, bad = await _validate(
        db_truth={},
        in_envelope_index={"p1": {"record_id": "recX", "owner_id": "ownerX", "namespace": "nsX"}},
        preserve_owner=False,
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: _validate_version_parents applies preserve_owner only to the "
        "envelope branch. The db_truth branch (lines 34-38) unconditionally "
        "compares owner != effective_owner and ns != effective_ns, so with "
        "preserve_owner=False a DB-truth parent whose stored owner/namespace "
        "differs from the caller's effective scope is still rejected — even "
        "though the envelope branch would accept the identical mismatch. The "
        "only caller (phases.py:294) is currently gated to preserve_owner=True "
        "for memory_versions imports (import_.py rejects the sidecar otherwise), "
        "so this is latent, not live; but the asymmetry means a future caller "
        "that relies on preserve_owner=False for DB-resolved parents will get "
        "spurious cross-tenant rejections. Fix: guard the owner/ns comparison "
        "in the db_truth branch with `if preserve_owner:` like the envelope "
        "branch. The test asserts the scoped behaviour, so it xfails today."
    ),
)
@pytest.mark.asyncio
async def test_validate_db_truth_parent_ignores_preserve_owner_scope():
    """A DB-truth parent should honour ``preserve_owner=False`` like the envelope branch.

    Expected (safe) behaviour: with ``preserve_owner=False`` the owner and
    namespace comparison is skipped for DB-truth parents too, so a parent
    whose stored owner differs from the caller's effective owner is accepted
    as long as the record id matches. Today the db_truth branch ignores the
    flag and rejects it.
    """
    ok, bad = await _validate(
        db_truth={"p1": ("rec1", "original-owner", "ns1")},
        preserve_owner=False,
    )
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_preserve_owner_false_still_rejects_db_truth_record_mismatch():
    # record comparison is unconditional in the db_truth branch.
    ok, bad = await _validate(
        db_truth={"p1": ("other", "original-owner", "ns1")},
        preserve_owner=False,
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_parent_found_in_neither_db_truth_nor_envelope_is_rejected():
    ok, bad = await _validate(db_truth={})
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_non_string_parent_uuid_is_preserved_in_the_bad_list():
    ok, bad = await _validate(parent_uuids=[7], db_truth={})
    assert (ok, bad) == (False, [7])


@pytest.mark.asyncio
async def test_multiple_bad_parents_reported_in_parent_uuid_order():
    ok, bad = await _validate(
        parent_uuids=["p1", "p2", "p3"],
        db_truth={
            "p1": ("rec2", "owner1", "ns1"),
            "p2": ("rec1", "ownerX", "ns1"),
        },
    )
    assert (ok, bad) == (False, ["p1", "p2", "p3"])


@pytest.mark.asyncio
async def test_db_truth_dict_skips_the_backend_fetch_entirely():
    repo = _FakeVersionRepo()
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(backend, db_truth={"p1": ("rec1", "owner1", "ns1")})
    assert (ok, bad) == (True, [])
    assert repo.calls == []


@pytest.mark.asyncio
async def test_db_truth_none_fetches_rows_and_builds_truth_from_them():
    repo = _FakeVersionRepo([_row()])
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(backend, db_truth=None)
    assert (ok, bad) == (True, [])
    assert repo.calls == [["p1"]]


@pytest.mark.asyncio
async def test_db_truth_none_with_no_matching_rows_rejects_the_parent():
    repo = _FakeVersionRepo([])  # fetch returns nothing for p1
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(backend, db_truth=None)
    assert (ok, bad) == (False, ["p1"])
    assert repo.calls == [["p1"]]


@pytest.mark.asyncio
async def test_db_truth_none_fetches_exactly_the_requested_parent_ids():
    repo = _FakeVersionRepo([_row("p2"), _row("p1")])
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(
        backend,
        parent_uuids=["p1", "p2"],
        db_truth=None,
    )
    assert (ok, bad) == (True, [])
    assert repo.calls == [["p1", "p2"]]


@pytest.mark.asyncio
async def test_backend_fetch_row_missing_namespace_key_raises_keyerror():
    repo = _FakeVersionRepo([{"id": "p1", "memory_id": "rec1", "owner_id": "owner1"}])
    backend = SimpleNamespace(memory_versions=repo)
    with pytest.raises(KeyError, match="namespace"):
        await _validate(backend, db_truth=None)


@pytest.mark.asyncio
async def test_backend_fetch_row_short_tuple_raises_valueerror():
    class _ShortRepo:
        async def fetch_memory_versions_by_ids(self, tx, ids):
            return [{"id": "p1", "memory_id": "rec1"}]  # dict, not tuple

    backend = SimpleNamespace(memory_versions=_ShortRepo())
    with pytest.raises(ValueError, match="not enough values to unpack"):
        await _validate(backend, db_truth={"p1": ("rec1",)})


@pytest.mark.asyncio
async def test_backend_fetch_propagates_repo_exceptions():
    repo = _FakeVersionRepo()
    repo.configure_raise(RuntimeError("db exploded"))
    backend = SimpleNamespace(memory_versions=repo)
    with pytest.raises(RuntimeError, match="db exploded"):
        await _validate(backend, db_truth=None)


# ── require_in_envelope gate ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_in_envelope_accepts_a_freshly_inserted_parent():
    ok, bad = await _validate(
        require_in_envelope=True,
        freshly_inserted_uuids={"p1"},
        db_truth={"p1": ("rec1", "owner1", "ns1")},
    )
    assert (ok, bad) == (True, [])


@pytest.mark.asyncio
async def test_require_in_envelope_rejects_a_parent_that_was_not_freshly_inserted():
    # The parent is present in db_truth and would pass the record/owner/ns
    # checks, but require_in_envelope rejects it before consulting either.
    ok, bad = await _validate(
        require_in_envelope=True,
        freshly_inserted_uuids=set(),
        db_truth={"p1": ("rec1", "owner1", "ns1")},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_require_in_envelope_with_none_fresh_set_rejects_and_still_fetches():
    # freshly_inserted_uuids=None fails the `p not in` membership test, so
    # every parent is rejected — but the db_truth fetch still happens first.
    repo = _FakeVersionRepo([_row()])
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(
        backend,
        require_in_envelope=True,
        freshly_inserted_uuids=None,
        db_truth=None,
    )
    assert (ok, bad) == (False, ["p1"])
    assert repo.calls == [["p1"]]


@pytest.mark.asyncio
async def test_require_in_envelope_fresh_parent_still_consults_db_truth_first():
    # A fresh parent that is already in db_truth with a bad record is still
    # rejected by the db_truth branch (the gate only adds a precondition).
    ok, bad = await _validate(
        require_in_envelope=True,
        freshly_inserted_uuids={"p1"},
        db_truth={"p1": ("rec2", "owner1", "ns1")},
    )
    assert (ok, bad) == (False, ["p1"])


@pytest.mark.asyncio
async def test_require_in_envelope_false_never_checks_the_fresh_set():
    # Even with an empty fresh set, require_in_envelope=False skips the gate
    # and the parent resolves through db_truth normally.
    ok, bad = await _validate(
        require_in_envelope=False,
        freshly_inserted_uuids=set(),
        db_truth={"p1": ("rec1", "owner1", "ns1")},
    )
    assert (ok, bad) == (True, [])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: _validate_version_parents performs the db_truth fetch "
        "(lines 25-27) BEFORE evaluating the require_in_envelope gate "
        "(lines 30-33). When the gate rejects a parent — the common case "
        "for a non-fresh parent on an already-imported record — the "
        "fetch has already run, so the query is wasted work on the "
        "reject path. Today the caller (phases.py:294) always supplies "
        "a pre-fetched db_truth, so the waste is latent; but the direct "
        "call path (db_truth=None) pays a query for every rejected "
        "parent. Fix: evaluate the require_in_envelope membership test "
        "before the `if db_truth is None:` fetch block. The test asserts "
        "the short-circuit (no fetch on the reject path), so it xfails."
    ),
)
@pytest.mark.asyncio
async def test_validate_require_in_envelope_short_circuits_before_db_fetch():
    """The require_in_envelope reject path should not trigger a DB fetch.

    Expected (efficient) behaviour: when ``require_in_envelope=True`` and the
    parent is not in ``freshly_inserted_uuids``, the function rejects it
    immediately without consulting ``db_truth`` or fetching rows. Today the
    fetch runs first, so the reject path still issues the query.
    """
    repo = _FakeVersionRepo([_row()])
    backend = SimpleNamespace(memory_versions=repo)
    ok, bad = await _validate(
        backend,
        require_in_envelope=True,
        freshly_inserted_uuids=set(),
        db_truth=None,
    )
    assert (ok, bad) == (False, ["p1"])
    # SAFE behaviour: no fetch should have been issued.
    assert repo.calls == []
