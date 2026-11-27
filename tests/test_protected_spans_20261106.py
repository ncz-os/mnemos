"""ARTEMIS protected-span detection (FB427, tests only).

Pins the categories `_find_protected_spans` protects, the de-duplication order,
and the offsets the compressor uses to rebuild sentences. Divergences from the
brief are pinned and reported, never endorsed; source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.domain.compression.artemis import _find_protected_spans

CATEGORIES = {
    "version",
    "iso_date",
    "slash_date",
    "email",
    "url",
    "uuid",
    "sha",
    "path",
    "ticker",
    "id_code",
    "phone",
    "percent",
    "currency",
    "quoted",
    "number",
}


def test_empty_input_has_no_spans() -> None:
    assert _find_protected_spans("") == []


def test_plain_prose_has_no_spans() -> None:
    assert _find_protected_spans("the quick brown fox jumps over the lazy dog") == []
    assert _find_protected_spans("a short sentence with words only") == []


@pytest.mark.parametrize(
    "text,token,kind",
    [
        ("release v1.2.3 today", "v1.2.3", "version"),
        ("ship on 2026-11-06 please", "2026-11-06", "iso_date"),
        ("due 3/14/2026 sharp", "3/14/2026", "slash_date"),
        ("mail ops@example.com now", "ops@example.com", "email"),
        ("see https://example.com/a?b=1 now", "https://example.com/a?b=1", "url"),
        (
            "job 01a1b2b0-efc8-7e54-bb91-fdb73b519aea done",
            "01a1b2b0-efc8-7e54-bb91-fdb73b519aea",
            "uuid",
        ),
        ("commit ccd68618ea711afd landed", "ccd68618ea711afd", "sha"),
        ("read docs/guide.md first", "docs/guide.md", "path"),
        ("ticket INC-1234 filed", "INC-1234", "id_code"),
        ("call +1 (555) 123-4567 now", "1 (555) 123-4567", "phone"),
        ("up 42% today", "42%", "percent"),
        ("costs $1,234.56 total", "$1,234.56", "currency"),
        ('say "hello there" aloud', '"hello there"', "quoted"),
        ("order 12345 units", "12345", "number"),
    ],
)
def test_one_exact_span_per_documented_category(text: str, token: str, kind: str) -> None:
    start = text.index(token)
    assert _find_protected_spans(text) == [(start, start + len(token), kind)]


def test_small_numbers_and_single_character_quotes_are_not_protected() -> None:
    assert _find_protected_spans("only 12 units left") == []
    assert _find_protected_spans('he said "a" loudly') == []
    assert _find_protected_spans("v1 is not a version") == []


def test_ticker_blocklist_and_length_bounds() -> None:
    assert _find_protected_spans("THE API and TODO") == []
    assert _find_protected_spans("acme CORP rose") == [(5, 9, "ticker")]
    assert _find_protected_spans("LONGWORD is too long") == []


def test_a_seven_digit_number_is_a_number_but_eight_digits_are_a_sha() -> None:
    """The `isdigit()` guard drops the sha pattern below 8 digits, so the looser
    number pattern supplies the span instead."""
    assert _find_protected_spans("run 1234567 times") == [(4, 11, "number")]
    assert _find_protected_spans("run 12345678 times") == [(4, 12, "sha")]


def test_same_start_keeps_the_longest_not_the_first_registered_kind() -> None:
    """DIVERGENCE pinned, not endorsed: the docstring says the first-registered
    kind wins an overlap, but the sort keeps the longest span at the earliest
    start, so the later-registered id_code beats the ticker on INC-1234."""
    assert _find_protected_spans("ticket INC-1234 filed") == [(7, 15, "id_code")]


def test_a_url_absorbs_the_version_inside_it() -> None:
    assert _find_protected_spans("see https://x.dev/v1.2.3 now") == [(4, 24, "url")]


def test_a_leading_plus_is_outside_the_phone_span() -> None:
    """DIVERGENCE pinned, not endorsed.

    ``\\b`` cannot sit between a space and ``+``, so the country-code plus sign
    is left out of the protected span while the digits are kept.
    """
    text = "call +1 (555) 123-4567 now"
    spans = _find_protected_spans(text)
    assert spans == [(6, 22, "phone")]
    assert spans[0][0] > text.index("+")
    assert text[spans[0][0] : spans[0][1]] == "1 (555) 123-4567"


def test_a_fenced_block_is_not_protected_as_a_unit() -> None:
    """DIVERGENCE pinned, not endorsed: there is no fenced-block category, so the
    fence lines are ordinary text and only the number inside is protected."""
    spans = _find_protected_spans("```python\nprint(1234)\n```")
    assert spans == [(16, 20, "number")]


def test_a_path_shaped_word_pair_is_protected() -> None:
    """DIVERGENCE pinned, not endorsed: any `a/b` matches the path pattern, so
    ordinary prose is protected too."""
    assert _find_protected_spans("input/output ratio") == [(0, 12, "path")]


def test_a_url_span_includes_trailing_sentence_punctuation() -> None:
    """DIVERGENCE pinned, not endorsed: the url pattern runs to the next
    whitespace, so a sentence-final period becomes part of the protected span."""
    spans = _find_protected_spans("see https://x.dev/a.")
    assert spans == [(4, 20, "url")]
    assert spans[0][1] - spans[0][0] == len("https://x.dev/a.")


def test_spans_are_ascending_non_overlapping_and_slice_back() -> None:
    text = (
        "Deploy v1.2.3 to https://example.com/api on 2026-11-06, "
        'costs $1,234.56 (up 42%), job 01a1b2b0-efc8-7e54-bb91-fdb73b519aea, '
        "commit ccd68618ea711afd, ticket INC-1234, mail ops@example.com."
    )
    spans = _find_protected_spans(text)
    assert spans, "the mixed paragraph must protect something"
    assert [s[0] for s in spans] == sorted(s[0] for s in spans)
    for (start, end, kind), nxt in zip(spans, spans[1:]):
        assert start < end
        assert kind in CATEGORIES
        assert end <= nxt[0], "spans must not overlap"
        assert text[start:end].strip(), "a span must slice back to real text"
    *_, last = spans
    assert text[last[0] : last[1]].strip()


def test_detection_is_deterministic() -> None:
    text = "v1.2.3 at 2026-11-06 via https://x.dev/a and INC-1234"
    assert _find_protected_spans(text) == _find_protected_spans(text)
