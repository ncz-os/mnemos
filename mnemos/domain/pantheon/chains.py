"""Model-group + fallback chain resolution for PANTHEON (LiteLLM model-group pattern).

A *model group* is a routing key shared by one or more interchangeable
*deployments* (provider+model pairs). A *fallback map* names, per group, the
ordered backup groups to try when the primary group's deployments are
exhausted. :func:`resolve_chain` flattens a primary group into the ordered
execution chain :class:`~mnemos.domain.pantheon.runtime.RouterRuntime` consumes:
the primary group's deployments first, then each fallback group's deployments,
de-duplicated by ``(provider, model)`` and bounded.

Pure and config-driven. NOT yet wired into the live router — this is the
mechanism. It is behavior-preserving by construction: a primary group holding a
single deployment with no fallbacks resolves to a one-element chain, i.e. exactly
today's single-provider routing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

GENERIC_FALLBACK_KEY = "*"
DEFAULT_MAX_CHAIN = 6


@dataclass(frozen=True)
class Deployment:
    """One interchangeable provider/model within a model group."""

    group: str
    provider: str
    model: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """Identity for de-duplication across groups."""
        return (self.provider, self.model)


def resolve_chain(
    primary_group: str,
    registry: Mapping[str, Sequence[Deployment]],
    fallbacks: Mapping[str, Sequence[str]] | None = None,
    *,
    max_chain: int = DEFAULT_MAX_CHAIN,
) -> list[Deployment]:
    """Flatten ``primary_group`` into an ordered, de-duplicated deployment chain.

    Order: the primary group's deployments, then the deployments of each group
    named in ``fallbacks[primary_group]`` (in order), then the generic
    ``fallbacks["*"]`` groups. A deployment already present (same provider+model)
    is not added again. The result is truncated to ``max_chain`` deployments.
    Returns ``[]`` for an unknown primary group with no resolvable fallbacks.
    """
    fallbacks = fallbacks or {}
    ordered_groups: list[str] = [primary_group]
    seen_groups: set[str] = {primary_group}

    for group in (*fallbacks.get(primary_group, ()), *fallbacks.get(GENERIC_FALLBACK_KEY, ())):
        if group not in seen_groups and group != primary_group:
            ordered_groups.append(group)
            seen_groups.add(group)

    chain: list[Deployment] = []
    seen_deployments: set[tuple[str, str]] = set()
    for group in ordered_groups:
        for deployment in registry.get(group, ()):
            if deployment.key in seen_deployments:
                continue
            seen_deployments.add(deployment.key)
            chain.append(deployment)
            if len(chain) >= max_chain:
                return chain
    return chain
