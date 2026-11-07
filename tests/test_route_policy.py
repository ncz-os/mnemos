"""Route policy: a banned provider/model pair can never be dispatched.

The gateway used to *attempt* pairs that can never be served — a
closed-weight frontier model on a provider that carries only open-weight
models, a model owner's model on a reseller — and only learned the pair was
impossible when the upstream refused. These tests pin the local refusal:

  * ``check_route`` returns a reason naming the rule for every banned pair
    (case variants, ``vendor/`` prefixes and ``:free`` suffixes included) and
    ``None`` for every allowed pair;
  * the dispatch choke point rejects a banned pair with HTTP 400 *before any
    HTTP client is constructed* — ``httpx``'s client classes are patched and
    asserted never called;
  * the catalog (model recommendation) and model discovery never surface a
    banned pair;
  * every pair the engine can build from its own default providers passes,
    so the defaults can never contain a banned pair.

No network: every test that reaches a dispatcher replaces it with a recorder.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from mnemos.core import route_policy
from mnemos.core.route_policy import (
    CLOSED_FRONTIER,
    DEEPSEEK_DIRECT,
    MINIMAX_DIRECT,
    OPEN_WEIGHT_ONLY,
    check_route,
    is_route_allowed,
)

OPEN_WEIGHT_HUB = "nvidia"
DEEPSEEK_ROUTE = "deepseek-direct"
MINIMAX_ROUTE = "minimax"


# ─── banned table ───────────────────────────────────────────────────────────
#
# (provider, model_id, expected rule). The model ids are the ones a history
# review found *attempted* through routes that cannot carry them, plus the
# prefixed / case / suffix spellings a caller can produce for the same pair.

BANNED = [
    # closed-weight frontier on a provider that carries only open-weight models
    (OPEN_WEIGHT_HUB, "gpt-6-sol", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "gpt-6-astra", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "gpt-5.5", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "GPT-5.5", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "GPT-6-SOL", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "gpt-5.5:free", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "gpt-5.5:extended", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "nvidia/gpt-5.5", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, f"{OPEN_WEIGHT_HUB}/GPT-5.5:free", OPEN_WEIGHT_ONLY),
    ("NVIDIA", "gpt-6-sol", OPEN_WEIGHT_ONLY),
    ("nvidia_nim", "gpt-5.5", OPEN_WEIGHT_ONLY),
    ("nvidia-nim", "claude-opus-4-7", OPEN_WEIGHT_ONLY),
    ("ngc", "claude-sonnet-4-6", OPEN_WEIGHT_ONLY),
    ("eih", "gemini-2.5-pro", OPEN_WEIGHT_ONLY),
    ("eih", "grok-4", OPEN_WEIGHT_ONLY),
    ("nvidia-eih", "gpt-6-astra", OPEN_WEIGHT_ONLY),
    ("nvidia_eih", "claude-opus-4-7", OPEN_WEIGHT_ONLY),
    # a closed-weight route of an open-weight lab on the open-weight hub
    (OPEN_WEIGHT_HUB, "deepseek-v4-pro", DEEPSEEK_DIRECT),
    (OPEN_WEIGHT_HUB, "DeepSeek-V4-Pro", DEEPSEEK_DIRECT),
    (OPEN_WEIGHT_HUB, "nvidia/deepseek-v4-pro", DEEPSEEK_DIRECT),
    (OPEN_WEIGHT_HUB, "minimax-m3.1", MINIMAX_DIRECT),
    (OPEN_WEIGHT_HUB, "minimaxai/minimax-m2", MINIMAX_DIRECT),
    # a model family the hub does not carry
    (OPEN_WEIGHT_HUB, "sonar-pro", OPEN_WEIGHT_ONLY),
    (OPEN_WEIGHT_HUB, "acme-internal-7b", OPEN_WEIGHT_ONLY),
    # DeepSeek off its direct route
    ("together", "deepseek-v3", DEEPSEEK_DIRECT),
    ("openrouter", "deepseek-chat", DEEPSEEK_DIRECT),
    ("groq", "DeepSeek-R1", DEEPSEEK_DIRECT),
    ("perplexity", "deepseek-r1:free", DEEPSEEK_DIRECT),
    # MiniMax off its direct route
    ("together", "minimax-m3", MINIMAX_DIRECT),
    ("openrouter", "MiniMax-M3.1-Flash", MINIMAX_DIRECT),
    # closed-frontier through a reseller or cloud marketplace
    ("together", "gpt-5", CLOSED_FRONTIER),
    ("openrouter", "claude-sonnet-4-6", CLOSED_FRONTIER),
    ("openrouter", "openai/gpt-5.5:free", CLOSED_FRONTIER),
    ("groq", "grok-4", CLOSED_FRONTIER),
    ("bedrock", "claude-3-5-sonnet", CLOSED_FRONTIER),
    ("azure", "gpt-4o", CLOSED_FRONTIER),
    ("vertex", "gemini-2.5-flash", CLOSED_FRONTIER),
    # ...and the same family under a deployment-flavoured id. A reseller id
    # is identified by its *leading* family, so a suffixed or prefixed
    # spelling (aws-bedrock-xyz, deepinfra-prod, openrouter-free) is the same
    # ban, not a hole.
    ("aws-bedrock-xyz", "gpt-5.5", CLOSED_FRONTIER),
    ("aws-bedrock", "gpt-5.5", CLOSED_FRONTIER),
    ("bedrock-prod", "claude-3-5-sonnet", CLOSED_FRONTIER),
    ("azure-openai-2", "gpt-6-sol", CLOSED_FRONTIER),
    ("deepinfra-prod", "gpt-5.5", CLOSED_FRONTIER),
    ("openrouter-free", "gemini-3-pro", CLOSED_FRONTIER),
    ("groq-eu", "grok-4", CLOSED_FRONTIER),
    ("together-ai", "claude-4", CLOSED_FRONTIER),
    ("fireworks-ai", "gpt-5.5:free", CLOSED_FRONTIER),
    ("aws_bedrock", "GPT-5.5", CLOSED_FRONTIER),
    ("AWS-BEDROCK-PROD", "gpt-5.5", CLOSED_FRONTIER),
    # the same suffixing applies to the direct-route rules
    ("openrouter-free", "deepseek-chat", DEEPSEEK_DIRECT),
    ("together-ai", "minimax-m3", MINIMAX_DIRECT),
]


@pytest.mark.parametrize("provider,model_id,rule", BANNED)
def test_banned_pair_returns_reason_naming_the_rule(provider, model_id, rule):
    reason = check_route(provider, model_id)

    assert reason is not None, f"{provider}/{model_id} must be refused"
    assert f"route policy {rule}" in reason
    assert model_id in reason and provider in reason
    assert is_route_allowed(provider, model_id) is False


@pytest.mark.parametrize("provider,model_id,rule", BANNED)
def test_banned_pair_is_refused_under_every_spelling(provider, model_id, rule):
    """A namespaced id is split into (provider, bare id) before the check, so
    the raw spelling, the split spelling and a lower-cased spelling must all
    be refused — the gateway accepts any of them as a model name."""
    bare = model_id.rsplit("/", 1)[-1]
    assert check_route(provider, bare) is not None
    assert check_route(provider.lower(), bare.lower()) is not None


ALLOWED = [
    # open-weight families on a provider that carries only open-weight models
    (OPEN_WEIGHT_HUB, "nemotron-3-ultra-550b-a55b"),
    (OPEN_WEIGHT_HUB, "nvidia/nemotron-4-340b-instruct"),
    (OPEN_WEIGHT_HUB, "Nemotron-70B"),
    (OPEN_WEIGHT_HUB, "meta/llama-3.3-70b-instruct"),
    (OPEN_WEIGHT_HUB, "nvidia/llama-3.1-70b-instruct"),
    (OPEN_WEIGHT_HUB, "mistralai/mixtral-8x7b-instruct"),
    (OPEN_WEIGHT_HUB, "Qwen/Qwen3-235B-A22B"),
    (OPEN_WEIGHT_HUB, "moonshotai/kimi-k2.6"),
    (OPEN_WEIGHT_HUB, "zai/glm-4.6"),
    (OPEN_WEIGHT_HUB, "nemotron-deepseek-distill"),
    ("nvidia_nim", "nemotron-4-340b"),
    ("ngc", "llama-3.1-8b-instruct"),
    ("eih", "nemotron-3-ultra-550b-a55b"),
    # DeepSeek on its direct route
    (DEEPSEEK_ROUTE, "deepseek-v4-flash"),
    (DEEPSEEK_ROUTE, "deepseek-v4-pro"),
    (DEEPSEEK_ROUTE, "DeepSeek-V4-Flash"),
    ("deepseek", "deepseek-chat"),
    # MiniMax on its direct route
    (MINIMAX_ROUTE, "minimax-m3"),
    (MINIMAX_ROUTE, "MiniMax-M3.1-Flash"),
    # closed-frontier on its owner's own route
    ("openai", "gpt-5.5"),
    ("openai", "gpt-6-sol"),
    ("claude", "claude-opus-4-7"),
    ("anthropic", "claude-sonnet-4-6"),
    ("gemini", "gemini-2.5-pro"),
    ("xai", "grok-4"),
    # unrelated pairs, including an operator-added provider
    ("together", "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"),
    ("groq", "llama-3.3-70b-versatile"),
    ("perplexity", "sonar-pro"),
    ("my-local-proxy", "gpt-5"),
    ("my-local-proxy", "anything-at-all"),
    # a family merely *contained* in a provider id is an operator's own
    # route, not the reseller the token happens to name - family matching is
    # anchored at the head, so these keep their current behaviour
    ("my-groq-proxy", "gpt-5"),
    ("acme-bedrock-lab", "claude-opus-4-7"),
    ("not-together-inference", "gemini-2.5-pro"),
    ("", ""),
]


# A reseller's id may be spelled with a deployment suffix (``aws-bedrock-xyz``)
# or a location prefix (``openrouter-free``). These are the same family and
# the same ban; the containment cases above prove the anchor is at the head.
RESELLER_ID_SPELLINGS = [
    "together", "together-ai", "groq", "groq-eu", "openrouter",
    "openrouter-free", "deepinfra", "deepinfra-prod", "fireworks",
    "fireworks-ai", "aws", "aws-bedrock", "aws-bedrock-xyz", "bedrock",
    "bedrock-prod", "azure", "azure-openai-2", "vertex-ai", "sagemaker-prod",
    "AWS_BEDROCK_EU_WEST_1",
]


@pytest.mark.parametrize("provider", RESELLER_ID_SPELLINGS)
def test_every_spelling_of_a_reseller_id_is_banned_for_frontier_models(provider):
    for model_id in ("gpt-5.5", "gpt-6-sol", "GPT-6-ASTRA", "claude-opus-4-7",
                     "gemini-3-pro", "grok-4", "gpt-5.5:free"):
        reason = check_route(provider, model_id)
        assert reason is not None, f"{provider}/{model_id} was not refused"
        assert CLOSED_FRONTIER in reason


@pytest.mark.parametrize("provider,model_id", ALLOWED)
def test_allowed_pair_passes(provider, model_id):
    assert check_route(provider, model_id) is None
    assert is_route_allowed(provider, model_id) is True


def test_policy_is_pure_and_repeatable():
    assert check_route("nvidia", "gpt-5.5") == check_route("nvidia", "gpt-5.5")
    assert check_route("nvidia", "nemotron-4-340b") is None
    # No state accumulates between calls in either direction.
    assert check_route("openai", "gpt-5.5") is None


def test_rules_are_data_not_per_call_branches():
    rule_ids = {rule.rule for rule in route_policy.RULES}
    assert rule_ids == {OPEN_WEIGHT_ONLY, DEEPSEEK_DIRECT, MINIMAX_DIRECT, CLOSED_FRONTIER}
    for rule in route_policy.RULES:
        assert rule.providers, f"{rule.rule} has no provider family"


# ─── no HTTP client is ever constructed for a refused pair ──────────────────


@pytest.fixture
def transport_spy(monkeypatch):
    """Tripwire on every HTTP client the gateway could build.

    Patches ``httpx``'s client classes and module-level helpers, so any
    construction or call — including by a provider callback — records an
    attempt instead of touching the network.
    """
    calls: list[tuple] = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(f"network call attempted for a refused route: {args!r}")

    monkeypatch.setattr(httpx, "AsyncClient", _record)
    monkeypatch.setattr(httpx, "Client", _record)
    monkeypatch.setattr(httpx, "request", _record)
    monkeypatch.setattr(httpx, "get", _record)
    monkeypatch.setattr(httpx, "post", _record)
    return calls


def _gateway(monkeypatch, *, provider: str, route_calls: list, response_text: str = "ok"):
    """Install a GRAEAE-shaped engine whose route() records instead of dialing.

    ``route()`` returns the provider-response shape a real engine returns
    (``{"status": ..., "response_text": ...}``); ``_route_to_provider`` is
    the thin text extractor over it, so the response_text is the assertion
    surface for an allowed pair.
    """
    from mnemos.api.routes import openai_compat

    class _Recorder:
        def __init__(self):
            self.providers = {
                provider: {
                    "api": "openai",
                    "model": "irrelevant",
                    "url": "https://example.invalid/v1/chat/completions",
                    "key_name": provider,
                }
            }

        async def route(self, *args, **kwargs):
            route_calls.append((args, kwargs))
            return {"status": "success", "response_text": response_text}

        async def route_stream(self, *args, **kwargs):
            route_calls.append((args, kwargs))
            yield {"content": response_text}

    async def _resolver(_model: str) -> str:
        return provider

    monkeypatch.setattr(openai_compat, "_resolve_provider_for_model", _resolver)
    monkeypatch.setattr(openai_compat, "_compat_get_engine", lambda: _Recorder())
    return openai_compat


@pytest.mark.parametrize(
    "provider,model_id,rule",
    [
        (OPEN_WEIGHT_HUB, "gpt-6-sol", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "gpt-6-astra", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "gpt-5.5", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "GPT-5.5", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "gpt-5.5:free", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "nvidia/gpt-5.5", OPEN_WEIGHT_ONLY),
        ("nvidia_nim", "gpt-5.5", OPEN_WEIGHT_ONLY),
        ("ngc", "claude-opus-4-7", OPEN_WEIGHT_ONLY),
        ("eih", "grok-4", OPEN_WEIGHT_ONLY),
        (OPEN_WEIGHT_HUB, "deepseek-v4-pro", DEEPSEEK_DIRECT),
        ("together", "deepseek-v3", DEEPSEEK_DIRECT),
        (OPEN_WEIGHT_HUB, "minimax-m3.1", MINIMAX_DIRECT),
        ("together", "gpt-5", CLOSED_FRONTIER),
    ],
)
def test_rejected_at_dispatch_choke_point_without_any_http_call(
    monkeypatch, transport_spy, provider, model_id, rule
):
    from fastapi import HTTPException

    route_calls: list = []
    openai_compat = _gateway(monkeypatch, provider=provider, route_calls=route_calls)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            openai_compat._route_to_provider(
                model=model_id,
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                user=None,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "route_policy_violation"
    assert f"route policy {rule}" in exc.value.detail["error"]["message"]
    assert route_calls == [], "the banned route reached the provider dispatcher"
    assert transport_spy == [], "an HTTP client was constructed for a banned route"


def test_policy_refusal_precedes_engine_construction(monkeypatch):
    """Unit-level proof at the choke point itself: ``get_graeae_engine`` is
    the escape hatch dispatch uses to reach the network, and it is never
    called for a refused pair."""
    import mnemos.domain.openai_compat.providers as gateway_providers
    from mnemos.domain.openai_compat.providers import OpenAICompatError, _prepare_provider_route

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the engine (and its HTTP stack) was built for a refused pair")

    monkeypatch.setattr(gateway_providers, "get_graeae_engine", _forbidden)

    async def _resolver(_model: str) -> str:
        return OPEN_WEIGHT_HUB

    with pytest.raises(OpenAICompatError) as exc:
        asyncio.run(
            _prepare_provider_route(
                "gpt-5.5",
                [{"role": "user", "content": "hello"}],
                resolve_provider=_resolver,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "route_policy_violation"
    assert f"route policy {OPEN_WEIGHT_ONLY}" in exc.value.detail["error"]["message"]


def test_streaming_dispatch_also_refuses_before_any_http_call(monkeypatch, transport_spy):
    from fastapi import HTTPException

    route_calls: list = []
    openai_compat = _gateway(monkeypatch, provider=OPEN_WEIGHT_HUB, route_calls=route_calls)

    async def _drain():
        async for _chunk in openai_compat._route_to_provider_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hello"}],
        ):
            pass

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_drain())

    assert exc.value.status_code == 400
    assert f"route policy {OPEN_WEIGHT_ONLY}" in exc.value.detail["error"]["message"]
    assert route_calls == []
    assert transport_spy == []


def test_allowed_pair_still_reaches_the_provider(monkeypatch, transport_spy):
    """The guard must not change behaviour for an allowed pair."""
    route_calls: list = []
    openai_compat = _gateway(monkeypatch, provider=OPEN_WEIGHT_HUB, route_calls=route_calls)

    text = asyncio.run(
        openai_compat._route_to_provider(
            model="nvidia/nemotron-3-ultra-550b-a55b",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            max_tokens=64,
            user=None,
        )
    )

    assert text == "ok"
    assert len(route_calls) == 1
    args, _kwargs = route_calls[0]
    assert args[0] == OPEN_WEIGHT_HUB
    assert args[1] == "nemotron-3-ultra-550b-a55b"
    assert transport_spy == []


def test_allowed_pair_text_comes_from_the_provider_response(monkeypatch, transport_spy):
    """``_route_to_provider`` extracts ``response_text`` from the
    provider-response dict it dispatches; an allowed pair's text must come
    back verbatim, not be compared against the dict itself."""
    route_calls: list = []
    openai_compat = _gateway(
        monkeypatch, provider=OPEN_WEIGHT_HUB, route_calls=route_calls, response_text="nemotron says hi"
    )

    text = asyncio.run(
        openai_compat._route_to_provider(
            model="nemotron-3-ultra-550b-a55b",
            messages=[{"role": "user", "content": "hello"}],
            temperature=None,
            max_tokens=None,
            user=None,
        )
    )

    assert isinstance(text, str)
    assert text == "nemotron says hi"
    assert len(route_calls) == 1
    assert transport_spy == []


# ─── catalog / discovery never surface a banned pair ───────────────────────


def _row(provider, model_id, **over):
    row = {
        "provider": provider,
        "model_id": model_id,
        "display_name": model_id,
        "family": "general",
        "context_window": 128000,
        "input_cost_per_mtok": 0.5,
        "output_cost_per_mtok": 0.5,
        "graeae_weight": 1.0,
        "quality_score": 1.0,
        "capabilities": '["chat", "reasoning", "coding"]',
    }
    row.update(over)
    return row


def test_recommendation_never_selects_a_banned_pair():
    from mnemos.core.recommendation import choose_recommended_model

    # The banned pairs outrank every allowed row on every axis the policy
    # sorts by (higher weight, cheaper), so only the guard can exclude them.
    rows = [
        _row(OPEN_WEIGHT_HUB, "gpt-5.5", graeae_weight=5.0),
        _row(OPEN_WEIGHT_HUB, "gpt-6-sol", graeae_weight=4.5),
        _row(OPEN_WEIGHT_HUB, "deepseek-v4-pro", graeae_weight=0.5),
        _row("together", "gpt-5", graeae_weight=3.5),
        _row(DEEPSEEK_ROUTE, "deepseek-v4-pro", graeae_weight=0.5),
        _row(OPEN_WEIGHT_HUB, "nemotron-3-ultra-550b-a55b", graeae_weight=0.5),
    ]

    chosen, _caps = choose_recommended_model(rows, "reasoning", cost_budget=50.0, quality_floor=0.05)

    assert chosen is not None
    assert check_route(chosen["provider"], chosen["model_id"]) is None
    assert chosen["provider"] == DEEPSEEK_ROUTE


def test_recommendation_returns_none_when_only_banned_rows_qualify():
    from mnemos.core.recommendation import choose_recommended_model

    rows = [
        _row(OPEN_WEIGHT_HUB, "gpt-5.5"),
        _row("together", "claude-sonnet-4-6"),
    ]

    chosen, _caps = choose_recommended_model(rows, "reasoning", cost_budget=50.0, quality_floor=0.05)

    assert chosen is None


def test_list_models_hides_banned_pairs(monkeypatch):
    from tests._fake_backend import FakePostgresConnection, install_fake_postgres_backend

    from mnemos.api.routes import openai_compat

    rows = [
        {"provider": OPEN_WEIGHT_HUB, "model_id": "gpt-5.5", "display_name": "GPT-5.5", "graeae_weight": 0.99},
        {"provider": OPEN_WEIGHT_HUB, "model_id": "nemotron-3-ultra-550b-a55b",
         "display_name": "Nemotron", "graeae_weight": 0.5},
        {"provider": "together", "model_id": "deepseek-v3", "display_name": "DeepSeek V3", "graeae_weight": 0.4},
    ]
    install_fake_postgres_backend(monkeypatch, FakePostgresConnection(registry_rows=rows))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=None))

    assert [m.id for m in resp.data] == ["nemotron-3-ultra-550b-a55b"]


# ─── engine defaults can never contain a banned pair ───────────────────────


def _require_graeae():
    pytest.importorskip(
        "mnemos.domain.graeae.engine",
        reason="the GRAEAE add-on is not installed in this environment",
    )


def _default_pairs():
    """(provider, model) pairs from GRAEAE's own built-in provider defaults.

    Read straight from the engine's default-provider factory, so a default
    edited into a banned pair fails here, in-tree, with no service running.
    Interactive providers may bind their model through a callback instead of
    a static id, so every string the callback closes over is checked too.
    """
    _require_graeae()
    from mnemos.domain.openai_compat.providers import get_graeae_engine

    engine = get_graeae_engine()
    pairs = []
    for provider, cfg in engine.providers.items():
        model = cfg.get("model")
        if isinstance(model, str) and model:
            pairs.append((provider, model))
        for builder in (cfg.get("payload_builder"), cfg.get("headers_builder")):
            for cell in getattr(builder, "__closure__", None) or ():
                try:
                    carried = cell.cell_contents
                except ValueError:  # empty cell
                    continue
                if isinstance(carried, bytes):
                    carried = carried.decode("utf-8", "replace")
                if isinstance(carried, str) and carried:
                    for token in re.findall(r"[A-Za-z][A-Za-z0-9._:/-]{2,}", carried):
                        pairs.append((provider, token))
    return pairs


def test_engine_defaults_build_at_least_one_pair():
    _require_graeae()
    assert _default_pairs(), "the engine exposed no provider/model defaults to check"


def test_every_engine_default_pair_passes_check_route():
    _require_graeae()
    pairs = _default_pairs()
    banned = [(provider, model, check_route(provider, model))
              for provider, model in pairs if check_route(provider, model) is not None]
    assert banned == [], f"engine defaults contain banned pairs: {banned}"
