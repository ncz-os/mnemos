"""Provider/model dispatch policy — the locally-enforced route rules.

Some provider/model pairs are *structurally* impossible to serve: a
provider that carries only open-weight models never holds a closed-weight
frontier model, a model owner's own direct route is the only sanctioned
place to dispatch that owner's models, and the closed-weight frontier is
never resold. Dispatch to such a pair used to be attempted anyway and
failed upstream — a round trip and a confusing provider error for a pair
we already knew could not work.

This module is the single source of truth for those rules. It is pure
stdlib, has no I/O and holds no state: ``check_route(provider, model_id)``
returns ``None`` when the pair may be dispatched and a human-readable
reason (naming the rule) when it may not.

Rules
-----
OPEN_WEIGHT_ONLY   A provider that carries only open-weight models may be
                   routed open-weight models only (Nemotron and other
                   open-weight families). Every closed-weight route —
                   the frontier families, the closed-weight route of an
                   open-weight lab, and any third-party model the
                   open-weight hub does not actually carry — is refused.
DEEPSEEK_DIRECT    DeepSeek models are dispatched only through the
                   DeepSeek direct provider: never through an open-weight
                   hub, a reseller, or a cloud marketplace route.
MINIMAX_DIRECT     MiniMax models are dispatched only through MiniMax
                   direct. A closed-weight MiniMax route that happens to
                   exist on an open-weight hub is banned.
CLOSED_FRONTIER    Closed-frontier models (``gpt-*``, ``claude-*``,
                   ``gemini-*``, ``grok-*``) are never routed through a
                   reseller or a cloud marketplace route.

Every rule is *provider-family x model-pattern* data (see
``_ROUTE_RULES``) — no per-model special cases, no network lookups.

Call sites
----------
The rules are applied at the choke points every dispatch passes through,
before any HTTP client is constructed:

* ``mnemos.domain.openai_compat.providers._prepare_provider_route`` — the
  single route-preparation step shared by the blocking dispatch path
  (:func:`..._route_to_provider_response`), the streaming dispatch path
  (:func:`~mnemos.domain.openai_compat.streaming._route_to_provider_stream`)
  and ``chat_completion``. A banned pair raises
  :class:`~mnemos.domain.openai_compat.providers.OpenAICompatError` with
  status 400.
* ``mnemos.core.recommendation.choose_recommended_model`` — PANTHEON's
  catalog, so the router never *recommends* a pair it would then refuse.
* ``mnemos.domain.openai_compat.router.list_models`` — the discovery
  surface, which no longer advertises a banned pair as ``available``.

A provider or model unknown to this module is always allowed, so an
operator-added provider keeps its current behaviour exactly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "OPEN_WEIGHT_ONLY",
    "DEEPSEEK_DIRECT",
    "MINIMAX_DIRECT",
    "CLOSED_FRONTIER",
    "RULES",
    "normalize_provider",
    "normalize_model_id",
    "check_route",
    "is_route_allowed",
]

# Rule ids. Each one is quoted verbatim in the refusal reason so an
# operator reading a 4xx can jump straight to the rule that fired.
OPEN_WEIGHT_ONLY = "open-weight-only-provider"
DEEPSEEK_DIRECT = "deepseek-direct-only"
MINIMAX_DIRECT = "minimax-direct-only"
CLOSED_FRONTIER = "no-frontier-resale"

_RULE_DESCRIPTIONS = {
    OPEN_WEIGHT_ONLY: (
        "a provider that carries only open-weight models may be routed open-weight models only"
    ),
    DEEPSEEK_DIRECT: "DeepSeek models are dispatched only through the DeepSeek direct provider",
    MINIMAX_DIRECT: "MiniMax models are dispatched only through MiniMax direct",
    CLOSED_FRONTIER: "closed-frontier models are never routed through a reseller or a cloud marketplace",
}


def _provider_key(value: str) -> str:
    """Fold a provider id to a comparison key.

    ``nvidia_nim``, ``NVIDIA-NIM`` and ``nvidia.nim`` are the same family;
    ``ngc`` and ``eih`` are distinct ids that each name the same
    open-weight hub. Glued suffixes matter: the historical ids ``nvidia-eih``
    and ``nvidia_eih`` are that hub behind a reseller prefix, so a trailing
    ``eih``/``ngc`` segment is the same family too. The folded key is also
    checked *unseparated* (``nvidianim``) by the caller.
    """
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _segments(key: str) -> tuple[str, ...]:
    return tuple(part for part in key.split("-") if part)


# A provider's folded id is matched against a family name in three ways:
# exactly (``groq``), with its separators dropped (``groqai``), or with the
# family as the id's *leading run of segments* (``groq-eu``,
# ``aws-bedrock-xyz``, ``openrouter-free``). The leading run is what makes a
# deployment-flavoured id reduce to its family: ``aws-bedrock-xyz`` ->
# ``aws-bedrock`` -> ``aws``, ``deepinfra-prod`` -> ``deepinfra``.
#
# Matching is anchored at the head on purpose: a family merely *contained*
# in an id (``my-groq-proxy``, ``acme-bedrock-lab``) is an operator's own
# provider and keeps its current behaviour. Only a trailing ``eih``/``ngc``
# segment is also accepted, because that is how the open-weight hub's
# reseller-prefixed ids historically appeared (``nvidia-eih``).
#
# Family names stay separator-free in the data tables; ``_family_candidates``
# yields the folded forms an id is tested against.


def _family_candidates(key: str, *, suffixes: bool = False) -> tuple[str, ...]:
    """Folded spellings of ``key`` a family name is compared against.

    ``key`` with separators dropped (``groqai``), then the same for each
    leading run of segments (``awsbedrock``, ``aws`` for ``aws-bedrock-xyz``),
    then - only when the rule accepts them - the trailing segment.
    """
    segments = _segments(key)
    candidates = [key.replace("-", "")]
    for end in range(len(segments) - 1, 0, -1):
        candidates.append("".join(segments[:end]))
    if suffixes and len(segments) > 1:
        candidates.append(segments[-1])
    return tuple(candidates)


# --- provider families -------------------------------------------------------

# A provider that carries only open-weight models. The historical ids are
# the hub itself (``nvidia``, ``nvidia_nim``, ``ngc``) and the hub behind a
# reseller prefix (``eih``, ``nvidia-eih``); the trailing-segment form keeps
# the latter in-family.
_OPEN_WEIGHT_HUB_PROVIDERS = frozenset({"nvidia", "nvidianim", "ngc", "eih"})
_OPEN_WEIGHT_HUB_SUFFIXES = frozenset({"nvidia", "nvidianim", "ngc", "eih"})

_DEEPSEEK_DIRECT_PROVIDERS = frozenset({"deepseek", "deepseekdirect", "deepseekai"})
_MINIMAX_DIRECT_PROVIDERS = frozenset({"minimax", "minimaxdirect", "minimaxai"})

# Resellers and cloud marketplace routes: a closed-frontier model is only
# ever dispatched through its owner's own route. Names are separator-free;
# a deployment-flavoured id (``aws-bedrock-xyz``, ``deepinfra-prod``,
# ``openrouter-free``) matches through its leading family.
_RESELLER_PROVIDERS = frozenset(
    {
        # third-party inference resellers
        "together", "groq", "openrouter", "fireworks", "perplexity",
        "deepinfra", "hyperbolic", "novita", "lepton", "anyscale",
        "replicate", "sambanova", "cerebras", "targon", "chutes",
        "parasail", "nebius", "baseten", "edenai", "aimlapi",
        # cloud marketplaces
        "aws", "bedrock", "azure", "azureopenai", "vertex", "vertexai",
        "googlevertex", "gcp", "oci", "ocigenai", "databricks", "sagemaker",
    }
)

# --- model families ----------------------------------------------------------

_FRONTIER_PREFIXES = ("gpt-", "claude-", "gemini-", "grok-")

# Open-weight families a provider that carries only open-weight models may
# serve. Everything else is refused there.
_OPEN_WEIGHT_MODEL_PREFIXES = (
    "nemotron",
    "llama",
    "mistral",
    "mixtral",
    "codestral",
    "qwen",
    "kimi",
    "moonshot",
    "glm",
    "yi-",
    "phi-",
    "gemma",
    "granite",
    "command-",
    "starcoder",
    "solar-",
    "falcon",
    "dbrx",
    "olmo",
    "hermes",
    "minitron",
    "opt-",
    "zamba",
    "snowflake",
    "arctic",
)

# Model ids that name their own lab in the id, so ``nvidia/``-style
# namespace prefixes resolve to the owner's route family rather than to the
# hub that happens to resell copies.
_DEEPSEEK_ID_TOKENS = ("deepseek",)
_MINIMAX_ID_TOKENS = ("minimax", "minimaxai")

# ``org/model`` where ``org`` is a lab whose own route is the only sanctioned
# dispatch path. The pattern is (provider-family, model token).
_LAB_NAMESPACES = {
    "deepseek": _DEEPSEEK_ID_TOKENS,
    "deepseekai": _DEEPSEEK_ID_TOKENS,
    "minimax": _MINIMAX_ID_TOKENS,
    "minimaxai": _MINIMAX_ID_TOKENS,
    "openai": _FRONTIER_PREFIXES,
    "anthropic": _FRONTIER_PREFIXES,
    "google": _FRONTIER_PREFIXES,
    "xai": _FRONTIER_PREFIXES,
}

# Model-id suffixes vendors append to distinguish an access tier, a
# revision-selection mode or a local-cache variant. They are not part of the
# model family, so they are stripped before a prefix check.
_MODEL_ID_SUFFIXES = (
    ":free",
    ":extended",
    ":beta",
    ":online",
    ":latest",
    "-latest",
    "-high",
    "-medium",
    "-low",
    "-reasoning",
    "-instruct",
)


@dataclass(frozen=True)
class RouteRule:
    """One ``provider family x model pattern -> banned`` policy row."""

    rule: str
    providers: frozenset[str]
    provider_suffixes: frozenset[str]
    reason: Optional[str] = None


RULES: tuple[RouteRule, ...] = (
    RouteRule(rule=OPEN_WEIGHT_ONLY, providers=_OPEN_WEIGHT_HUB_PROVIDERS,
              provider_suffixes=_OPEN_WEIGHT_HUB_SUFFIXES),
    RouteRule(rule=DEEPSEEK_DIRECT, providers=_OPEN_WEIGHT_HUB_PROVIDERS | _RESELLER_PROVIDERS,
              provider_suffixes=_OPEN_WEIGHT_HUB_SUFFIXES),
    RouteRule(rule=MINIMAX_DIRECT, providers=_OPEN_WEIGHT_HUB_PROVIDERS | _RESELLER_PROVIDERS,
              provider_suffixes=_OPEN_WEIGHT_HUB_SUFFIXES),
    RouteRule(rule=CLOSED_FRONTIER, providers=_RESELLER_PROVIDERS,
              provider_suffixes=frozenset()),
)


def normalize_provider(provider: str) -> str:
    """Return the folded comparison key for a provider id (``""`` if empty)."""
    return _provider_key(provider)


def normalize_model_id(model_id: str) -> str:
    """Fold a model id for pattern matching.

    Lowercases, drops a ``vendor/`` gateway namespace (``nvidia/gpt-5.5`` ->
    ``gpt-5.5``), drops an access-tier / revision suffix (``:free``,
    ``-latest``) and normalizes ``_``/``.``/whitespace separators to ``-``.
    """
    text = (model_id or "").strip().lower()
    for prefix, _ in _LAB_NAMESPACES.items():
        if text.startswith(f"{prefix}/"):
            text = text[len(prefix) + 1 :]
            break
    else:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"[\s_.]+", "-", text)
    for suffix in _MODEL_ID_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip("-")


def _provider_matches(key: str, families: frozenset[str], suffixes: frozenset[str]) -> bool:
    """True when ``key`` belongs to one of ``families``.

    Family names are separator-free: ``nvidia_nim`` folds to ``nvidia-nim``
    whose dropped form ``nvidianim`` is the family; ``aws-bedrock-xyz``
    reduces to its leading family ``aws``; and, where the rule accepts them,
    a trailing ``eih``/``ngc`` segment keeps reseller-prefixed hub ids
    in-family. Family names are folded here as well, so a dashed spelling
    in a rule table can never silently stop matching.
    """
    if not key:
        return False
    names = {name.replace("-", "") for name in families}
    return any(candidate in names for candidate in _family_candidates(key, suffixes=bool(suffixes)))


def _is_frontier(model: str) -> bool:
    return model.startswith(_FRONTIER_PREFIXES)


def _is_open_weight(model: str) -> bool:
    return model.startswith(_OPEN_WEIGHT_MODEL_PREFIXES)


def _owner_of(model: str) -> Optional[str]:
    """The lab a model id names, when the id names one at all."""
    for lab, tokens in _LAB_NAMESPACES.items():
        if model.startswith(tokens):
            return lab
    return None


def _named_by(model: str, tokens: tuple[str, ...]) -> bool:
    """True when the model id names one of ``tokens`` as its family.

    ``deepseek-v4-flash`` and ``DeepSeek-V4-Flash`` are that family; a
    family merely *containing* the token (``nemotron-deepseek-distill``) is
    not, so a distilled open-weight model keeps its open-weight route.
    """
    return model in tokens or model.startswith(tuple(f"{token}-" for token in tokens))


def _is_deepseek(model: str) -> bool:
    return _named_by(model, _DEEPSEEK_ID_TOKENS)


def _is_minimax(model: str) -> bool:
    return _named_by(model, _MINIMAX_ID_TOKENS)


def _reason(rule: str, provider: str, model_id: str, detail: str) -> str:
    return (
        f"route policy {rule}: {_RULE_DESCRIPTIONS[rule]} — "
        f"refusing to dispatch model {model_id!r} via provider {provider!r} ({detail})"
    )


def check_route(provider: str, model_id: str) -> Optional[str]:
    """Return why ``provider``/``model_id`` may not be dispatched, or None.

    Pure function: no I/O, no caching, no side effects. ``None`` means the
    pair is allowed (including every pair this module knows nothing about).
    """
    provider_key = _provider_key(provider)
    model = normalize_model_id(model_id)
    if not provider_key or not model:
        return None

    if _is_deepseek(model) and not _provider_matches(
        provider_key, _DEEPSEEK_DIRECT_PROVIDERS, frozenset()
    ):
        return _reason(DEEPSEEK_DIRECT, provider, model_id, "DeepSeek model off its direct route")

    if _is_minimax(model) and not _provider_matches(
        provider_key, _MINIMAX_DIRECT_PROVIDERS, frozenset()
    ):
        return _reason(MINIMAX_DIRECT, provider, model_id, "MiniMax model off its direct route")

    if _provider_matches(provider_key, _RESELLER_PROVIDERS, frozenset()) and _is_frontier(model):
        return _reason(CLOSED_FRONTIER, provider, model_id, "closed-frontier model on a resold route")

    if _provider_matches(provider_key, _OPEN_WEIGHT_HUB_PROVIDERS, _OPEN_WEIGHT_HUB_SUFFIXES):
        if not _is_open_weight(model):
            return _reason(
                OPEN_WEIGHT_ONLY,
                provider,
                model_id,
                f"model family not carried by an open-weight-only provider (owner: {_owner_of(model) or 'unknown'})",
            )

    return None


def is_route_allowed(provider: str, model_id: str) -> bool:
    """Boolean form of :func:`check_route` for filtering code paths."""
    return check_route(provider, model_id) is None
