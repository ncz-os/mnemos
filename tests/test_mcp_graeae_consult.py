"""Unit tests for the graeae_consult MCP tool.

All tests mock the GRAEAE engine — no paid provider calls are made.
Coverage: registration, happy path, muses validation, mode selection,
debate/majority routing, cache-hit surfacing, and engine errors.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeEngine:
    """Test-double GRAEAE engine."""

    def __init__(self, consult_return=None, providers=None):
        self.consult = AsyncMock(return_value=consult_return or self._default())
        self.providers = providers or {
            "claude": {"model": "claude-opus-4-6"},
            "openai": {"model": "gpt-5.2"},
            "gemini": {"model": "gemini-3-pro"},
        }

    @staticmethod
    def _default():
        return {
            "all_responses": {
                "claude": {
                    "status": "success",
                    "response_text": "Paris is the capital of France.",
                    "model_id": "claude-opus-4-6",
                    "final_score": 0.95,
                },
            },
            "consensus_response": "Paris is the capital of France.",
            "consensus_score": 0.95,
            "winning_muse": "claude",
            "cost": 0.02,
            "latency_ms": 1200,
        }


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.fixture
def patch_engine(monkeypatch, fake_engine):
    """Replace get_graeae_engine with a test double."""
    import mnemos.domain.graeae.engine as engine_mod
    monkeypatch.setattr(engine_mod, "get_graeae_engine", lambda: fake_engine)
    return fake_engine


# ── Registration / schema ─────────────────────────────────────────────────────

def test_graeae_consult_is_registered():
    """graeae_consult appears in the canonical TOOL_REGISTRY."""
    from mnemos.mcp.tools import TOOL_REGISTRY
    assert "graeae_consult" in TOOL_REGISTRY


def test_graeae_consult_schema_requires_prompt():
    """The tool schema requires only 'prompt'."""
    from mnemos.mcp.tools import TOOL_REGISTRY
    tool = TOOL_REGISTRY["graeae_consult"]
    assert tool["required"] == ["prompt"]
    assert "prompt" in tool["parameters"]


def test_graeae_consult_schema_exposes_muses_and_mode():
    """Optional muses and mode parameters are declared."""
    from mnemos.mcp.tools import TOOL_REGISTRY
    tool = TOOL_REGISTRY["graeae_consult"]
    params = tool["parameters"]
    assert "muses" in params
    assert "mode" in params
    assert "category" in params


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_happy_path(patch_engine, fake_engine):
    """Default call (prompt only) delegates to engine.consult()."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="What is the capital of France?")

    fake_engine.consult.assert_awaited_once()
    call_kwargs = fake_engine.consult.call_args.kwargs
    assert call_kwargs["prompt"] == "What is the capital of France?"
    assert call_kwargs["task_type"] == "general"
    assert call_kwargs["selection"] is None
    assert call_kwargs["mode"] == "auto"

    assert result["success"] is True
    assert result["synthesis"] == "Paris is the capital of France."
    assert result["winning_muse"] == "claude"
    assert result["consensus_score"] == 0.95
    assert result["cost"] == 0.02
    assert result["per_muse"]["claude"]["status"] == "success"


@pytest.mark.asyncio
async def test_graeae_consult_with_category(patch_engine, fake_engine):
    """category maps to engine task_type."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    await tool_graeae_consult(prompt="test", category="code_generation")
    assert fake_engine.consult.call_args.kwargs["task_type"] == "code_generation"


@pytest.mark.asyncio
async def test_graeae_consult_with_mode(patch_engine, fake_engine):
    """mode is forwarded to engine."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    await tool_graeae_consult(prompt="test", mode="debate")
    assert fake_engine.consult.call_args.kwargs["mode"] == "debate"


@pytest.mark.asyncio
async def test_graeae_consult_with_muses(patch_engine, fake_engine):
    """When muses=[...] is set, selection is built from known providers."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(
        prompt="test",
        muses=["claude", "openai"],
    )

    call_kwargs = fake_engine.consult.call_args.kwargs
    assert call_kwargs["selection"] == {"claude": None, "openai": None}

    assert result["success"] is True


# ── Muse validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_unknown_muse_returns_error(patch_engine, fake_engine):
    """An unrecognized muse name fails loudly with available list."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(
        prompt="test",
        muses=["invalid_muse_name"],
    )

    assert result["success"] is False
    assert "unknown provider" in result["error"]
    assert "available" in result
    fake_engine.consult.assert_not_awaited()


