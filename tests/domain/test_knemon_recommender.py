from __future__ import annotations

import pytest

from mnemos.domain.pantheon.recommendation import choose_recommended_model


_ROWS = [
    {
        "provider": "nvidia",
        "model_id": "nemotron-3-content-safety",
        "display_name": "Nemotron 3 Content Safety",
        "capabilities": ["chat", "reasoning"],
        "input_cost_per_mtok": 0.01,
        "output_cost_per_mtok": 0.01,
        "graeae_weight": 0.99,
        "context_window": 8192,
    },
    {
        "provider": "nvidia",
        "model_id": "qwen/qwen3-coder-480b-a35b-instruct",
        "display_name": "Qwen3 Coder 480B",
        "capabilities": ["chat", "coding", "reasoning"],
        "input_cost_per_mtok": 1.0,
        "output_cost_per_mtok": 3.0,
        "graeae_weight": 0.86,
        "context_window": 128000,
    },
    {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "capabilities": ["chat", "coding", "reasoning"],
        "input_cost_per_mtok": 3.0,
        "output_cost_per_mtok": 15.0,
        "graeae_weight": 0.94,
        "context_window": 200000,
    },
    {
        "provider": "anthropic",
        "model_id": "claude-opus-4-7",
        "display_name": "Claude Opus 4.7",
        "capabilities": ["chat", "coding", "reasoning"],
        "input_cost_per_mtok": 15.0,
        "output_cost_per_mtok": 75.0,
        "graeae_weight": 0.99,
        "context_window": 200000,
    },
    {
        "provider": "mnemos-local",
        "model_id": "bge-m3",
        "display_name": "BGE M3",
        "capabilities": ["embedding"],
        "input_cost_per_mtok": 0.0,
        "output_cost_per_mtok": 0.0,
        "graeae_weight": 0.82,
        "context_window": 8192,
    },
    {
        "provider": "groq",
        "model_id": "llama-3.1-8b-instant",
        "display_name": "Llama 3.1 8B Instant",
        "capabilities": ["chat", "routing"],
        "input_cost_per_mtok": 0.05,
        "output_cost_per_mtok": 0.08,
        "graeae_weight": 0.78,
        "context_window": 8192,
    },
    {
        "provider": "perplexity",
        "model_id": "sonar",
        "display_name": "Sonar",
        "capabilities": ["chat", "web_search"],
        "input_cost_per_mtok": 1.0,
        "output_cost_per_mtok": 1.0,
        "graeae_weight": 0.88,
        "context_window": 128000,
    },
]


@pytest.mark.parametrize(
    ("task_type", "expected_provider", "expected_model"),
    [
        ("code-fix", "nvidia", "qwen/qwen3-coder-480b-a35b-instruct"),
        ("code-generation", "nvidia", "qwen/qwen3-coder-480b-a35b-instruct"),
        ("narrative", "anthropic", "claude-sonnet-4-6"),
        ("reasoning", "anthropic", "claude-opus-4-7"),
        ("embedding", "mnemos-local", "bge-m3"),
        ("routing", "groq", "llama-3.1-8b-instant"),
        ("web_search", "perplexity", "sonar"),
    ],
)
def test_task_type_recommendations_are_capability_specific(task_type, expected_provider, expected_model):
    model, required = choose_recommended_model(_ROWS, task_type, cost_budget=10.0, quality_floor=0.7)

    assert required
    assert model is not None
    assert model["provider"] == expected_provider
    assert model["model_id"] == expected_model
    assert "content-safety" not in model["model_id"]


def test_embedding_task_requires_dedicated_embedding_model():
    model, required = choose_recommended_model(_ROWS, "embed", cost_budget=10.0, quality_floor=0.7)

    assert required == ["embedding"]
    assert model is not None
    assert model["model_id"] == "bge-m3"


def test_json_object_capabilities_match_task_requirements():
    rows = [
        {
            "provider": "deepseek-direct",
            "model_id": "deepseek-v4-pro",
            "display_name": "DeepSeek V4 Pro",
            "capabilities": '{"chat": true, "coding": true, "reasoning": true}',
            "input_cost_per_mtok": 0.435,
            "output_cost_per_mtok": 0.87,
            "graeae_weight": 0.85,
            "context_window": 128000,
        }
    ]

    model, required = choose_recommended_model(rows, "code-fix", cost_budget=10.0, quality_floor=0.7)

    assert required == ["coding"]
    assert model is not None
    assert model["model_id"] == "deepseek-v4-pro"
