"""Retry backoff timing for PANTHEON (LiteLLM ``_calculate_retry_after`` pattern).

Pure + dependency-free. Computes how long to wait before retrying a transient
failure on the *same* deployment. Two rules carried over from LiteLLM:

  * If a healthy sibling deployment exists, retry **instantly** (0s) — there is
    no reason to wait when another deployment can serve the request now. Backoff
    only matters when this deployment is the last option.
  * Honor a server ``Retry-After`` when it is sane (``0 < ra <= 60`` seconds);
    otherwise use exponential backoff ``INITIAL * 2**attempt`` capped at ``MAX``.

Jitter is added so a fleet of workers retrying the same deployment does not
synchronize into a thundering herd. ``rng`` is injectable (returns a float in
``[0, 1)``) so tests are deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Callable

INITIAL_RETRY_DELAY: float = 0.5
MAX_RETRY_DELAY: float = 8.0
JITTER: float = 0.75
RETRY_AFTER_MAX: float = 60.0


def compute_backoff(
    attempt: int,
    *,
    retry_after: float | None = None,
    has_healthy_siblings: bool = False,
    rng: Callable[[], float] | None = None,
) -> float:
    """Seconds to sleep before retry ``attempt`` (0-based) of one deployment.

    ``attempt`` 0 is the first retry. ``retry_after`` is the server-supplied
    Retry-After header value (seconds), used only when ``0 < retry_after <= 60``.
    ``has_healthy_siblings`` short-circuits to ``0.0`` (retry a sibling now).
    """
    if has_healthy_siblings:
        return 0.0

    draw = rng() if rng is not None else random.random()
    jitter_amount = JITTER * draw

    if retry_after is not None and 0 < retry_after <= RETRY_AFTER_MAX:
        return float(retry_after) + jitter_amount

    delay = min(INITIAL_RETRY_DELAY * (2**attempt), MAX_RETRY_DELAY)
    return delay + jitter_amount
