"""APOLLO narration (FB431, tests only).

Pins the narration dispatcher, the fallback narrator's prose shape and the
portfolio expansion it routes to. Divergences from the brief are pinned and
reported, never endorsed; source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo import _narrate_fallback_form, narrate_encoded
from mnemos.domain.compression.apollo_schemas import (
    CodeSchema,
    CommitSchema,
    DecisionSchema,
    EventSchema,
    PersonSchema,
    PortfolioSchema,
)

PORTFOLIO = "AAPL:100@150.25/175.50:tech"
FALLBACK = "summary=Bob shipped it;facts=[v1.2-shipped|CI-passed];entities=[Bob|CI];concepts=[release]"


@pytest.mark.parametrize("value", [None, ""], ids=["none", "empty"])
def test_missing_input_answers_the_empty_placeholder(value: object) -> None:
    assert narrate_encoded(value) == ""  # type: ignore[arg-type]


def test_a_portfolio_expands_to_one_sentence_per_position() -> None:
    assert narrate_encoded(PORTFOLIO) == (
        "AAPL (tech): 100 shares at basis $150.25, currently $175.50 (up $25.25)."
    )


def test_an_unclassified_position_omits_the_category_clause() -> None:
    assert narrate_encoded("AAPL:100@150/175:unclassified") == (
        "AAPL: 100 shares at basis $150, currently $175 (up $25.00)."
    )


def test_a_falling_position_reads_as_down() -> None:
    assert narrate_encoded("AAPL:10@200/150:tech") == (
        "AAPL (tech): 10 shares at basis $200, currently $150 (down $50.00)."
    )


def test_several_positions_keep_their_order() -> None:
    encoded = "AAPL:100@150.25/175.50:tech;MSFT:50@300/310:tech"
    assert narrate_encoded(encoded) == (
        "AAPL (tech): 100 shares at basis $150.25, currently $175.50 (up $25.25). "
        "MSFT (tech): 50 shares at basis $300, currently $310 (up $10.00)."
    )


def test_leading_whitespace_before_a_portfolio_is_ignored() -> None:
    assert narrate_encoded("   " + PORTFOLIO) == narrate_encoded(PORTFOLIO)


def test_an_unparseable_later_position_is_bracketed_not_dropped() -> None:
    """The sniff validated the first position only; the narrator brackets the
    piece it cannot parse instead of losing it."""
    out = narrate_encoded(f"{PORTFOLIO};garbage")
    assert out.startswith("AAPL (tech):")
    assert "[unparseable: garbage]" in out


def test_a_fallback_line_expands_to_prose() -> None:
    assert narrate_encoded(FALLBACK) == (
        "Bob shipped it. Facts: v1.2-shipped, CI-passed. Entities: Bob, CI. Concepts: release."
    )


def test_empty_sections_are_omitted_from_the_prose() -> None:
    assert narrate_encoded("summary=S;facts=[a];entities=[];concepts=[]") == "S. Facts: a."


def test_an_empty_summary_leaves_no_leading_period() -> None:
    assert narrate_encoded("summary=;facts=[a];entities=[b];concepts=[]") == "Facts: a. Entities: b."


@pytest.mark.parametrize("summary,rendered", [("Done!", "Done!"), ("Really?", "Really?"), ("Ship it", "Ship it.")])
def test_summary_punctuation_is_kept_or_added(summary: str, rendered: str) -> None:
    assert narrate_encoded(f"summary={summary};facts=[];entities=[];concepts=[]") == rendered


def test_all_sections_empty_answers_empty() -> None:
    assert narrate_encoded("summary=;facts=[];entities=[];concepts=[]") == ""


def test_a_padded_fallback_line_is_still_narrated() -> None:
    assert narrate_encoded("  " + FALLBACK + "  ") == narrate_encoded(FALLBACK)


@pytest.mark.parametrize(
    "prefix,schema",
    [
        ("CODE:", CodeSchema),
        ("COMMIT:", CommitSchema),
        ("DECISION:", DecisionSchema),
        ("PERSON:", PersonSchema),
        ("EVENT:", EventSchema),
    ],
    ids=["code", "commit", "decision", "person", "event"],
)
def test_prefixed_forms_delegate_to_their_schema(prefix: str, schema: type) -> None:
    encoded = prefix + "key: value"
    assert narrate_encoded(encoded) == schema().narrate(encoded)


def test_a_prefix_outranks_the_portfolio_sniff() -> None:
    encoded = "DECISION:AAPL:1@2/3:tech"
    assert narrate_encoded(encoded) == DecisionSchema().narrate(encoded)
    assert narrate_encoded(encoded) != PortfolioSchema().narrate(encoded)


def test_an_unknown_shape_is_returned_verbatim() -> None:
    for encoded in ("plain prose", "summary=incomplete", "AAPL:1@2"):
        assert narrate_encoded(encoded) == encoded


def test_a_whitespace_only_string_is_echoed() -> None:
    """DIVERGENCE pinned, not endorsed: only a falsy input gets the empty
    placeholder, so whitespace comes back unchanged."""
    assert narrate_encoded("   ") == "   "


@pytest.mark.parametrize("form", [PORTFOLIO, FALLBACK], ids=["portfolio", "fallback"])
def test_narration_is_idempotent(form: str) -> None:
    once = narrate_encoded(form)
    assert narrate_encoded(once) == once


def test_narration_removes_the_raw_delimiters() -> None:
    portfolio_out = narrate_encoded(PORTFOLIO)
    assert "@" not in portfolio_out and "/" not in portfolio_out
    fallback_out = narrate_encoded(FALLBACK)
    assert "summary=" not in fallback_out
    assert "|" not in fallback_out
    assert "[" not in fallback_out and "]" not in fallback_out


def test_the_fallback_narrator_echoes_an_unmatched_line() -> None:
    assert _narrate_fallback_form("not a fallback line") == "not a fallback line"


@pytest.mark.parametrize("value", ["", "   ", "summary=", "AAPL:", "summary=x;facts=[a", "CODE:"], ids=repr)
def test_narration_never_raises(value: str) -> None:
    assert isinstance(narrate_encoded(value), str)
