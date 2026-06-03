"""Cross-group fallback + in-group retry loop for PANTHEON (LiteLLM Router pattern).

Pure async orchestration: given an ordered ``chain`` of deployments and an async
``call`` that executes one deployment, try them in order with the retry/fall-over
policy from :mod:`mnemos.domain.pantheon.errors` and the timing from
:mod:`mnemos.domain.pantheon.backoff`. All side-effecting collaborators (the
executor, the clock, the RNG, the error classifier) are injected, so the loop is
fully unit-testable with no network and no real sleeping.

Control flow per deployment (LiteLLM ``async_function_with_retries`` nested in
``async_function_with_fallbacks``):

  * success            -> return immediately.
  * RETRY (transient)  -> back off, retry the SAME deployment up to
                          ``num_retries`` times, then fall over.
  * FALLOVER           -> advance to the next deployment immediately (no retry).
  * RAISE (terminal,
    nowhere to fall to) -> raise ``AllDeploymentsFailed``.

Bounded by ``max_fallbacks`` deployments tried. The load-bearing guarantee: a
non-retryable error (a plain 400) NEVER retries — it falls over instantly — so
one broken lead model cannot cascade and stall the group (the real outage).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from mnemos.domain.pantheon.backoff import compute_backoff
from mnemos.domain.pantheon.errors import NormalizedError, RetryAction, decide

DEFAULT_NUM_RETRIES: int = 2
DEFAULT_MAX_FALLBACKS: int = 5


@dataclass(frozen=True)
class AttemptRecord:
    """One failed attempt: which deployment, what it failed with, what we did."""

    deployment: Any
    error: NormalizedError
    action: RetryAction
    retry_index: int


@dataclass(frozen=True)
class FallbackResult:
    """A successful execution and the trail of failures that preceded it."""

    result: Any
    deployment: Any
    attempts: tuple[AttemptRecord, ...] = field(default_factory=tuple)


class AllDeploymentsFailed(Exception):
    """Every eligible deployment failed. Carries the full attempt trail and the
    last underlying exception."""

    def __init__(self, attempts: Sequence[AttemptRecord], last_exception: BaseException | None):
        self.attempts = tuple(attempts)
        self.last_exception = last_exception
        classes = ", ".join(a.error.error_class.value for a in self.attempts) or "none"
        super().__init__(f"all deployments failed after {len(self.attempts)} attempt(s): [{classes}]")


def _default_retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    return value if isinstance(value, (int, float)) else None


async def execute_with_fallbacks(
    chain: Sequence[Any],
    call: Callable[[Any], Awaitable[Any]],
    *,
    classify: Callable[[BaseException], NormalizedError],
    num_retries: int = DEFAULT_NUM_RETRIES,
    max_fallbacks: int = DEFAULT_MAX_FALLBACKS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] | None = None,
    retry_after_of: Callable[[BaseException], float | None] = _default_retry_after,
    can_retry: Callable[[Any], bool] | None = None,
) -> FallbackResult:
    """Run ``call`` over ``chain`` with retry+fallover; return the first success.

    ``classify`` maps a raised exception to a :class:`NormalizedError`. Raises
    :class:`AllDeploymentsFailed` if every eligible deployment fails (or a
    terminal error occurs with nowhere to fall over to). Raises ``ValueError``
    on an empty chain.
    """
    deployments = list(chain)[: max(0, max_fallbacks)]
    if not deployments:
        raise ValueError("execute_with_fallbacks: empty (or zero-bounded) deployment chain")

    attempts: list[AttemptRecord] = []
    last_exc: BaseException | None = None

    for idx, deployment in enumerate(deployments):
        has_siblings = idx < len(deployments) - 1
        num_remaining = len(deployments) - idx
        retry = 0
        while True:
            try:
                result = await call(deployment)
                return FallbackResult(result=result, deployment=deployment, attempts=tuple(attempts))
            except Exception as exc:  # noqa: BLE001 — orchestrator must observe every provider failure
                last_exc = exc
                err = classify(exc)
                action = decide(err, num_deployments=num_remaining, has_fallbacks=has_siblings)
                attempts.append(AttemptRecord(deployment, err, action, retry))

                if action is RetryAction.RAISE:
                    raise AllDeploymentsFailed(attempts, last_exc) from exc
                if action is RetryAction.FALLOVER:
                    break  # advance to next deployment
                # action is RETRY
                if retry >= num_retries:
                    break  # retries exhausted -> fall over to next deployment
                if can_retry is not None and not can_retry(deployment):
                    break  # deployment no longer usable (e.g. just cooled) -> fall over
                delay = compute_backoff(
                    retry,
                    retry_after=retry_after_of(exc),
                    has_healthy_siblings=has_siblings,
                    rng=rng,
                )
                await sleep(delay)
                retry += 1

    raise AllDeploymentsFailed(attempts, last_exc)
