"""Tests for build_fallback_chain (pure cross-provider chain construction)."""

from __future__ import annotations

from mnemos.domain.pantheon.router import RouteDecision, build_fallback_chain


def _primary(candidates):
    return RouteDecision(
        alias="auto:code",
        provider="openai",
        model_id="gpt-5.4",
        route_type="single",
        reason="r",
        model={"id": "gpt-5.4", "provider": "openai"},
        candidates=candidates,
    )


_MODELS = [
    {"id": "gpt-5.4", "provider": "openai"},
    {"id": "deepseek-v4-flash", "provider": "deepseek"},
    {"id": "grok-4.3", "provider": "xai"},
]


def test_chain_is_primary_then_distinct_candidates():
    chain = build_fallback_chain(_primary(["gpt-5.4", "deepseek-v4-flash", "grok-4.3"]), _MODELS)
    assert [(d.provider, d.model_id) for d in chain] == [
        ("openai", "gpt-5.4"),
        ("deepseek", "deepseek-v4-flash"),
        ("xai", "grok-4.3"),
    ]
    assert chain[1].reason == "fallback-candidate"


def test_primary_deduped_from_candidates():
    # primary appears in its own candidate list -> not added twice
    chain = build_fallback_chain(_primary(["gpt-5.4", "deepseek-v4-flash"]), _MODELS)
    assert [(d.provider, d.model_id) for d in chain] == [
        ("openai", "gpt-5.4"),
        ("deepseek", "deepseek-v4-flash"),
    ]


def test_unknown_candidate_skipped():
    chain = build_fallback_chain(_primary(["nope", "grok-4.3"]), _MODELS)
    assert [d.model_id for d in chain] == ["gpt-5.4", "grok-4.3"]


def test_max_chain_caps():
    chain = build_fallback_chain(_primary(["deepseek-v4-flash", "grok-4.3"]), _MODELS, max_chain=2)
    assert len(chain) == 2  # primary + 1 fallback


def test_consensus_primary_is_single_element():
    p = RouteDecision(alias="c", provider="p", model_id=None, route_type="consensus", reason="r")
    assert build_fallback_chain(p, _MODELS) == [p]


def test_no_candidates_is_single_element():
    chain = build_fallback_chain(_primary([]), _MODELS)
    assert [d.model_id for d in chain] == ["gpt-5.4"]


def test_candidate_missing_provider_skipped():
    models = [{"id": "gpt-5.4", "provider": "openai"}, {"id": "noprov"}]
    chain = build_fallback_chain(_primary(["noprov"]), models)
    assert [d.model_id for d in chain] == ["gpt-5.4"]
