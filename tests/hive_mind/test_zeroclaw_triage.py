from __future__ import annotations

from mnemos.hive_mind.zeroclaw_triage import routing_patch_for_decision


def test_zeroclaw_triage_patch_preserves_caps_and_applies_model_affinity() -> None:
    patch = routing_patch_for_decision(
        {"required_capabilities": ["repo-write"], "max_cost_tier": "B"},
        {
            "provider": "groq",
            "model_id": "qwen3-32b",
            "estimated_cost_usd": 0.0123,
            "dispatch_kind": "zeroclaw",
            "dispatch_required_capabilities": ["coding", "model:groq_qwen3_32b"],
            "dispatch_preferred_providers": ["groq"],
            "dispatch_preferred_models": ["qwen3-32b"],
        },
    )

    assert patch["eligible_kinds"] == ["zeroclaw"]
    assert patch["preferred_providers"] == ["groq"]
    assert patch["preferred_models"] == ["qwen3-32b"]
    assert patch["required_capabilities"] == ["repo-write", "coding", "model:groq_qwen3_32b"]
    assert patch["routing_metadata"]["estimated_cost_usd"] == 0.0123


def test_zeroclaw_triage_widens_default_cap_to_selected_provider_tier() -> None:
    patch = routing_patch_for_decision(
        {
            "required_capabilities": ["coding"],
            "max_cost_tier": "A",
            "routing_metadata": {"submitter_max_cost_tier_explicit": False},
        },
        {
            "provider": "groq",
            "model_id": "qwen3-32b",
            "dispatch_kind": "zeroclaw",
            "dispatch_required_capabilities": ["coding", "model:groq_qwen3_32b"],
            "dispatch_cost_tier": "B",
        },
    )

    assert patch["max_cost_tier"] == "B"


def test_zeroclaw_triage_does_not_widen_explicit_submitter_cap() -> None:
    patch = routing_patch_for_decision(
        {
            "required_capabilities": ["coding"],
            "max_cost_tier": "A",
            "routing_metadata": {"submitter_max_cost_tier_explicit": True},
        },
        {
            "provider": "groq",
            "model_id": "qwen3-32b",
            "dispatch_kind": "zeroclaw",
            "dispatch_required_capabilities": ["coding", "model:groq_qwen3_32b"],
            "dispatch_cost_tier": "B",
        },
    )

    assert patch["max_cost_tier"] == "A"
