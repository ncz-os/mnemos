"""Characterisation: `metadata_json_path` (FB787).

The helper returns the quoted JSON path to one top-level metadata field. That
path is the only thing ever concatenated into a statement, so its safety rests
entirely on the key pattern: quotes and backslashes are not escaped, they are
refused.
"""

from __future__ import annotations

import re

import pytest

from mnemos.persistence import types

PATTERN = types.METADATA_EQ_KEY_PATTERN


def test_a_plain_key_becomes_a_quoted_path():
    assert types.metadata_json_path("job_id") == '$."job_id"'


def test_the_form_is_the_root_then_the_quoted_key():
    for key in ("a", "A", "_a", "k1", "a" * 64):
        path = types.metadata_json_path(key)
        assert path.startswith('$."')
        assert path.endswith('"')
        assert path == '$."' + key + '"'


def test_underscores_digits_and_hyphens_survive_inside_the_quotes():
    assert types.metadata_json_path("run-2_id") == '$."run-2_id"'


def test_a_dotted_key_is_one_literal_field_name():
    """The dot is legal inside a key, so it stays inside the quotes — the path is a
    single top-level field named `a.b`, not the nested path `$.a.b`."""
    assert types.metadata_json_path("a.b") == '$."a.b"'
    assert types.metadata_json_path("a.b") != "$.a.b"


def test_the_longest_accepted_key_is_accepted():
    key = "a" * 64
    assert types.metadata_json_path(key) == '$."' + key + '"'


@pytest.mark.parametrize(
    "key",
    [
        "",
        "a" * 65,
        "1a",
        ".a",
        "-a",
        "job id",
        "naïve",
        "a\nb",
        "abc\n",
    ],
)
def test_keys_outside_the_pattern_are_refused(key):
    with pytest.raises(ValueError, match="must match"):
        types.metadata_json_path(key)


@pytest.mark.parametrize("key", [None, 1, b"job_id", True, ["job_id"]])
def test_a_non_string_key_is_refused(key):
    with pytest.raises(ValueError, match="must match"):
        types.metadata_json_path(key)


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        types.metadata_json_path("")


@pytest.mark.parametrize("key", ['a"b', 'a"b"c', '""'])
def test_a_key_carrying_a_quote_is_refused(key):
    """DIVERGENCE: the brief expects special characters to be escaped per a
    documented rule. Nothing is escaped and no escaping rule exists — a quote in
    the key is a refusal, which is also why the returned path can never contain
    an unescaped quote from the key."""
    with pytest.raises(ValueError, match="must match"):
        types.metadata_json_path(key)


@pytest.mark.parametrize("key", ["a\\b", "a\\", "\\"])
def test_a_key_carrying_a_backslash_is_refused(key):
    with pytest.raises(ValueError, match="must match"):
        types.metadata_json_path(key)


@pytest.mark.parametrize(
    "key",
    ["a b", "a$b", "a[b]", "a]b", "a#b", "a->b", "a->>b", "a.b[0]", "a'b", "a%b", "a;b"],
)
def test_path_operator_syntax_is_refused(key):
    with pytest.raises(ValueError, match="must match"):
        types.metadata_json_path(key)


@pytest.mark.parametrize("key", ["job_id", "a.b", "run-2", "a" * 64, "__proto__"])
def test_an_accepted_key_yields_no_operator_syntax_or_stray_quote(key):
    path = types.metadata_json_path(key)
    assert path.count('"') == 2
    for token in ("->", ">>", "[", "]", "#", "'", "\\"):
        assert token not in path


def test_the_refusal_names_the_pattern():
    with pytest.raises(ValueError, match=re.escape(PATTERN)):
        types.metadata_json_path("bad key")


def test_the_path_is_stable_across_calls():
    assert types.metadata_json_path("job_id") == types.metadata_json_path("job_id")


def test_the_key_pattern_is_the_shared_filter_pattern():
    """The path helper and the filter validator accept exactly the same keys, which
    is what lets the filter build a path per key it has already accepted."""
    for key in ("job_id", "a.b", "a" * 64):
        assert types.metadata_json_path(key) == '$."' + key + '"'
        assert types.normalize_metadata_eq({key: "v"}) == {key: "v"}
