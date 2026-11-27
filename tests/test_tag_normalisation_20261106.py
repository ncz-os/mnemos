"""FB443 — memory tag normalisation for ``mnemos.domain.models``.

Pins :func:`mnemos.domain.models._normalize_memory_tags` on ``master``
(``22a3f7d8``): ``None`` stays ``None``, tags are stripped and de-duplicated in
first-appearance order, blanks and over-long tags are refused, and the input is
never mutated.

Pinned divergences, not endorsed: there is no case folding, and there is no
``isinstance(value, list)`` guard — a bare string is iterated into characters,
an ``int`` raises ``TypeError``, and a non-string element raises
``AttributeError`` rather than ``ValueError``.

Pure-function tests: nothing is read, written, or sent.
"""

from __future__ import annotations

import pytest

from mnemos.domain.models import MAX_TAG_LENGTH, _normalize_memory_tags


def test_none_stays_none():
    assert _normalize_memory_tags(None) is None


def test_empty_list_stays_empty():
    assert _normalize_memory_tags([]) == []


def test_single_tag_is_returned_unchanged():
    assert _normalize_memory_tags(["hive"]) == ["hive"]


def test_surrounding_whitespace_is_stripped():
    assert _normalize_memory_tags(["  hive  ", "\trepos\n"]) == ["hive", "repos"]


def test_inner_whitespace_is_preserved():
    assert _normalize_memory_tags(["two words"]) == ["two words"]


def test_duplicates_collapse_preserving_first_order():
    assert _normalize_memory_tags(["b", "a", "b", "a", "c"]) == ["b", "a", "c"]


def test_duplicates_that_only_match_after_stripping_collapse():
    assert _normalize_memory_tags([" hive ", "hive", " other "]) == ["hive", "other"]


def test_case_is_preserved_and_not_folded():
    """Pinned divergence: normalisation is strip-only, so case variants stay distinct."""
    assert _normalize_memory_tags(["Hive", "hive", "HIVE"]) == ["Hive", "hive", "HIVE"]


def test_blank_tag_is_refused():
    with pytest.raises(ValueError, match="blank"):
        _normalize_memory_tags([""])


def test_whitespace_only_tag_is_refused():
    with pytest.raises(ValueError, match="blank"):
        _normalize_memory_tags(["   "])


def test_one_blank_tag_rejects_the_whole_list():
    with pytest.raises(ValueError, match="blank"):
        _normalize_memory_tags(["ok", "", "also-ok"])


def test_tag_at_the_length_limit_is_accepted():
    tag = "x" * MAX_TAG_LENGTH
    assert _normalize_memory_tags([tag]) == [tag]


def test_tag_over_the_length_limit_is_refused():
    with pytest.raises(ValueError, match=str(MAX_TAG_LENGTH)):
        _normalize_memory_tags(["x" * (MAX_TAG_LENGTH + 1)])


def test_length_is_measured_after_stripping():
    tag = "  " + "y" * MAX_TAG_LENGTH + "  "
    assert _normalize_memory_tags([tag]) == ["y" * MAX_TAG_LENGTH]


def test_length_limit_counts_characters_not_bytes():
    tag = "é" * MAX_TAG_LENGTH  # 510 UTF-8 bytes
    assert _normalize_memory_tags([tag]) == [tag]
    with pytest.raises(ValueError):
        _normalize_memory_tags(["é" * (MAX_TAG_LENGTH + 1)])


def test_string_input_is_iterated_into_characters():
    """Pinned divergence: the annotation says List[str] but nothing enforces it."""
    assert _normalize_memory_tags("ab") == ["a", "b"]


def test_empty_string_input_returns_an_empty_list():
    assert _normalize_memory_tags("") == []


def test_integer_input_raises_type_error():
    with pytest.raises(TypeError):
        _normalize_memory_tags(5)


def test_non_string_element_raises_attribute_error():
    with pytest.raises(AttributeError):
        _normalize_memory_tags(["ok", None])


def test_tuple_input_is_accepted():
    assert _normalize_memory_tags(("a", "b")) == ["a", "b"]


def test_dict_input_iterates_its_keys():
    assert _normalize_memory_tags({"a": 1, "b": 2}) == ["a", "b"]


def test_input_list_is_not_mutated():
    tags = [" b ", "a", "b"]
    original = list(tags)
    _normalize_memory_tags(tags)
    assert tags == original


def test_result_is_a_fresh_list_for_a_single_tag():
    tags = ["hive"]
    result = _normalize_memory_tags(tags)
    assert result == ["hive"]
    result.append("other")
    assert tags == ["hive"]
