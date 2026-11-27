"""ARTEMIS block and sentence splitting (FB428, tests only).

Pins the labeled-block extractor and the two sentence tokenizers, including
the offsets the compressor anchors to. Divergences from the brief are pinned
and reported, never endorsed; source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.domain.compression.artemis import (
    _extract_labeled_blocks,
    _split_sentences,
    _split_sentences_with_spans,
)

TWO_LINES = "Name: Ada\nRole: engineer"
THREE_LINES = "Name: Ada\nRole: engineer\nTeam: core"


def test_two_consecutive_labeled_lines_are_one_block() -> None:
    assert _extract_labeled_blocks(TWO_LINES) == [TWO_LINES]


def test_three_consecutive_labeled_lines_are_still_one_block() -> None:
    assert _extract_labeled_blocks(THREE_LINES) == [THREE_LINES]


def test_a_blank_line_ends_a_run() -> None:
    text = "Name: Ada\nRole: eng\n\nName: Bob\nRole: ops"
    assert _extract_labeled_blocks(text) == ["Name: Ada\nRole: eng", "Name: Bob\nRole: ops"]


def test_prose_between_runs_ends_a_run() -> None:
    text = "Name: Ada\nRole: eng\nSome prose here.\nName: Bob\nRole: ops"
    assert _extract_labeled_blocks(text) == ["Name: Ada\nRole: eng", "Name: Bob\nRole: ops"]


def test_blocks_come_back_in_document_order() -> None:
    text = "Aa: one\nBb: two\nprose\nCc: three\nDd: four"
    assert _extract_labeled_blocks(text) == ["Aa: one\nBb: two", "Cc: three\nDd: four"]


def test_a_single_labeled_line_is_not_a_block() -> None:
    """The docstring's own example: prose that looks labeled stays prose."""
    assert _extract_labeled_blocks("The issue: X was broken") == []
    assert _extract_labeled_blocks("Name: Ada") == []


def test_empty_input_has_no_blocks() -> None:
    assert _extract_labeled_blocks("") == []
    assert _extract_labeled_blocks("\n\n") == []


def test_key_charset_and_syntax_requirements() -> None:
    assert _extract_labeled_blocks("X: one\nY: two") == [], "a one-character key is not a label"
    assert _extract_labeled_blocks("Key_1: one\nKey_2: two") == ["Key_1: one\nKey_2: two"], (
        "underscore is inside the key class"
    )
    assert _extract_labeled_blocks("Key.1: one\nKey.2: two") == [], "a dot is outside the key class"
    assert _extract_labeled_blocks("Name:Ada\nRole:eng") == [], "a space after the colon is required"
    assert _extract_labeled_blocks("  Name: Ada\n  Role: eng") == [], "an indented line is not a label"


def test_the_block_is_verbatim_including_trailing_spaces() -> None:
    text = "Name: Ada  \nRole: eng  "
    assert _extract_labeled_blocks(text) == [text]


def test_crlf_input_keeps_the_carriage_returns() -> None:
    """DIVERGENCE pinned, not endorsed: the split is on "\\n" only, so CRLF
    input carries "\\r" into the preserved block."""
    text = "Name: Ada\r\nRole: eng\r"
    block = _extract_labeled_blocks(text)[0]
    assert "\r" in block
    assert block == "Name: Ada\r\nRole: eng\r"


def test_two_sentences_split_on_a_capital_after_the_period() -> None:
    assert _split_sentences("Alpha beta. Gamma delta.") == ["Alpha beta.", "Gamma delta."]


def test_an_abbreviation_splits_and_its_fragment_is_dropped() -> None:
    """DIVERGENCE pinned, not endorsed: there is no abbreviation list, so "Dr."
    is read as a sentence and then discarded for being three characters."""
    assert _split_sentences("Dr. Smith left. He waved.") == ["Smith left.", "He waved."]


def test_a_decimal_does_not_split() -> None:
    assert _split_sentences("Pi is 3.14 exactly.") == ["Pi is 3.14 exactly."]


def test_a_lowercase_continuation_does_not_split() -> None:
    assert _split_sentences("One. two") == ["One. two"]


def test_a_short_sentence_is_dropped() -> None:
    assert _split_sentences("Hi. Go now.") == ["Go now."]


def test_a_paragraph_break_splits() -> None:
    assert _split_sentences("Alpha beta.\n\nGamma delta.") == ["Alpha beta.", "Gamma delta."]


def test_internal_whitespace_is_collapsed() -> None:
    assert _split_sentences("Alpha   beta\tgamma.") == ["Alpha beta gamma."]


def test_spans_are_ascending_and_slices_strip_back_to_the_sentence() -> None:
    text = "Alpha beta.\n\nGamma delta. Epsilon zeta."
    spans = _split_sentences_with_spans(text)
    assert [s for s, _, _ in spans] == ["Alpha beta.", "Gamma delta.", "Epsilon zeta."]
    assert [start for _, start, _ in spans] == sorted(start for _, start, _ in spans)
    for sentence, start, end in spans:
        assert text[start:end].strip() == sentence


def test_the_gaps_between_spans_are_whitespace_only() -> None:
    text = "Alpha beta. Gamma delta. Epsilon zeta."
    spans = _split_sentences_with_spans(text)
    cursor = 0
    for _, start, end in spans:
        assert not text[cursor:start].strip(), "only separators may sit between spans"
        cursor = end
    assert not text[cursor:].strip()


def test_a_dropped_fragment_leaves_no_span() -> None:
    """A three-character first sentence is normalised away, so the first span
    starts after it; the text it came from remains in the gap."""
    text = "Hi. There is more."
    spans = _split_sentences_with_spans(text)
    assert spans == [("There is more.", 4, len(text))]


def test_the_tail_after_the_last_boundary_is_kept() -> None:
    text = "Alpha beta. Gamma delta"
    spans = _split_sentences_with_spans(text)
    assert spans[-1][0] == "Gamma delta"
    assert spans[-1][2] == len(text)


@pytest.mark.parametrize("text", ["", "   ", "\n\n"], ids=repr)
def test_blank_input_has_no_sentences(text: str) -> None:
    assert _split_sentences(text) == []
    assert _split_sentences_with_spans(text) == []


def test_splitting_is_deterministic() -> None:
    text = "Alpha beta. Gamma delta."
    assert _split_sentences(text) == _split_sentences(text)
    assert _split_sentences_with_spans(text) == _split_sentences_with_spans(text)
