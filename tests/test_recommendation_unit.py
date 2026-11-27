from dataclasses import FrozenInstanceError

import pytest

from mnemos.core import recommendation
from mnemos.core.recommendation import (
    RecommendationPolicy,
    choose_recommended_model,
    default_quality_floor,
    recommendation_policy,
)


def model(model_id="ordinary", **overrides):
    row = {
        "provider": "local",
        "model_id": model_id,
        "display_name": model_id,
        "capabilities": ["code"],
        "cost_tier": "A",
        "input_cost_per_mtok": 0.5,
        "output_cost_per_mtok": 1.5,
        "quality_score": 0.8,
        "context_window": 4096,
    }
    row.update(overrides)
    return row


def choose(rows, task="coding", budget=10.0, floor=0.7):
    return choose_recommended_model(rows, task, budget, floor)


@pytest.mark.parametrize(
    "task,expected",
    [
        (" CODE-FIX ", "coding"),
        ("summarization", "narrative"),
        ("architecture_design", "reasoning"),
        ("embeddings", "embedding"),
        ("classify", "routing"),
        ("web-search", "web_search"),
        ("unknown", "narrative"),
    ],
)
def test_policy_normalizes_aliases_and_defaults(task, expected):
    assert recommendation_policy(task).task_type == expected


def test_policy_is_immutable_and_has_empty_optional_defaults():
    policy = RecommendationPolicy(task_type="custom", required_caps=("chat",))
    assert policy.any_caps == ()
    assert policy.cost_ceiling is None
    with pytest.raises(FrozenInstanceError):
        policy.task_type = "other"


@pytest.mark.parametrize("floor,expected", [(-1, 0.7), (0, 0.7), (0.2, 0.2), (1.1, 1.1)])
def test_default_quality_floor(floor, expected):
    assert default_quality_floor("coding", floor) == expected


@pytest.mark.parametrize(
    "capabilities,accepted",
    [
        ([" CODER "], True),
        ('["code"]', True),
        ('{"code": true, "embedding": false}', True),
        ({"code": 1}, False),
        ("code", True),
        ('"code"', False),
        (None, False),
        (["code", "embeddings"], False),
        (["chat"], False),
    ],
)
def test_capability_encodings_aliases_and_exclusions(capabilities, accepted):
    chosen, required = choose([model(capabilities=capabilities)])
    assert (chosen is not None) is accepted
    assert required == ["coding"]


@pytest.mark.parametrize(
    "row",
    [
        model("safety-coder"),
        model(display_name="Content-Safety model"),
        model("gpt-5", provider="nvidia"),
        model("claude-opus-4-7", provider="anthropic", capabilities=["chat"]),
    ],
)
def test_excludes_special_purpose_disallowed_route_and_names(row):
    task = "narrative" if row["capabilities"] == ["chat"] else "coding"
    assert choose([row], task)[0] is None


@pytest.mark.parametrize("context,accepted", [(32768, True), (32769, False), (None, True), ("bad", True)])
def test_routing_any_capability_and_context_limit(context, accepted):
    chosen, required = choose([model(capabilities=["chat"], context_window=context)], "routing")
    assert (chosen is not None) is accepted
    assert required == ["chat", "routing"]


def test_tier_fallback_only_when_no_allowed_tier_exists():
    fallback = model("fallback", cost_tier="C")
    allowed = model("allowed", cost_tier="B", quality_score=0.1)
    assert choose([fallback])[0]["model_id"] == "fallback"
    assert choose([fallback, allowed])[0] is None
    assert choose([model("fallback", cost_tier="C", capabilities=["chat"])], "narrative")[0] is None


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"cost_tier": None, "usage_tier": " b "}, True),
        ({"cost_tier": "invalid", "input_cost_per_mtok": 10, "output_cost_per_mtok": 10}, True),
        ({"cost_tier": "invalid", "input_cost_per_mtok": 11, "output_cost_per_mtok": 11}, False),
        ({"cost_tier": None, "input_cost_per_mtok": None, "quality_score": 0.94}, True),
        ({"cost_tier": None, "input_cost_per_mtok": None, "quality_score": 0.95}, False),
    ],
)
def test_tier_can_be_inferred_or_taken_from_usage_tier(updates, expected):
    assert (choose([model(capabilities=["routing"], **updates)], "routing")[0] is not None) is expected


