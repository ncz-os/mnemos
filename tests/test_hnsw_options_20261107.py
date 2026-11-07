"""Characterisation: `hnsw_index_options_fragment` (FB791).

The helper exists for one reason: the bare `WITH (...)` clause has no leading
space, so a DDL site that concatenates it straight after `vector_cosine_ops)`
emits `vector_cosine_ops)WITH (...)`, which Postgres rejects. The fragment
carries the separator so a call site cannot get the spacing wrong.
"""

from __future__ import annotations

import inspect
import re

from mnemos.persistence import vector_tuning

fragment = vector_tuning.hnsw_index_options_fragment


def test_the_fragment_is_the_documented_clause():
    assert fragment() == " WITH (m = 16, ef_construction = 64)"


def test_the_fragment_is_the_bare_constant_with_one_leading_space():
    assert fragment() == " " + vector_tuning.HNSW_INDEX_OPTIONS_SQL


def test_the_bare_constant_has_no_leading_space():
    """The defect the helper exists to prevent."""
    assert not vector_tuning.HNSW_INDEX_OPTIONS_SQL.startswith(" ")
    assert f"vector_cosine_ops){vector_tuning.HNSW_INDEX_OPTIONS_SQL}" == (
        "vector_cosine_ops)WITH (m = 16, ef_construction = 64)"
    )


def test_the_fragment_separates_a_concatenated_call_site():
    rendered = f"CREATE INDEX i ON t USING hnsw (embedding vector_cosine_ops){fragment()}"
    assert "vector_cosine_ops) WITH (m = 16" in rendered
    assert ")WITH" not in rendered


def test_the_options_come_from_the_module_constants():
    text = fragment()
    assert f"m = {vector_tuning.HNSW_INDEX_M}" in text
    assert f"ef_construction = {vector_tuning.HNSW_INDEX_EF_CONSTRUCTION}" in text


def test_the_documented_build_parameters_are_pgvector_defaults():
    assert vector_tuning.HNSW_INDEX_M == 16
    assert vector_tuning.HNSW_INDEX_EF_CONSTRUCTION == 64


def test_the_fragment_has_exactly_one_leading_space_and_no_trailing_space():
    text = fragment()
    assert text.startswith(" WITH")
    assert text == text.rstrip()
    assert text.lstrip() == text[1:]
    assert not text.startswith("  ")


def test_the_fragment_is_a_single_line():
    assert "\n" not in fragment()


def test_the_fragment_is_stable_across_calls():
    assert fragment() == fragment()
    assert {fragment() for _ in range(5)} == {" WITH (m = 16, ef_construction = 64)"}


def test_the_fragment_takes_no_arguments():
    """DIVERGENCE: the brief expects an unset option to be omitted rather than
    defaulted silently. The helper has no parameters and no option set to pass, so
    both build parameters are always emitted from module constants and there is no
    omission path to characterise."""
    assert list(inspect.signature(fragment).parameters) == []
    text = fragment()
    assert "m =" in text
    assert "ef_construction =" in text


def test_the_fragment_is_one_postgres_options_clause_with_no_dialect_switch():
    """DIVERGENCE: the brief expects a "documented dialect form". There is no dialect
    parameter and only one form — Postgres index-option syntax — while the module
    records that Oracle has no vector index DDL at all."""
    assert list(inspect.signature(fragment).parameters) == []
    assert vector_tuning.ORACLE_HAS_VECTOR_INDEX_DDL is False
    assert "USING" not in fragment()
    assert "CREATE" not in fragment()


def test_the_clause_has_the_parser_shape_of_a_parenthesised_option_list():
    text = fragment().strip()
    assert re.fullmatch(r"WITH \(m = \d+, ef_construction = \d+\)", text)
    assert text.count("(") == 1
    assert text.count(")") == 1
    assert text.endswith(")")
    assert not text.endswith(",")


def test_the_fragment_interpolates_numbers_not_placeholders():
    text = fragment()
    for token in ("?", "%s", "{}", "$1", ":"):
        assert token not in text


def test_the_fragment_mentions_only_the_two_build_parameters():
    names = re.findall(r"([a-z_]+) = ", fragment())
    assert names == ["m", "ef_construction"]


def test_both_helpers_are_exported():
    assert "hnsw_index_options_fragment" in vector_tuning.__all__
    assert "HNSW_INDEX_OPTIONS_SQL" in vector_tuning.__all__


def test_calling_the_fragment_leaves_the_constants_untouched():
    before = (
        vector_tuning.HNSW_INDEX_OPTIONS_SQL,
        vector_tuning.HNSW_INDEX_M,
        vector_tuning.HNSW_INDEX_EF_CONSTRUCTION,
    )
    fragment()
    assert before == (
        vector_tuning.HNSW_INDEX_OPTIONS_SQL,
        vector_tuning.HNSW_INDEX_M,
        vector_tuning.HNSW_INDEX_EF_CONSTRUCTION,
    )
