"""Tests for PANTHEON model-group fallback chain resolution."""

from __future__ import annotations

from mnemos.domain.pantheon.chains import Deployment, resolve_chain


def _dep(group, provider, model):
    return Deployment(group=group, provider=provider, model=model)


def test_single_group_single_deployment_is_behavior_preserving():
    reg = {"coding": [_dep("coding", "openai", "gpt-5.4")]}
    chain = resolve_chain("coding", reg)
    assert [d.key for d in chain] == [("openai", "gpt-5.4")]


def test_group_with_multiple_deployments_keeps_order():
    reg = {"coding": [_dep("coding", "openai", "gpt-5.4"), _dep("coding", "groq", "gpt-oss-20b")]}
    chain = resolve_chain("coding", reg)
    assert [d.key for d in chain] == [("openai", "gpt-5.4"), ("groq", "gpt-oss-20b")]


def test_fallback_groups_appended_in_order():
    reg = {
        "primary": [_dep("primary", "openai", "gpt-5.4")],
        "cheap": [_dep("cheap", "deepseek", "v4-flash")],
        "local": [_dep("local", "groq", "gpt-oss-20b")],
    }
    fb = {"primary": ["cheap", "local"]}
    chain = resolve_chain("primary", reg, fb)
    assert [d.key for d in chain] == [
        ("openai", "gpt-5.4"),
        ("deepseek", "v4-flash"),
        ("groq", "gpt-oss-20b"),
    ]


def test_generic_star_fallback_applied():
    reg = {
        "primary": [_dep("primary", "openai", "gpt-5.4")],
        "backup": [_dep("backup", "xai", "grok")],
    }
    fb = {"*": ["backup"]}
    chain = resolve_chain("primary", reg, fb)
    assert [d.key for d in chain] == [("openai", "gpt-5.4"), ("xai", "grok")]


def test_dedup_same_provider_model_across_groups():
    reg = {
        "primary": [_dep("primary", "deepseek", "v4")],
        "cheap": [_dep("cheap", "deepseek", "v4"), _dep("cheap", "groq", "oss")],
    }
    fb = {"primary": ["cheap"]}
    chain = resolve_chain("primary", reg, fb)
    assert [d.key for d in chain] == [("deepseek", "v4"), ("groq", "oss")]  # deepseek/v4 not repeated


def test_max_chain_bounds_length():
    reg = {"g": [_dep("g", "p", f"m{i}") for i in range(10)]}
    chain = resolve_chain("g", reg, max_chain=3)
    assert len(chain) == 3
    assert [d.model for d in chain] == ["m0", "m1", "m2"]


def test_unknown_group_returns_empty():
    assert resolve_chain("missing", {"other": [_dep("other", "p", "m")]}) == []


def test_self_referential_fallback_ignored():
    reg = {"g": [_dep("g", "p", "m")]}
    fb = {"g": ["g"]}  # group lists itself as a fallback
    chain = resolve_chain("g", reg, fb)
    assert [d.key for d in chain] == [("p", "m")]  # no infinite loop, no duplicate


def test_duplicate_fallback_group_visited_once():
    reg = {
        "a": [_dep("a", "p", "ma")],
        "b": [_dep("b", "p", "mb")],
    }
    fb = {"a": ["b", "b"]}
    chain = resolve_chain("a", reg, fb)
    assert [d.key for d in chain] == [("p", "ma"), ("p", "mb")]
