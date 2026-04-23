"""Per-endpoint circuit breaker for GPU-backed compression engines.

Why this module exists (and what it is NOT):

  * It IS circuit-breaker + CPU-fallback coordination. Engines that
    declare `gpu_required` or `gpu_optional` in their
    `CompressionEngine.gpu_intent` consult a GPUGuard before making
    their HTTP call; if the guard's circuit is open (too many recent
    failures), the engine fast-fails with a structured error instead
    of piling a doomed request onto a dead endpoint.

  * It is NOT a request batcher. True HTTP-level batching (accumulate
    N concurrent compress calls, flush as a single /v1/completions
    with a list prompt) is a v3.2 optimization. Modern inference
    servers (vLLM, Ollama) already batch concurrent requests
    internally at the model layer, so the v3.1 win-over-do-nothing is
    the correctness work — fail fast when the endpoint is down, route
    gpu_required engines around outages, track recovery — not the
    throughput work.

Circuit states:

                +------------------+                +---------------+
                |                  | fail_count  >= |               |
   success -->  |      CLOSED      | ------------>  |      OPEN     |
                |                  | threshold      |               |
                +------------------+                +-------+-------+
                        ^                                   |
                        | success on probe                  | cooldown
                        |                                   |  elapsed
                        |         +----------------+        |
                        +-------- |   HALF_OPEN    | <------+
                                  |                |
                                  +----------------+
                                          |
                                          | failure on probe
                                          v
                                    back to OPEN

  * CLOSED     — healthy; requests flow through, successes reset the
                 counter, failures increment it.
  * OPEN       — circuit tripped; `is_available()` returns False
                 until the cooldown window elapses. Engines fast-fail
                 or route around.
  * HALF_OPEN  — cooldown elapsed; exactly ONE probe request is
                 admitted. While that probe is in flight, concurrent
                 `is_available()` calls return False so the possibly-
                 still-broken endpoint isn't flooded. Probe success
                 transitions back to CLOSED; probe failure re-opens
                 the circuit for another cooldown window. If a probe
                 is in flight longer than `probe_timeout_seconds`
                 (caller crashed / was cancelled without recording
                 success/failure), the flag is abandoned and the
                 next call is admitted as a fresh probe.

The guard is per-endpoint (keyed by URL) and lives in a process-local
registry. Multiple engines sharing the same `GPU_PROVIDER_HOST` share
one guard — one ALETHEIA timeout informs ANAMNESIS that the endpoint
is unresponsive without ANAMNESIS having to time out too.

For v3.2 horizontal-scaling work, the registry becomes a Redis-backed
shared-state singleton. v3.1 is single-worker per the DEPLOYMENT.md
scaling note; process-local state is correct for the constraint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class GuardConfig:
    """Tunables for a GPUGuard.

    Defaults are conservative for the single-worker v3.1 deployment:
    three consecutive failures open the circuit for 30 seconds. Operators
    who run against flaky remote endpoints can relax these; operators
    who want stricter failure isolation can tighten them.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    # Upper bound on how long a single HALF_OPEN probe can stay
    # "in flight" before the next caller is admitted as a fresh probe.
    # Covers the case where the probe caller is cancelled (asyncio
    # timeout higher in the stack) or otherwise fails to invoke
    # record_success / record_failure. Default 120s is ~4x the default
    # cooldown and roomy above a realistic GPU handler timeout (~30s).
    probe_timeout_seconds: float = 120.0
    # Minimum time between state-transition log messages. Prevents log
    # floods when many concurrent tasks hit a recently-opened circuit.
    log_throttle_seconds: float = 5.0


