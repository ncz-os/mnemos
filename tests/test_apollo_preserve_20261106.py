"""APOLLO must-preserve block and fallback normalisation (FB432, tests only).

Pins the prompt block built from ARTEMIS protected spans and the line scan
that sanitises an LLM fallback answer. Source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo import _build_must_preserve_block, _normalize_fallback_output

HEADER = "MUST PRESERVE — these specific tokens from the memory MUST appear verbatim in your output:"
VALID = "summary=Bob shipped it;facts=[v1.2-shipped|CI-passed];entities=[Bob|CI];concepts=[release]"


def _expected(*tokens: str) -> str:
    listed = "\n".join(f"  - {token}" for token in tokens)
    return f"\n{HEADER}\n{listed}\n"


def test_one_protected_token_is_listed_exactly() -> None:
    assert _build_must_preserve_block("see https://x.dev/a now") == _expected("https://x.dev/a")


def test_the_block_starts_with_a_newline_and_ends_with_one() -> None:
    block = _build_must_preserve_block("see https://x.dev/a now")
    assert block.startswith("\n" + HEADER)
    assert block.endswith("\n")
    assert "verbatim" in block


@pytest.mark.parametrize("content", ["", "   ", "plain prose with no identifiers"], ids=repr)
def test_content_without_identifiers_produces_no_block(content: str) -> None:
    assert _build_must_preserve_block(content) == ""


def test_identical_tokens_are_listed_once() -> None:
    assert _build_must_preserve_block("v1.2.3 and v1.2.3 again") == _expected("v1.2.3")


def test_source_order_is_kept_while_deduplicating() -> None:
    content = "v9.9.9 then v1.2.3 then v9.9.9"
    assert _build_must_preserve_block(content) == _expected("v9.9.9", "v1.2.3")


def test_mixed_identifier_kinds_keep_their_order() -> None:
    content = "on 2026-11-06 ticket INC-1234 mailed ops@example.com"
    assert _build_must_preserve_block(content) == _expected("2026-11-06", "INC-1234", "ops@example.com")


def test_the_identifier_list_is_capped_at_twenty() -> None:
    content = " ".join(str(1000 + index) for index in range(25))
    block = _build_must_preserve_block(content)
    assert block.count("\n  - ") == 20
    assert "  - 1000\n" in block
    assert "  - 1019\n" in block
    assert "  - 1020" not in block


def test_identifiers_after_the_four_thousandth_character_are_not_listed() -> None:
    content = "v1.2.3 " + ("x" * 4000) + " v9.9.9"
    block = _build_must_preserve_block(content)
    assert "  - v1.2.3\n" in block
    assert "v9.9.9" not in block


@pytest.mark.parametrize("raw", [None, ""], ids=["none", "empty"])
def test_missing_fallback_output_answers_none(raw: object) -> None:
    assert _normalize_fallback_output(raw) is None  # type: ignore[arg-type]


def test_a_canonical_line_is_returned_unchanged() -> None:
    assert _normalize_fallback_output(VALID) == VALID


def test_surrounding_whitespace_is_stripped() -> None:
    assert _normalize_fallback_output("   " + VALID + "   ") == VALID


def test_preamble_and_suffix_lines_are_tolerated() -> None:
    raw = f"Here is the encoding:\n{VALID}\nHope that helps."
    assert _normalize_fallback_output(raw) == VALID


def test_the_first_matching_line_wins() -> None:
    second = "summary=Other;facts=[x];entities=[];concepts=[]"
    raw = f"{VALID}\n{second}"
    assert _normalize_fallback_output(raw) == VALID


@pytest.mark.parametrize(
    "raw",
    [
        "plain prose only",
        "summary=S;facts=[a];entities=[]",
        "summary=S;facts=[a];entities=[];concepts=[] and more",
        "SUMMARY=S;facts=[a];entities=[];concepts=[]",
    ],
)
def test_lines_without_the_full_shape_answer_none(raw: str) -> None:
    assert _normalize_fallback_output(raw) is None


def test_crlf_line_endings_leave_no_carriage_return() -> None:
    assert _normalize_fallback_output(VALID + "\r\n") == VALID
    assert "\r" not in _normalize_fallback_output(f"preamble\r\n{VALID}\r\n")


def test_normalisation_is_idempotent() -> None:
    once = _normalize_fallback_output(f"preamble\n  {VALID}  \n")
    assert once == VALID
    assert _normalize_fallback_output(once) == once


def test_an_indented_line_behind_blank_lines_is_found() -> None:
    raw = f"\n\n\t {VALID} \n\n"
    assert _normalize_fallback_output(raw) == VALID