@pytest.mark.asyncio
async def test_graeae_consult_empty_muses_list_returns_error(patch_engine, fake_engine):
    """An empty muses list returns an error."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test", muses=[])

    assert result["success"] is False
    fake_engine.consult.assert_not_awaited()


def test_validate_muses_rejects_invalid_names():
    """_validate_muses rejects names with characters beyond [A-Za-z0-9_-]."""
    from mnemos.mcp.tools.graeae import _validate_muses

    with pytest.raises(ValueError):
        _validate_muses(["bad name"])

    with pytest.raises(ValueError):
        _validate_muses(["bad/name"])

    # Valid names pass through
    assert _validate_muses(["claude", "openai"]) == ["claude", "openai"]
    assert _validate_muses(None) is None


def test_validate_muses_rejects_too_many():
    """More than 16 muses is rejected."""
    from mnemos.mcp.tools.graeae import _validate_muses

    with pytest.raises(ValueError):
        _validate_muses([f"muse_{i}" for i in range(20)])


# ── Error handling ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_engine_valueerror(patch_engine, fake_engine):
    """Engine ValueError is caught and returned as success=False."""
    fake_engine.consult.side_effect = ValueError("unsupported mode 'bad'")
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test", mode="bad")

    assert result["success"] is False
    assert "unsupported mode" in result["error"]


@pytest.mark.asyncio
async def test_graeae_consult_engine_exception(patch_engine, fake_engine):
    """Generic engine exception is caught and surfaced."""
    fake_engine.consult.side_effect = RuntimeError("provider down")
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test")

    assert result["success"] is False
    assert "Consultation engine error" in result["error"]


# ── Response surface ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_surfaces_cache_hit(patch_engine, fake_engine):
    """When the engine returns cache_hit, the tool surfaces it."""
    cached_response = {
        "all_responses": {},
        "consensus_response": "cached answer",
        "consensus_score": 0.90,
        "winning_muse": "openai",
        "cost": 0.0,
        "latency_ms": 5,
        "cache_hit": True,
    }
    fake_engine.consult.return_value = cached_response
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test")

    assert result["cache_hit"] is True
    assert result["synthesis"] == "cached answer"


@pytest.mark.asyncio
async def test_graeae_consult_surfaces_debate_rounds(patch_engine, fake_engine):
    """Debate-mode response includes round_1 and round_2."""
    debate_response = {
        "all_responses": {},
        "consensus_response": "debated answer",
        "consensus_score": 0.99,
        "winning_muse": "claude",
        "cost": 0.05,
        "latency_ms": 3000,
        "round_1": {"claude": {"response_text": "r1"}},
        "round_2": {"claude": {"response_text": "r2"}},
    }
    fake_engine.consult.return_value = debate_response
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test", mode="debate")

    assert result["round_1"] == {"claude": {"response_text": "r1"}}
    assert result["round_2"] == {"claude": {"response_text": "r2"}}


@pytest.mark.asyncio
async def test_graeae_consult_surfaces_majority_quorum(patch_engine, fake_engine):
    """Majority-mode response includes quorum fields."""
    majority_response = {
        "all_responses": {},
        "consensus_response": "quorum answer",
        "consensus_score": 0.85,
        "winning_muse": "claude",
        "cost": 0.03,
        "latency_ms": 2500,
        "quorum_reached": True,
        "quorum_threshold": 0.66,
        "similarity_pairs": {"claude:openai": 0.92},
    }
    fake_engine.consult.return_value = majority_response
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test", mode="majority")

    assert result["quorum_reached"] is True
    assert result["quorum_threshold"] == 0.66
    assert result["similarity_pairs"] == {"claude:openai": 0.92}


@pytest.mark.asyncio
async def test_graeae_consult_all_failure_no_crash(patch_engine, fake_engine):
    """When all providers fail, per_muse surfaces errors, synthesis is empty."""
    all_fail = {
        "all_responses": {
            "claude": {
                "status": "unavailable",
                "response_text": "",
                "model_id": "claude-opus-4-6",
                "final_score": 0.0,
                "error": "circuit open",
            },
            "openai": {
                "status": "unavailable",
                "response_text": "",
                "model_id": "gpt-5.2",
                "final_score": 0.0,
                "error": "rate-limited",
            },
        },
        "consensus_response": "",
        "consensus_score": 0.0,
        "winning_muse": None,
        "cost": 0.0,
        "latency_ms": 0,
    }
    fake_engine.consult.return_value = all_fail
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    result = await tool_graeae_consult(prompt="test")

    assert result["success"] is True  # engine call succeeded, all failures are surfaced
    assert result["synthesis"] == ""
    assert result["winning_muse"] is None
    assert result["per_muse"]["claude"]["status"] == "unavailable"
    assert result["per_muse"]["claude"]["error"] == "circuit open"
