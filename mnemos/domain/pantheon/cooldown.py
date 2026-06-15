"""Per-deployment cooldown circuit-breaker for PANTHEON (LiteLLM cooldown pattern).

Three pieces, separated so the decision logic is pure and the storage is a
swappable contract:

  * :func:`evaluate_cooldown` — PURE trip decision. Given the normalized error
    plus this-minute success/failure counts and whether the model group has a
    single deployment, decide whether to trip the breaker. No I/O.
  * :class:`CooldownStore` — the ABSTRACT cache-aside contract every concrete
    backend must satisfy (in-memory here; Oracle/SQLite-backed later). Cooldown
    expiry is a *logical TTL*: the store returns a ``cooled_until`` timestamp and
    the caller compares it to ``now`` — there is no DB-native TTL and no
    per-request DELETE. Counters are minute-bucketed and incremented atomically.
  * :class:`CooldownManager` — ties decision + store + an injected clock.

Cache-aside contract (GRAEAE mandate, mem_1780459842961 §A): a concrete store is
expected to keep a process-local L1 for reads (eventually consistent, bounded
staleness) and flush counter writes write-behind in batches; it must never block
the LLM call path on a synchronous DB round-trip. :class:`InMemoryCooldownStore`
is that L1 with no durable tier — the reference impl and single-process default.

Keys are scoped by ``(tenant, deployment)`` so one tenant's failures (often a
bad BYOK key) never cool another tenant's separate key on the same provider.

Trip rules (LiteLLM ``_is_cooldown_required`` + ``_should_cooldown_deployment``):
the error type must be cooldownable (``NormalizedError.cooldownable`` — never a
plain 400 or an APIConnection fault); a SINGLE-deployment group is never cooled
(removing the only model helps nobody); otherwise trip on a timeout, on a 429,
on a permanent auth/not-found error, or when the failure rate exceeds 50% over
>= 5 requests this minute (or 100% over a high-traffic single-deployment burst).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock

from mnemos.domain.pantheon.errors import ErrorClass, NormalizedError

DEFAULT_COOLDOWN_SECONDS: float = 5.0
FAILURE_THRESHOLD_PERCENT: float = 0.5
FAILURE_THRESHOLD_MIN_REQUESTS: int = 5
HIGH_TRAFFIC_FAILURE_THRESHOLD: int = 1000

# Error classes that are permanent for a deployment/key (worth cooling so the
# router stops hammering a model that structurally cannot serve the request).
_PERMANENT_CLASSES: frozenset[ErrorClass] = frozenset(
    {ErrorClass.AUTHENTICATION, ErrorClass.NOT_FOUND, ErrorClass.PERMISSION_DENIED}
)

DEFAULT_TENANT: str = "_default"


@dataclass(frozen=True)
class CooldownDecision:
    should_cooldown: bool
    cooldown_seconds: float
    reason: str


def evaluate_cooldown(
    err: NormalizedError,
    *,
    successes: int,
    failures: int,
    is_single_deployment_group: bool,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> CooldownDecision:
    """Pure trip decision. ``successes``/``failures`` are this-minute counts for
    the deployment INCLUDING the failure just observed."""
    no = CooldownDecision(False, 0.0, "")

    # Stage 1: the error type must warrant a cooldown at all.
    if not err.cooldownable:
        return no
    # Never remove the only model in a group.
    if is_single_deployment_group:
        return no

    # Stage 2: rate / permanence thresholds.
    if err.error_class is ErrorClass.TIMEOUT:
        return CooldownDecision(True, cooldown_seconds, "timeout")
    if err.error_class is ErrorClass.RATE_LIMIT or err.status_code == 429:
        return CooldownDecision(True, cooldown_seconds, "rate_limit")
    if err.error_class in _PERMANENT_CLASSES:
        return CooldownDecision(True, cooldown_seconds, f"permanent:{err.error_class.value}")

    total = successes + failures
    percent = (failures / total) if total else 0.0
    if percent >= 1.0 and total >= HIGH_TRAFFIC_FAILURE_THRESHOLD:
        return CooldownDecision(True, cooldown_seconds, "all_failing_high_traffic")
    if percent > FAILURE_THRESHOLD_PERCENT and total >= FAILURE_THRESHOLD_MIN_REQUESTS:
        return CooldownDecision(True, cooldown_seconds, "failure_rate")
    return no


class CooldownStore(ABC):
    """Abstract cache-aside contract for cooldown state + minute-bucket counters.

    All methods are keyed by ``(tenant, deployment)``. ``minute`` is an integer
    minute bucket (e.g. ``int(now // 60)``). Cooldown uses a logical TTL: stores
    a ``cooled_until`` epoch-seconds timestamp; readers compare it to the current
    time (expiry == comparison, never a delete).
    """

    @abstractmethod
    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        """Return the epoch-seconds the deployment is cooled until, or ``None``."""

    @abstractmethod
    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        """Record that the deployment is cooled until ``cooled_until``."""

    @abstractmethod
    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        """Atomically increment the success/failure counter for the minute bucket."""

    @abstractmethod
    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        """Return ``(successes, failures)`` for the minute bucket."""


class InMemoryCooldownStore(CooldownStore):
    """Process-local reference store (the L1). Thread-safe, no durable tier."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cooled: dict[tuple[str, str], float] = {}
        self._counts: dict[tuple[str, str, int], list[int]] = {}

    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        with self._lock:
            return self._cooled.get((tenant, deployment))

    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        with self._lock:
            self._cooled[(tenant, deployment)] = cooled_until

    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        with self._lock:
            # Prune stale minute buckets (only the current minute is ever read;
            # keep the previous one for the minute-boundary race) so memory stays
            # bounded in a long-running process.
            cutoff = minute - 1
            for key in [k for k in self._counts if k[2] < cutoff]:
                del self._counts[key]
            bucket = self._counts.setdefault((tenant, deployment, minute), [0, 0])
            bucket[0 if success else 1] += 1

    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        with self._lock:
            bucket = self._counts.get((tenant, deployment, minute), [0, 0])
            return (bucket[0], bucket[1])


class CooldownManager:
    """Records call outcomes, trips the breaker, and reports cooled deployments.

    ``now`` is passed in (or read from the injected ``clock``) so the logical-TTL
    comparison is deterministic in tests and DB-time-driven in production.
    """

    def __init__(self, store: CooldownStore, *, default_cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS):
        self._store = store
        self._default_cooldown = default_cooldown_seconds

    @staticmethod
    def _minute(now: float) -> int:
        return int(now // 60)

    def is_cooled(self, deployment: str, now: float, *, tenant: str = DEFAULT_TENANT) -> bool:
        cooled_until = self._store.get_cooled_until(tenant, deployment)
        return cooled_until is not None and cooled_until > now

    def filter_available(self, deployments: list[str], now: float, *, tenant: str = DEFAULT_TENANT) -> list[str]:
        """Return the deployments not currently cooled (preserving order)."""
        return [d for d in deployments if not self.is_cooled(d, now, tenant=tenant)]

    def record_success(self, deployment: str, now: float, *, tenant: str = DEFAULT_TENANT) -> None:
        self._store.incr(tenant, deployment, self._minute(now), success=True)

    def record_failure(
        self,
        deployment: str,
        err: NormalizedError,
        now: float,
        *,
        is_single_deployment_group: bool,
        tenant: str = DEFAULT_TENANT,
        cooldown_seconds: float | None = None,
    ) -> CooldownDecision:
        """Record a failure, evaluate the breaker, and set a cooldown if tripped.
        Returns the :class:`CooldownDecision`."""
        minute = self._minute(now)
        self._store.incr(tenant, deployment, minute, success=False)
        successes, failures = self._store.get_counts(tenant, deployment, minute)
        decision = evaluate_cooldown(
            err,
            successes=successes,
            failures=failures,
            is_single_deployment_group=is_single_deployment_group,
            cooldown_seconds=cooldown_seconds if cooldown_seconds is not None else self._default_cooldown,
        )
        if decision.should_cooldown:
            self._store.set_cooldown(tenant, deployment, now + decision.cooldown_seconds)
        return decision
