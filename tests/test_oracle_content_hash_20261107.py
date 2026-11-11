"""Characterisation: the Oracle content hash (FB968)."""

from __future__ import annotations

import hashlib

import pytest

from mnemos.persistence.oracle import _content_hash

EMPTY = hashlib.sha256(b"").hexdigest()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_identical_content_hashes_identically():
    assert _content_hash("hello") == _content_hash("hello")
    assert _content_hash("hello") == sha256("hello")


def test_the_digest_is_sha256_hex():
    digest = _content_hash("hello")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert int(digest, 16) >= 0


def test_none_and_empty_share_the_empty_digest():
    assert _content_hash(None) == EMPTY
    assert _content_hash("") == EMPTY


def test_a_one_character_change_changes_the_digest():
    assert _content_hash("hello") != _content_hash("hellp")


def test_line_endings_are_normalised():
    assert _content_hash("a\r\nb") == sha256("a\nb")
    assert _content_hash("a\rb") == sha256("a\nb")
    assert _content_hash("a\nb") == sha256("a\nb")


def test_crlf_is_replaced_before_a_lone_cr():
    assert _content_hash("a\r\r\nb") == sha256("a\n\nb")
    assert _content_hash("a\r\n\r\nb") == sha256("a\n\nb")


def test_values_are_stringified():
    assert _content_hash(123) == sha256("123")
    assert _content_hash(True) == sha256("True")
    assert _content_hash(["a", "b"]) == sha256("['a', 'b']")
    assert _content_hash({"k": 1}) == sha256("{'k': 1}")


def test_bytes_hash_as_their_repr():
    assert _content_hash(b"abc") == sha256("b'abc'")
    assert _content_hash(b"abc") == _content_hash("b'abc'")


def test_a_str_method_is_used():
    class Doc:
        def __str__(self) -> str:
            return "line1\r\nline2"

    assert _content_hash(Doc()) == sha256("line1\nline2")


def test_unicode_is_encoded_as_utf8():
    assert _content_hash("héllo") == sha256("héllo")
    assert _content_hash("héllo") != _content_hash("hello")


def test_falsy_values_still_stringify():
    assert _content_hash(0) == sha256("0")
    assert _content_hash(False) == sha256("False")
    assert _content_hash([]) == sha256("[]")
    assert _content_hash(()) == sha256("()")


def test_whitespace_is_not_stripped():
    assert _content_hash("a ") != _content_hash("a")
    assert _content_hash(" a") != _content_hash("a")
    assert _content_hash("a\n") != _content_hash("a")


def test_long_content_is_hashed_whole():
    body = "x" * 100_000
    assert _content_hash(body) == sha256(body)


def test_the_sqlite_sibling_agrees_on_text():
    sqlite = pytest.importorskip("mnemos.persistence.sqlite")
    for value in ("hello", "", None, "a\r\nb", "héllo", " a "):
        assert sqlite._content_hash_for_sqlite(value) == _content_hash(value)


def test_the_mysql_sibling_agrees_on_the_same_input():
    mysql = pytest.importorskip("mnemos.persistence.mysql")
    for value in ("hello", "", None, "a\r\nb", 123, 0, False, [], {"k": 1}):
        assert mysql._content_hash(value) == _content_hash(value)


def test_the_sqlite_sibling_diverges_on_falsy_non_strings():
    sqlite = pytest.importorskip("mnemos.persistence.sqlite")
    assert _content_hash(0) == sha256("0")
    assert sqlite._content_hash_for_sqlite(0) == EMPTY
    assert sqlite._content_hash_for_sqlite(False) == EMPTY
    assert sqlite._content_hash_for_sqlite([]) == EMPTY
