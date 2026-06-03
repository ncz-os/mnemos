"""Unit tests for the graeae_consult MCP tool.

All tests mock the GRAEAE engine — no paid provider calls are made.
Coverage: registration, happy path, muses validation, mode selection,
debate/majority routing, cache-hit surfacing, and engine errors.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import pytest
from unittest.mock import AsyncMock

from mnemos.core.auth_context import UserContext
from mnemos.persistence.base import CONSULTATIONS_CAPABILITY


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


class FakeConsultationsRepo:
    def __init__(self, consultation_id="consult-123", side_effect=None):
        self.consultation_id = consultation_id
        self.side_effect = side_effect
        self.calls = []

    async def create_consultation_with_audit(self, tx, **kwargs):
        self.calls.append((tx, kwargs))
        if self.side_effect is not None:
            raise self.side_effect
        return self.consultation_id


class FakeBackend:
    supports_webhooks = False
    capabilities = {CONSULTATIONS_CAPABILITY}

    def __init__(self, consultations=None):
        self.consultations = consultations or FakeConsultationsRepo()
        self.close = AsyncMock()
        self.tx = object()

    @asynccontextmanager
    async def transactional(self):
        yield self.tx


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.fixture
def patch_engine(monkeypatch, fake_engine):
    """Replace get_graeae_engine with a test double."""
    import mnemos.domain.graeae.engine as engine_mod
    monkeypatch.setattr(engine_mod, "get_graeae_engine", lambda: fake_engine)
    return fake_engine


@pytest.fixture
def authenticated_user():
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="team-red",
        authenticated=True,
    )


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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(
        prompt="What is the capital of France?",
        user=UserContext(
            user_id="alice",
            group_ids=[],
            role="user",
            namespace="default",
            authenticated=True,
        ),
    )

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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    await tool_graeae_consult(
        prompt="test",
        category="code_generation",
        user=UserContext(
            user_id="alice",
            group_ids=[],
            role="user",
            namespace="default",
            authenticated=True,
        ),
    )
    assert fake_engine.consult.call_args.kwargs["task_type"] == "code_generation"


@pytest.mark.asyncio
async def test_graeae_consult_with_mode(patch_engine, fake_engine):
    """mode is forwarded to engine."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    await tool_graeae_consult(
        prompt="test",
        mode="debate",
        user=UserContext(
            user_id="alice",
            group_ids=[],
            role="user",
            namespace="default",
            authenticated=True,
        ),
    )
    assert fake_engine.consult.call_args.kwargs["mode"] == "debate"


@pytest.mark.asyncio
async def test_graeae_consult_with_muses(patch_engine, fake_engine, authenticated_user):
    """When muses=[...] is set, selection is built from known providers."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", muses=["claude", "openai"], user=authenticated_user)

    call_kwargs = fake_engine.consult.call_args.kwargs
    assert call_kwargs["selection"] == {"claude": None, "openai": None}

    assert result["success"] is True


# ── Muse validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_unknown_muse_returns_error(patch_engine, fake_engine, authenticated_user):
    """An unrecognized muse name fails loudly with available list."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", muses=["invalid_muse_name"], user=authenticated_user)

    assert result["success"] is False
    assert "unknown provider" in result["error"]
    assert "available" in result
    fake_engine.consult.assert_not_awaited()


@pytest.mark.asyncio
async def test_graeae_consult_empty_muses_list_returns_error(patch_engine, fake_engine, authenticated_user):
    """An empty muses list returns an error."""
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", muses=[], user=authenticated_user)

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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(
        prompt="test",
        mode="bad",
        user=UserContext(
            user_id="alice",
            group_ids=[],
            role="user",
            namespace="default",
            authenticated=True,
        ),
    )

    assert result["success"] is False
    assert "unsupported mode" in result["error"]


@pytest.mark.asyncio
async def test_graeae_consult_engine_exception(patch_engine, fake_engine, authenticated_user):
    """Generic engine exception is caught and surfaced."""
    fake_engine.consult.side_effect = RuntimeError("provider down")
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", user=authenticated_user)

    assert result["success"] is False
    assert result["error"] == "Consultation engine error"
    assert result["error_type"] == "RuntimeError"


# ── Response surface ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graeae_consult_surfaces_cache_hit(patch_engine, fake_engine, authenticated_user):
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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", user=authenticated_user)

    assert result["cache_hit"] is True
    assert result["synthesis"] == "cached answer"


@pytest.mark.asyncio
async def test_graeae_consult_surfaces_debate_rounds(patch_engine, fake_engine, authenticated_user):
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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", mode="debate", user=authenticated_user)

    assert result["round_1"] == {"claude": {"response_text": "r1"}}
    assert result["round_2"] == {"claude": {"response_text": "r2"}}


@pytest.mark.asyncio
async def test_graeae_consult_surfaces_majority_quorum(patch_engine, fake_engine, authenticated_user):
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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", mode="majority", user=authenticated_user)

    assert result["quorum_reached"] is True
    assert result["quorum_threshold"] == 0.66
    assert result["similarity_pairs"] == {"claude:openai": 0.92}


@pytest.mark.asyncio
async def test_graeae_consult_all_failure_no_crash(patch_engine, fake_engine, authenticated_user):
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

    import mnemos.core.lifecycle as lifecycle

    lifecycle._persistence_backend = FakeBackend()
    result = await tool_graeae_consult(prompt="test", user=authenticated_user)

    assert result["success"] is True  # engine call succeeded, all failures are surfaced
    assert result["synthesis"] == ""
    assert result["winning_muse"] is None
    assert result["per_muse"]["claude"]["status"] == "unavailable"
    assert result["per_muse"]["claude"]["error"] == "circuit open"


@pytest.mark.asyncio
async def test_graeae_consult_persists_audit_row_and_returns_consultation_id(
    monkeypatch,
    patch_engine,
    authenticated_user,
):
    from mnemos.core import lifecycle
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    backend = FakeBackend()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    result = await tool_graeae_consult(prompt="test prompt", muses=["anthropic"], user=authenticated_user)

    assert result["success"] is True
    assert result["consultation_id"] == "consult-123"
    tx, kwargs = backend.consultations.calls[0]
    assert tx is backend.tx
    assert kwargs["owner_id"] == "alice"
    assert kwargs["namespace"] == "team-red"
    assert kwargs["mode"] == "auto"
    assert kwargs["task_type"] == "general"
    assert patch_engine.consult.call_args.kwargs["selection"] == {"claude": None}


@pytest.mark.asyncio
async def test_graeae_consult_persistence_failure_fails_closed(
    monkeypatch,
    patch_engine,
    authenticated_user,
):
    from mnemos.core import lifecycle
    from mnemos.mcp.tools.graeae import tool_graeae_consult

    backend = FakeBackend(consultations=FakeConsultationsRepo(side_effect=RuntimeError("db write failed")))
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    result = await tool_graeae_consult(prompt="test prompt", user=authenticated_user)

    assert result["success"] is False
    assert result["error"] == "Consultation persistence failed; audit trail is required."
    assert result["error_type"] == "RuntimeError"
