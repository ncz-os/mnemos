"""ARTEMIS canonical dedup hashing (FB413, 2026-11-06).

Pins the content-addressing contract in ``mnemos/domain/artemis_dedup.py``:

* ``normalize_content_for_hash`` applies exactly ONE rule — newline style
  (CRLF and lone CR become LF) — and is idempotent. Case, spacing, tabs and
  Unicode composition are significant, contrary to a looser "whitespace/case
  normalisation" reading.
* ``content_sha256`` is the sha256 of the normalised UTF-8 bytes: 64 lowercase
  hex characters, equal for line-ending-only differences, different for any real
  content change.
* ``None`` and ``""`` address the same (empty) body.
* ``DedupDecision`` is a frozen value object with the documented defaults; its
  ``action`` Literal is typing-only and is not validated at runtime.

Pure functions: no settings, no database, no network.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys
from dataclasses import FrozenInstanceError

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mnemos.domain.artemis_dedup import (  # noqa: E402
    DedupDecision,
    content_sha256,
    normalize_content_for_hash,
)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ── the normalisation rule ─────────────────────────────────────────────────


def test_normalisation_unifies_every_newline_style():
    norm = normalize_content_for_hash
    assert norm("a\r\nb") == "a\nb"
    assert norm("a\rb") == "a\nb"
    assert norm("a\nb") == "a\nb"
    assert norm("a\r\nb\rc\n") == "a\nb\nc\n"
    # CRLF is replaced first, so "\r\r\n" is one CR plus one LF -- not two LFs.
    assert norm("a\r\r\nb") == "a\n\nb"
    assert norm("\r\n") == "\n"


def test_normalisation_is_idempotent():
    for content in ("", "a", "a\r\nb", "a\rb", "a\r\r\nb", "\r\n\r\n", "é\r\n🙂", "  a  b  "):
        once = normalize_content_for_hash(content)
        assert normalize_content_for_hash(once) == once, repr(content)


def test_normalisation_leaves_case_and_spacing_alone():
    norm = normalize_content_for_hash
    assert norm("Alpha") == "Alpha"
    assert norm("  a  b  ") == "  a  b  "
    assert norm("\t a\n") == "\t a\n"
    # ... so these are DIFFERENT memories, not one canonical body.
    assert norm("A") != norm("a")
    assert norm(" a") != norm("a")
    assert norm("a  b") != norm("a b")


def test_normalisation_does_not_compose_unicode():
    # NFC ("é") and decomposed ("e" + combining acute) stay distinct bodies.
    assert normalize_content_for_hash("e\u0301") != normalize_content_for_hash("\u00e9")


def test_none_and_empty_address_the_same_body():
    assert normalize_content_for_hash(None) == ""
    assert content_sha256(None) == content_sha256("") == EMPTY_SHA256


# ── the digest ─────────────────────────────────────────────────────────────


def test_digest_is_the_sha256_of_the_normalised_utf8_bytes():
    for content in ("", "same", "line1\r\nline2", "é🙂", "  A  "):
        expected = hashlib.sha256(normalize_content_for_hash(content).encode("utf-8")).hexdigest()
        assert content_sha256(content) == expected, repr(content)
    # Independent literals, so a broken normaliser cannot satisfy the check by
    # agreeing with itself: "\r\n" must hash as the two bytes "\n" and b"".
    assert content_sha256("\r\n") == hashlib.sha256(b"\n").hexdigest()
    assert content_sha256("line1\r\nline2") == hashlib.sha256(b"line1\nline2").hexdigest()


def test_digest_shape_is_lowercase_hex_of_64():
    digest = content_sha256("shape")
    assert len(digest) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_line_ending_only_differences_are_one_memory():
    variants = ("identical\r\ncontent", "identical\ncontent", "identical\rcontent")
    digests = {content_sha256(variant) for variant in variants}
    assert len(digests) == 1
    assert digests == {hashlib.sha256(b"identical\ncontent").hexdigest()}


def test_any_real_content_change_changes_the_digest():
    assert content_sha256("abc") != content_sha256("abd")
    assert content_sha256("Alpha") != content_sha256("alpha")
    assert content_sha256("a b") != content_sha256("a  b")
    assert content_sha256("a\nb") != content_sha256("a\n\nb")
    assert content_sha256("") != content_sha256(" ")


def test_non_ascii_content_hashes_as_utf8():
    assert content_sha256("é") == hashlib.sha256("é".encode()).hexdigest()
    assert content_sha256("é") != hashlib.sha256("é".encode("latin-1")).hexdigest()
    assert content_sha256("🙂") == hashlib.sha256("🙂".encode()).hexdigest()


# ── DedupDecision ──────────────────────────────────────────────────────────


def test_decision_defaults_and_value_equality():
    decision = DedupDecision(action="create", content_hash="0" * 64)
    assert (decision.action, decision.content_hash) == ("create", "0" * 64)
    assert decision.existing_id is None and decision.hint is None
    assert decision == DedupDecision("create", "0" * 64)
    assert decision == DedupDecision(action="create", content_hash="0" * 64, existing_id=None, hint=None)
    assert decision != DedupDecision("reject", "0" * 64)


def test_decision_is_frozen_and_hashable():
    decision = DedupDecision("reject", "a" * 64, existing_id="mem_1", hint="duplicate")
    with pytest.raises(FrozenInstanceError):
        decision.action = "create"
    assert hash(decision) == hash(DedupDecision("reject", "a" * 64, existing_id="mem_1", hint="duplicate"))
    assert len({decision, DedupDecision("reject", "a" * 64, existing_id="mem_1", hint="duplicate")}) == 1


def test_decision_action_is_not_validated_at_runtime():
    # DedupAction is a typing Literal; the dataclass performs no check, so an
    # out-of-vocabulary action is preserved verbatim (pinned as-is).
    odd = DedupDecision(action="bogus", content_hash="x")  # type: ignore[arg-type]
    assert odd.action == "bogus"