def test_preference_ranking_precedes_cost_and_quality_and_formats_result():
    preferred = model(
        "qwen/qwen3-coder-480b-instruct",
        provider="nvidia",
        display_name="Preferred",
        input_cost_per_mtok="4",
        output_cost_per_mtok="6",
        graeae_weight="0.75",
    )
    cheaper = model("generic", input_cost_per_mtok=0, output_cost_per_mtok=0, quality_score=0.99)
    chosen, required = choose([cheaper, preferred])
    assert chosen == {
        "provider": "nvidia",
        "model_id": "qwen/qwen3-coder-480b-instruct",
        "display_name": "Preferred",
        "cost_per_mtok": 5.0,
        "quality_score": 0.75,
        "context_window": 4096,
    }
    assert required == ["coding"]


def test_ranking_uses_name_token_then_cost_then_quality():
    rows = [
        model("generic", input_cost_per_mtok=0, output_cost_per_mtok=0),
        model("other-coder", quality_score=0.75),
        model("best-coder", quality_score=0.95),
        model("costly-coder", quality_score=0.99, input_cost_per_mtok=2, output_cost_per_mtok=2),
    ]
    assert choose(rows)[0]["model_id"] == "best-coder"


def test_policy_ceiling_not_requested_budget_controls_qualification():
    preferred = model("deepseek-coder", provider="deepseek-direct", input_cost_per_mtok=5, output_cost_per_mtok=5)
    cheap = model("generic", input_cost_per_mtok=0.1, output_cost_per_mtok=0.1)
    assert choose([cheap, preferred], budget=0.01)[0]["model_id"] == "deepseek-coder"
    preferred.update(input_cost_per_mtok=11, output_cost_per_mtok=11)
    assert choose([preferred, cheap], budget=100)[0]["model_id"] == "generic"


def test_requested_budget_is_used_when_policy_has_no_ceiling(monkeypatch):
    policy = RecommendationPolicy(
        task_type="custom", required_caps=("code",), preferred_models=(("local", "preferred"),)
    )
    monkeypatch.setattr(recommendation, "recommendation_policy", lambda task: policy)
    preferred = model("preferred", input_cost_per_mtok=2, output_cost_per_mtok=2)
    cheap = model("cheap", input_cost_per_mtok=1, output_cost_per_mtok=1)
    assert choose([preferred, cheap], budget=1)[0]["model_id"] == "cheap"
    assert choose([preferred, cheap], budget=2)[0]["model_id"] == "preferred"


def test_degraded_selection_allows_missing_or_excess_cost_but_still_requires_quality():
    missing = model("missing", input_cost_per_mtok=None)
    costly = model("costly", input_cost_per_mtok=20, output_cost_per_mtok=20)
    low_quality = model("low", input_cost_per_mtok=0, output_cost_per_mtok=0, quality_score=0.1)
    assert choose([missing, costly, low_quality])[0]["model_id"] == "costly"
    assert choose([missing])[0]["cost_per_mtok"] is None
    assert choose([low_quality])[0] is None


def test_zero_floor_defaults_to_point_seven_and_negative_floor_clamps_to_zero():
    low = model(quality_score=0.2)
    assert choose([low], floor=0)[0] is None
    assert choose([low], floor=-1)[0]["model_id"] == "ordinary"


def test_quality_floor_is_inclusive_and_falls_back_from_zero_weight():
    row = model(graeae_weight=0, quality_score=0.7)
    assert choose([row], floor=0.7)[0]["quality_score"] == 0.7
    assert choose([row], floor=0.71)[0] is None


def test_key_only_row_and_bad_numeric_values():
    class KeyOnlyRow:
        def __getitem__(self, key):
            return {"provider": "local", "model_id": "row", "capabilities": ["code"],
                    "input_cost_per_mtok": "bad", "output_cost_per_mtok": "2",
                    "quality_score": "0.8"}[key]

    chosen, required = choose([KeyOnlyRow()])
    assert chosen == {
        "provider": "local",
        "model_id": "row",
        "display_name": None,
        "cost_per_mtok": 1.0,
        "quality_score": 0.8,
        "context_window": None,
    }
    assert required == ["coding"]


def test_empty_rows_return_task_capabilities():
    assert choose([], "search") == (None, ["web_search"])