class GPUGuard:
    """Per-endpoint circuit breaker with async coordination.

    Thread/task safety: all state mutation goes through an asyncio.Lock.
    `is_available()` is a cheap read but takes the lock briefly to
    compute the cooldown-elapsed check atomically with a possible
    state transition to HALF_OPEN.

    Intended usage from an engine's compress() method:

        guard = get_guard(self._core.gpu_url)
        if not await guard.is_available():
            return CompressionResult(... error="gpu_guard: circuit open ...")
        try:
            result = await self._core.extract_facts(...)
        except Exception as exc:
            await guard.record_failure(exc)
            raise
        else:
            await guard.record_success()
            return result
    """

    def __init__(
        self,
        endpoint: str,
        config: Optional[GuardConfig] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.config = config or GuardConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._last_log_at: float = 0.0
        self._last_error: Optional[str] = None
        # Single-probe coordination: when the circuit transitions
        # OPEN -> HALF_OPEN, exactly one concurrent caller is admitted
        # as the probe. Subsequent concurrent callers see
        # _probe_in_flight and fast-fail until the probe resolves.
        self._probe_in_flight: bool = False
        self._probe_started_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    async def is_available(self) -> bool:
        """True if the caller may proceed with a request.

        State-dependent admission:
          * CLOSED:    always admit.
          * OPEN:      admit iff cooldown has elapsed (the admitted
                       caller becomes the probe; state transitions to
                       HALF_OPEN).
          * HALF_OPEN: admit iff no probe currently in flight
                       (single-probe guarantee) OR the prior probe
                       has been "in flight" longer than
                       `cooldown_seconds` (caller abandoned — admit a
                       fresh probe).
        """
        async with self._lock:
            now = time.monotonic()

            if self._state is CircuitState.OPEN:
                if self._opened_at is None:
                    # Shouldn't happen, but stay defensive.
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    self._probe_started_at = now
                    return True
                if now - self._opened_at >= self.config.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    self._probe_started_at = now
                    self._log_throttled(
                        "circuit HALF_OPEN: probe request may now proceed "
                        "against %s",
                        self.endpoint,
                    )
                    return True
                return False

            if self._state is CircuitState.HALF_OPEN:
                if not self._probe_in_flight:
                    # Probe slot is free (prior probe resolved to
                    # HALF_OPEN without re-admitting us? defensive).
                    self._probe_in_flight = True
                    self._probe_started_at = now
                    return True
                # A probe is in flight. Admit only if it has been
                # outstanding longer than `probe_timeout_seconds` —
                # at that point we treat the original caller as
                # abandoned (cancelled / crashed / never called
                # record_*) and let this caller re-probe. Otherwise
                # fast-fail to protect the endpoint from concurrent
                # probes (the "single-probe" guarantee).
                if (
                    self._probe_started_at is not None
                    and now - self._probe_started_at
                    >= self.config.probe_timeout_seconds
                ):
                    logger.warning(
                        "gpu_guard[%s]: prior probe abandoned after %.1fs, "
                        "admitting new probe",
                        self.endpoint,
                        now - self._probe_started_at,
                    )
                    self._probe_started_at = now
                    return True
                return False

            # CLOSED
            return True

    async def record_success(self) -> None:
        """Note a successful request. Resets failure counter; if the
        circuit was HALF_OPEN, transitions back to CLOSED. Clears the
        single-probe in-flight flag so future OPEN -> HALF_OPEN
        transitions can admit a new probe."""
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                logger.info(
                    "gpu_guard[%s]: probe succeeded, circuit CLOSED",
                    self.endpoint,
                )
                self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._last_error = None
            self._probe_in_flight = False
            self._probe_started_at = None

    async def record_failure(self, exc: Optional[BaseException] = None) -> None:
        """Note a failure. Increments the failure counter; if the counter
        crosses the threshold (or if we were HALF_OPEN), OPEN the circuit.
        Clears the single-probe in-flight flag on HALF_OPEN resolution."""
        async with self._lock:
            self._last_error = (
                f"{type(exc).__name__}: {exc}" if exc is not None else "unspecified"
            )

            if self._state is CircuitState.HALF_OPEN:
                # Probe failed — immediately re-open for another cooldown window.
                self._opened_at = time.monotonic()
                self._state = CircuitState.OPEN
                self._probe_in_flight = False
                self._probe_started_at = None
                logger.warning(
                    "gpu_guard[%s]: probe failed, circuit re-OPEN (%s)",
                    self.endpoint,
                    self._last_error,
                )
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                if self._state is not CircuitState.OPEN:
                    self._opened_at = time.monotonic()
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "gpu_guard[%s]: %d consecutive failures, circuit OPEN "
                        "for %.0fs (%s)",
                        self.endpoint,
                        self._consecutive_failures,
                        self.config.cooldown_seconds,
                        self._last_error,
                    )

    def _log_throttled(self, fmt: str, *args) -> None:
        """Emit a log message at most once per log_throttle_seconds."""
        now = time.monotonic()
        if now - self._last_log_at >= self.config.log_throttle_seconds:
            logger.info(fmt, *args)
            self._last_log_at = now

    def snapshot(self) -> Dict[str, object]:
        """Diagnostic snapshot for the manifest-read endpoint / admin UI."""
        return {
            "endpoint": self.endpoint,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
            "last_error": self._last_error,
            "probe_in_flight": self._probe_in_flight,
            "probe_started_at": self._probe_started_at,
        }

    def reset(self) -> None:
        """Force the circuit back to CLOSED. Operator escape hatch — not
        used by engine code. Synchronous; caller is responsible for
        coordinating with in-flight requests."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._last_error = None
        self._probe_in_flight = False
        self._probe_started_at = None


# ---- process-local registry -------------------------------------------------


_GUARDS: Dict[str, GPUGuard] = {}
_REGISTRY_LOCK = asyncio.Lock()


def get_guard(endpoint: str, config: Optional[GuardConfig] = None) -> GPUGuard:
    """Return the process-local guard for `endpoint`. Constructs on first
    access with the supplied `config` (or GuardConfig defaults)."""
    endpoint_key = endpoint.rstrip("/")
    existing = _GUARDS.get(endpoint_key)
    if existing is not None:
        return existing
    created = GPUGuard(endpoint_key, config=config)
    _GUARDS[endpoint_key] = created
    return created


def all_guards() -> Dict[str, GPUGuard]:
    """Read-only view of the registry — for diagnostics / admin surfaces."""
    return dict(_GUARDS)


def reset_registry() -> None:
    """Clear the registry. Test-only helper. Do not call from engine code."""
    _GUARDS.clear()


__all__ = [
    "CircuitState",
    "GuardConfig",
    "GPUGuard",
    "get_guard",
    "all_guards",
    "reset_registry",
]
