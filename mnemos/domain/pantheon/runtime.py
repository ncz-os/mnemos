"""PANTHEON router runtime — composes the routing primitives into one entry point.

Ties together:

  * the cooldown breaker (:mod:`mnemos.domain.pantheon.cooldown`) — pre-filter
    cooled-down deployments before selection, and record each call's outcome so
    the breaker can trip,
  * the fallback/retry orchestrator (:mod:`mnemos.domain.pantheon.fallback`) —
    run the surviving deployment chain with the retry/fall-over policy.

This is the object :mod:`mnemos.domain.pantheon.gateway` calls instead of
hand-rolling provider forwarding. It is pure orchestration: the executor, clock,
RNG and sleep are injected, so it is fully unit-testable with no network.

Resilience choices:
  * If every deployment in the group is currently cooled, the runtime tries the
    FULL chain anyway rather than hard-failing — a cooldown is advisory, not a
    hard ban, and serving a slow/recovering model beats serving nothing.
  * ``is_single_deployment_group`` is computed from the ORIGINAL group size (not
    the post-filter subset) so the breaker's "never cool the only model" rule is
    honored correctly even when siblings are temporarily cooled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from mnemos.domain.pantheon.cooldown import DEFAULT_TENANT, CooldownManager
from mnemos.domain.pantheon.errors import NormalizedError
from mnemos.domain.pantheon.fallback import (
    DEFAULT_MAX_FALLBACKS,
    DEFAULT_NUM_RETRIES,
    FallbackResult,
    execute_with_fallbacks,
)


class RouterRuntime:
    """Cooldown-aware execution runtime for a resolved deployment chain."""

    def __init__(
        self,
        cooldown: CooldownManager,
        *,
        clock: Callable[[], float],
        num_retries: int = DEFAULT_NUM_RETRIES,
        max_fallbacks: int = DEFAULT_MAX_FALLBACKS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] | None = None,
    ):
        self._cooldown = cooldown
        self._clock = clock
        self._num_retries = num_retries
        self._max_fallbacks = max_fallbacks
        self._sleep = sleep
        self._rng = rng

    async def route(
        self,
        deployments: Sequence[Any],
        call: Callable[[Any], Awaitable[Any]],
        *,
        classify: Callable[[BaseException], NormalizedError],
        tenant: str = DEFAULT_TENANT,
        key_of: Callable[[Any], str] = str,
    ) -> FallbackResult:
        """Run ``call`` over the model group's ``deployments`` with cooldown
        pre-filtering, retry/fall-over, and per-attempt outcome recording."""
        group = list(deployments)
        if not group:
            raise ValueError("RouterRuntime.route: empty deployment group")

        is_single = len(group) == 1

        def cooled(deployment: Any) -> bool:
            return self._cooldown.is_cooled(key_of(deployment), self._clock(), tenant=tenant)

        available = [d for d in group if not cooled(d)]
        chain = available or group  # all cooled -> try the full chain anyway

        async def instrumented(deployment: Any) -> Any:
            try:
                result = await call(deployment)
            except Exception as exc:  # noqa: BLE001 — record then re-raise for the fallback loop
                now = self._clock()  # observation time, not request start
                err = classify(exc)
                self._cooldown.record_failure(
                    key_of(deployment), err, now, is_single_deployment_group=is_single, tenant=tenant
                )
                raise
            self._cooldown.record_success(key_of(deployment), self._clock(), tenant=tenant)
            return result

        return await execute_with_fallbacks(
            chain,
            instrumented,
            classify=classify,
            num_retries=self._num_retries,
            max_fallbacks=self._max_fallbacks,
            sleep=self._sleep,
            rng=self._rng,
            can_retry=lambda d: not cooled(d),
        )
