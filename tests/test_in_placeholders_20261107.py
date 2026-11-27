"""FB1002: ``_in_placeholders`` contract, pure tests.

``_in_placeholders`` expands a list of ids into Oracle named binds because the thin
client has no array bind. It is pure, so every documented rule is exercised directly.

DIVERGENCE (brief premise vs code): duplicates are expanded rather than collapsed, and
the empty-input result is `("", {})` so each caller must short-circuit before building an
`IN (...)` clause.
"""

from __future__ import annotations

import pytest

from mnemos.persistence.oracle import _in_placeholders


def test_empty_list_returns_no_clause_and_no_params() -> None:
    assert _in_placeholders([]) == ("", {})


def test_empty_tuple_and_none_are_treated_as_empty() -> None:
    """DIVERGENCE: ``None`` is accepted although the annotation is ``Sequence[str]``."""
    assert _in_placeholders(()) == ("", {})
    assert _in_placeholders(None) == ("", {})


def test_single_id_uses_the_documented_default_prefix() -> None:
    assert _in_placeholders(["memory-1"]) == (":id0", {"id0": "memory-1"})


def test_n_ids_yield_n_ordered_binds() -> None:
    ids = ["a", "b", "c", "d"]

    placeholders, params = _in_placeholders(ids, "mid")

    assert placeholders == ":mid0,:mid1,:mid2,:mid3"
    assert params == {"mid0": "a", "mid1": "b", "mid2": "c", "mid3": "d"}


def test_clause_never_contains_spaces_or_parentheses() -> None:
    placeholders, _ = _in_placeholders(["a", "b"], "vid")

    assert " " not in placeholders
    assert "(" not in placeholders
    assert ")" not in placeholders


def test_params_holds_exactly_one_entry_per_bind() -> None:
    ids = [f"m{i}" for i in range(7)]

    placeholders, params = _in_placeholders(ids, "mid")

    assert len(params) == len(ids) == placeholders.count(":")
    assert list(params) == [f"mid{i}" for i in range(len(ids))]
    assert [params[f"mid{i}"] for i in range(len(ids))] == ids


def test_duplicate_ids_are_expanded_not_collapsed() -> None:
    """Documented rule: expand to named binds; de-duplication is the caller's job."""
    placeholders, params = _in_placeholders(["a", "a", "b"], "mid")

    assert placeholders == ":mid0,:mid1,:mid2"
    assert params == {"mid0": "a", "mid1": "a", "mid2": "b"}


def test_values_are_passed_through_without_coercion() -> None:
    """DIVERGENCE: no value type validation, despite the ``Sequence[str]`` annotation.

    Values outside the annotation are passed through unchanged; callers own
    validation. If a future version starts rejecting non-str values this test
    will fail by design — that would be a contract change, not a bug here.
    """
    _, params = _in_placeholders([1, "", None, "x"], "mid")

    assert params == {"mid0": 1, "mid1": "", "mid2": None, "mid3": "x"}


def test_prefix_is_concatenated_verbatim() -> None:
    assert _in_placeholders(["a"], "x_1") == (":x_10", {"x_10": "a"})
    # Footgun, pinned deliberately: an empty prefix yields bare-numeric binds
    # (":0"/{"0": ...}) — indistinguishable from positional-style binds and
    # collision-prone if two such clauses were ever merged. No caller does this
    # today; the test records the current verbatim behaviour, not an endorsement.
    assert _in_placeholders(["a"], "") == (":0", {"0": "a"})


def test_tuple_input_is_supported() -> None:
    assert _in_placeholders(("p", "q"), "vid") == (
        ":vid0,:vid1",
        {"vid0": "p", "vid1": "q"},
    )


def test_generator_input_raises_type_error() -> None:
    """DIVERGENCE: any truthy iterable passes the guard; only sequences work.

    The annotation is ``Sequence[str]`` and the implementation calls ``len()``,
    so a generator (truthy, but without ``__len__``) dies at that call with a
    ``TypeError``. This pins the *current* failure mode — it is not a documented
    contract, and a refactor that materialised the iterable (``ids = list(ids)``)
    would legitimately accept generators, so treat a future change here as a
    behaviour change to review, not a regression in this helper.
    """
    with pytest.raises(TypeError):
        _in_placeholders((value for value in ["a", "b"]), "mid")


def test_each_call_returns_fresh_objects() -> None:
    _, first = _in_placeholders([], "mid")
    _, second = _in_placeholders([], "mid")
    assert first is not second

    _, third = _in_placeholders(["a"], "mid")
    third["mid0"] = "mutated"
    _, fourth = _in_placeholders(["a"], "mid")
    assert fourth == {"mid0": "a"}
