"""APOLLO form detection (FB430, tests only).

Pins the two shape sniffs the narration dispatcher uses before it picks a
narrator. Divergences from the brief are pinned and reported, never endorsed;
source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo import looks_like_fallback, looks_like_portfolio

PORTFOLIO = "AAPL:100@150.25/175.50:tech"
FALLBACK = "summary=Bob shipped it;facts=[v1.2-shipped|CI-passed];entities=[Bob|CI];concepts=[release]"


@pytest.mark.parametrize(
    "encoded",
    [
        PORTFOLIO,
        "AAPL:100@150/175:tech",
        "A:1@2/3:u",
        "MSFT:50@300.5/310.25:tech;AAPL:100@150.25/175.50:tech",
        "GOOG:7@2800/2900:tech;",
    ],
)
def test_portfolio_forms_are_detected(encoded: str) -> None:
    assert looks_like_portfolio(encoded) is True


@pytest.mark.parametrize(
    "encoded",
    [
        "aapl:100@150.25/175.50:tech",
        "LONGER:1@2/3:tech",
        "AAPL:1@2/3:",
        "AAPL:1@2:x",
        "AAPL:1/2:tech",
        "the portfolio rose today",
        FALLBACK,
        "",
    ],
)
def test_non_portfolio_forms_are_rejected(encoded: str) -> None:
    assert looks_like_portfolio(encoded) is False


def test_none_is_rejected_despite_the_str_annotation() -> None:
    """DIVERGENCE pinned, not endorsed: the annotation says `str`, the falsy
    guard accepts None."""
    assert looks_like_portfolio(None) is False  # type: ignore[arg-type]
    assert looks_like_fallback(None) is False  # type: ignore[arg-type]


def test_only_the_first_position_is_validated() -> None:
    """DIVERGENCE pinned, not endorsed: the sniff checks the first `;` segment
    only, so a malformed later position (or a valid one behind a bad first
    segment) decides the answer on the wrong data."""
    assert looks_like_portfolio(f"{PORTFOLIO};not-a-position") is True
    assert looks_like_portfolio(f"not-a-position;{PORTFOLIO}") is False


def test_leading_whitespace_is_tolerated() -> None:
    assert looks_like_portfolio("   " + PORTFOLIO) is True


def test_trailing_junk_after_the_category_is_accepted() -> None:
    """DIVERGENCE pinned, not endorsed: the shape regex is not anchored at the
    end, so anything may follow the category."""
    assert looks_like_portfolio("AAPL:1@2/3:tech and then some words") is True


@pytest.mark.parametrize(
    "encoded",
    [
        FALLBACK,
        "summary=S|facts=[a]|entities=[b]|concepts=[c]",
        "summary=S;facts=[];entities=[];concepts=[]",
        "summary=;facts=[a];entities=[b];concepts=[c]",
        FALLBACK + "   \n",
    ],
)
def test_fallback_forms_are_detected(encoded: str) -> None:
    assert looks_like_fallback(encoded) is True


@pytest.mark.parametrize(
    "encoded",
    [
        "summary=S;facts=[a];entities=[b]",
        FALLBACK + " trailing",
        "SUMMARY=S;facts=[a];entities=[b];concepts=[c]",
        "summary=line one\nline two;facts=[a];entities=[b];concepts=[c]",
        PORTFOLIO,
        "",
    ],
)
def test_non_fallback_forms_are_rejected(encoded: str) -> None:
    assert looks_like_fallback(encoded) is False


def test_a_summary_may_absorb_separators() -> None:
    """DIVERGENCE pinned, not endorsed: the lazy summary capture backtracks, so
    a semicolon or pipe inside the summary is absorbed into the summary and the
    line still detects; only a line that cannot satisfy the anchored tail is
    rejected."""
    assert looks_like_fallback("summary=A; B;facts=[x];entities=[];concepts=[]") is True
    assert looks_like_fallback("summary=A | B;facts=[x];entities=[];concepts=[]") is True
    assert looks_like_fallback("summary=A;facts=[x];entities=[];concepts=[] trailing") is False


def test_the_two_predicates_are_mutually_exclusive() -> None:
    corpus = [
        PORTFOLIO,
        FALLBACK,
        "summary=S|facts=[a]|entities=[]|concepts=[]",
        "AAPL:1@2/3:x;junk",
        "",
        "plain prose",
    ]
    for encoded in corpus:
        assert not (looks_like_portfolio(encoded) and looks_like_fallback(encoded)), encoded


def test_empty_input_is_false_for_both() -> None:
    assert looks_like_portfolio("") is False
    assert looks_like_fallback("") is False
