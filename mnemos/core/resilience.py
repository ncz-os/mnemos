from __future__ import annotations

"""GRAEAE resilience primitives with in-process and Redis-backed backends."""

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

_PROVIDER_RPM: dict[str, int] = {
    "perplexity": 50,
    "groq": 60,
    "claude_opus": 40,
    "xai": 30,
    "openai": 60,
    "gemini": 60,
    "nvidia": 50,
    "together": 60,
}
_DEFAULT_RPM = 50

_PROVIDER_SLOTS: dict[str, int] = {
    "perplexity": 3,
    "groq": 4,
    "claude_opus": 3,
    "xai": 3,
    "openai": 3,
    "gemini": 3,
    "nvidia": 3,
    "together": 3,
}
_DEFAULT_SLOTS = 3

_OPEN_CACHE_SECONDS = 0.25


async def maybe_await(value: Any) -> Any:
    """Await coroutine-like values while leaving synchronous fakes compatible."""
    if inspect.isawaitable(value):
        return await value
    return value


async def call_maybe_async(func: Any, *args: Any, **kwargs: Any) -> Any:
    return await maybe_await(func(*args, **kwargs))


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class InProcessCircuitBreaker:
    """Tracks failures for a single provider within one Python process."""

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
        success_threshold: int = 2,
    ):
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.success_threshold = success_threshold
        self.state = CircuitState.CLOSED
        self._failures = 0
        self._probe_successes = 0
        self._opened_at: datetime | None = None
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self._opened_at is None:
                    return False
                elapsed = (datetime.now(timezone.utc) - self._opened_at).total_seconds()
                if elapsed >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self._probe_successes = 0
                    logger.info("[CB] %s: OPEN -> HALF_OPEN", self.provider)
                    return True
                return False
            return True

    def check_open(self) -> bool:
        return not self.is_allowed()

    def record_success(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self._probe_successes += 1
                if self._probe_successes >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    self._failures = 0
                    self._opened_at = None
                    logger.info("[CB] %s: HALF_OPEN -> CLOSED", self.provider)
            elif self.state == CircuitState.CLOSED:
                self._failures = max(0, self._failures - 1)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if self._failures >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self._opened_at = datetime.now(timezone.utc)
                    logger.warning(
                        "[CB] %s: TRIPPED after %d failures",
                        self.provider,
                        self._failures,
                    )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self.state.value, "failures": self._failures}


class InProcessCircuitBreakerPool:
    """Pool of in-process circuit breakers, one per provider."""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 300):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._breakers: dict[str, InProcessCircuitBreaker] = {}

    def _get(self, provider: str) -> InProcessCircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = InProcessCircuitBreaker(
                provider,
                self._failure_threshold,
                self._cooldown_seconds,
            )
        return self._breakers[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def record_success(self, provider: str) -> None:
        self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        self._get(provider).record_failure()

    def status(self) -> dict[str, dict[str, Any]]:
        return {provider: breaker.status() for provider, breaker in self._breakers.items()}


_NATS_CB_BUCKET = "MNEMOS_PANTHEON_DISPATCH"
_NATS_CB_PREFIX = "circuit."
_NATS_CB_DECAY_SECONDS = 300
_NATS_SYNC_TIMEOUT_SECONDS = 0.25
_NATS_CAS_ATTEMPTS = 16
_NATS_STABLE_FALLBACK_SECRET = b"mnemos-nats-circuit-breaker-key-v1"


def _nats_missing_key(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    return "notfound" in name or "not found" in msg or "no keys" in msg


def _nats_wrong_revision(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    return "wronglast" in name or "wrong last" in msg or "wrong last sequence" in msg


def _nats_entry_value(entry: Any) -> bytes:
    value = getattr(entry, "value", entry)
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _nats_entry_revision(entry: Any) -> int | None:
    revision = getattr(entry, "revision", None)
    if revision is None:
        revision = getattr(entry, "rev", None)
    return int(revision) if revision is not None else None


def _nats_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


async def _nats_maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _nats_kv_put(kv: Any, key: str, value: bytes) -> Any:
    # nats-py KV operations do not accept per-key TTL. Expiry is configured
    # on the bucket via KeyValueConfig(ttl=...).
    return await _nats_maybe_await(kv.put(key, value))


async def _nats_kv_create(kv: Any, key: str, value: bytes) -> Any:
    return await _nats_maybe_await(kv.create(key, value))


async def _nats_kv_update(kv: Any, key: str, value: bytes, revision: int) -> Any:
    return await _nats_maybe_await(kv.update(key, value, last=revision))


class _NatsLoopThread:
    """Owns the asyncio loop and any NATS connections created by NATS resilience."""

    def __init__(self, *, name: str = "mnemos-nats-resilience") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._owned_connections: list[Any] = []
        self._lock = threading.Lock()
        self._closed = False
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any) -> Future:
        with self._lock:
            if self._closed:
                close = getattr(coro, "close", None)
                if close is not None:
                    close()
                raise RuntimeError("NATS resilience loop is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def add_connection(self, nc: Any) -> None:
        with self._lock:
            self._owned_connections.append(nc)

    async def _shutdown(self) -> None:
        tasks = [task for task in asyncio.all_tasks(self._loop) if task is not asyncio.current_task(self._loop)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            connections = list(self._owned_connections)
            self._owned_connections.clear()
        for nc in connections:
            try:
                drain = getattr(nc, "drain", None)
                if drain is not None:
                    await _nats_maybe_await(drain())
                close = getattr(nc, "close", None)
                if close is not None:
                    await _nats_maybe_await(close())
            except Exception:
                logger.exception("NATS resilience connection close failed")

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=timeout)
        except Exception as exc:
            logger.debug("NATS resilience loop shutdown did not finish cleanly: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("NATS resilience loop did not stop within %.1fs", timeout)
            return
        self._loop.close()


class NatsCircuitBreaker:
    """JetStream KV-backed circuit breaker shared across gateway workers."""

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str,
        failure_threshold: int,
        cooldown_seconds: int,
        *,
        bucket: str = _NATS_CB_BUCKET,
        settings: Any | None = None,
        sync_timeout: float = _NATS_SYNC_TIMEOUT_SECONDS,
    ):
        self.key_prefix = key_prefix
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.bucket = bucket
        self._settings = settings
        self._sync_timeout = sync_timeout
        self._loop = _NatsLoopThread()
        self._kv_future = self._loop.submit(self._ensure_kv(kv_or_js))
        self._open_cache: dict[str, float] = {}
        self._last_status: dict[str, dict[str, Any]] = {}
        self._local_failures: dict[str, int] = defaultdict(int)
        self._local_fallback = InProcessCircuitBreakerPool(
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )
        self._state_lock = threading.Lock()

    async def _ensure_kv(self, source: Any | None) -> Any | None:
        if source is None:
            source = await self._connect_jetstream()
        if source is None:
            return None
        if all(hasattr(source, name) for name in ("get", "put")):
            return source
        try:
            return await _nats_maybe_await(source.key_value(self.bucket))
        except Exception as exc:
            if not _nats_missing_key(exc):
                logger.debug("NATS KV lookup for %s failed; attempting create: %s", self.bucket, exc)
        bucket_ttl = max(int(self.cooldown_seconds), _NATS_CB_DECAY_SECONDS)
        try:
            from nats.js.api import KeyValueConfig  # type: ignore[import-not-found]

            return await _nats_maybe_await(
                source.create_key_value(config=KeyValueConfig(bucket=self.bucket, history=1, ttl=bucket_ttl))
            )
        except ImportError:
            return await _nats_maybe_await(source.create_key_value(bucket=self.bucket, ttl=bucket_ttl))
        except TypeError:
            return await _nats_maybe_await(source.create_key_value(self.bucket))
        except Exception as exc:
            if _nats_missing_key(exc):
                raise
            return await _nats_maybe_await(source.key_value(self.bucket))

    async def _connect_jetstream(self) -> Any | None:
        if self._settings is not None:
            settings = self._settings
        else:
            from mnemos.core.config import get_settings

            settings = get_settings()
        nats_settings = getattr(settings, "nats", None)
        url = getattr(nats_settings, "url", None)
        if not url:
            return None
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("nats-py not installed; NATS resilience backend unavailable")
            return None
        try:
            kwargs: dict[str, Any] = {"servers": [url]}
            token = getattr(nats_settings, "token", None)
            if token:
                kwargs["token"] = token
            nc = await nats.connect(**kwargs)
            self._loop.add_connection(nc)
            return nc.jetstream()
        except Exception as exc:
            logger.warning("NATS resilience backend connect failed: %s", exc)
            return None

    async def _kv(self) -> Any | None:
        return await asyncio.wrap_future(self._kv_future)

    def _key_secret(self) -> bytes:
        if self._settings is not None:
            settings = self._settings
        else:
            try:
                from mnemos.core.config import get_settings

                settings = get_settings()
            except Exception:
                settings = None
        configured = getattr(getattr(settings, "pantheon", None), "nats_key_secret", "") if settings else ""
        if configured:
            return str(configured).encode("utf-8")
        token = getattr(getattr(settings, "nats", None), "token", None) if settings else None
        if token:
            return str(token).encode("utf-8")
        return _NATS_STABLE_FALLBACK_SECRET

    def _key(self, provider: str) -> str:
        digest = hmac.new(self._key_secret(), provider.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{self.key_prefix}{_NATS_CB_PREFIX}{digest}"

    def _cache_open(self, provider: str) -> None:
        with self._state_lock:
            self._open_cache[provider] = time.monotonic() + min(_OPEN_CACHE_SECONDS, float(self.cooldown_seconds))

    def _clear_cache(self, provider: str) -> None:
        with self._state_lock:
            self._open_cache.pop(provider, None)

    def _set_status(self, provider: str, status: dict[str, Any]) -> None:
        with self._state_lock:
            self._last_status[provider] = status

    def cached_open(self, provider: str) -> bool:
        with self._state_lock:
            expires_at = self._open_cache.get(provider)
            if expires_at is None:
                return False
            if expires_at <= time.monotonic():
                self._open_cache.pop(provider, None)
                return False
            return True

    async def _read(self, provider: str) -> tuple[dict[str, Any] | None, int | None]:
        kv = await self._kv()
        if kv is None:
            return None, None
        try:
            entry = await _nats_maybe_await(kv.get(self._key(provider)))
        except Exception as exc:
            if _nats_missing_key(exc):
                return None, None
            raise
        return json.loads(_nats_entry_value(entry).decode("utf-8")), _nats_entry_revision(entry)

    async def _run_on_loop(self, coro: Any) -> Any:
        return await asyncio.wrap_future(self._loop.submit(coro))

    async def _run_bounded(self, coro: Any, fallback: Any = None) -> Any:
        fut = self._loop.submit(coro)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=self._sync_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            fut.cancel()
            logger.warning("NATS circuit breaker operation timed out; using local fallback")
            return fallback

    async def check_open(self, provider: str) -> bool:
        if self.cached_open(provider):
            self._set_status(provider, {"state": CircuitState.OPEN.value, "failures": None})
            return True
        result = await self._run_bounded(self._check_open(provider), fallback=None)
        if result is None:
            return not self._local_fallback.is_allowed(provider)
        return bool(result)

    async def _check_open(self, provider: str) -> bool:
        kv = await self._kv()
        if kv is None:
            return None
        payload, _revision = await self._read(provider)
        now = time.time()
        if payload and payload.get("state") == CircuitState.OPEN.value:
            opened_at = float(payload.get("opened_at_epoch", 0.0))
            if opened_at + self.cooldown_seconds > now:
                self._cache_open(provider)
                self._set_status(
                    provider,
                    {"state": CircuitState.OPEN.value, "failures": int(payload.get("failures", 0))},
                )
                return True
            self._set_status(
                provider,
                {"state": CircuitState.HALF_OPEN.value, "failures": int(payload.get("failures", 0))},
            )
            self._clear_cache(provider)
            return False
        self._set_status(
            provider,
            {"state": CircuitState.CLOSED.value, "failures": int(payload.get("failures", 0)) if payload else 0},
        )
        self._clear_cache(provider)
        return False

    async def record_failure(self, provider: str) -> None:
        with self._state_lock:
            self._local_failures[provider] += 1
        self._local_fallback.record_failure(provider)
        await self._run_bounded(self._record_failure(provider), fallback=None)

    async def _record_failure(self, provider: str) -> None:
        key = self._key(provider)
        kv = await self._kv()
        if kv is None:
            return
        now = time.time()
        for _attempt in range(_NATS_CAS_ATTEMPTS):
            payload, revision = await self._read(provider)
            if payload and payload.get("state") == CircuitState.OPEN.value:
                opened_at = float(payload.get("opened_at_epoch", 0.0))
                if opened_at + self.cooldown_seconds > now:
                    self._cache_open(provider)
                    self._set_status(
                        provider,
                        {"state": CircuitState.OPEN.value, "failures": int(payload.get("failures", 0))},
                    )
                    return
            failures = int(payload.get("failures", 0)) + 1 if payload else 1
            opened = failures >= self.failure_threshold
            new_payload: dict[str, Any] = {
                "state": CircuitState.OPEN.value if opened else CircuitState.CLOSED.value,
                "failures": failures,
                "updated_at_epoch": now,
            }
            if opened:
                new_payload["opened_at_epoch"] = now
                new_payload["opened_at"] = datetime.now(timezone.utc).isoformat()
            try:
                if revision is None:
                    try:
                        await _nats_kv_create(kv, key, _nats_json(new_payload))
                    except AttributeError:
                        await _nats_kv_put(kv, key, _nats_json(new_payload))
                else:
                    await _nats_kv_update(kv, key, _nats_json(new_payload), revision)
                self._set_status(provider, {"state": new_payload["state"], "failures": failures})
                if opened:
                    self._cache_open(provider)
                    logger.warning("[CB] %s: TRIPPED in NATS", provider)
                else:
                    self._clear_cache(provider)
                return
            except Exception as exc:
                if not _nats_wrong_revision(exc):
                    raise
        raise RuntimeError(f"NATS circuit breaker CAS failed for {provider}")

    async def record_success(self, provider: str) -> None:
        await self._run_bounded(self._record_success(provider), fallback=None)
        self._local_fallback.record_success(provider)

    async def _record_success(self, provider: str) -> None:
        key = self._key(provider)
        kv = await self._kv()
        if kv is None:
            return
        now = time.time()
        for _attempt in range(_NATS_CAS_ATTEMPTS):
            payload, revision = await self._read(provider)
            if payload and payload.get("state") == CircuitState.OPEN.value:
                opened_at = float(payload.get("opened_at_epoch", 0.0))
                if opened_at + self.cooldown_seconds > now:
                    self._cache_open(provider)
                    self._set_status(
                        provider,
                        {"state": CircuitState.OPEN.value, "failures": int(payload.get("failures", 0))},
                    )
                    return
            failures = max(0, int(payload.get("failures", 0)) - 1) if payload else 0
            new_payload = {"state": CircuitState.CLOSED.value, "failures": failures, "updated_at_epoch": now}
            try:
                if revision is None:
                    try:
                        await _nats_kv_create(kv, key, _nats_json(new_payload))
                    except AttributeError:
                        await _nats_kv_put(kv, key, _nats_json(new_payload))
                else:
                    await _nats_kv_update(kv, key, _nats_json(new_payload), revision)
                self._clear_cache(provider)
                self._set_status(provider, {"state": CircuitState.CLOSED.value, "failures": failures})
                return
            except Exception as exc:
                if not _nats_wrong_revision(exc):
                    raise
        raise RuntimeError(f"NATS circuit breaker success CAS failed for {provider}")

    def status(self, provider: str) -> dict[str, Any]:
        with self._state_lock:
            status = self._last_status.get(provider)
            failures = self._local_failures.get(provider)
        if status is not None:
            return dict(status)
        return {
            "state": CircuitState.OPEN.value if self.cached_open(provider) else CircuitState.CLOSED.value,
            "failures": failures,
        }

    def close(self) -> None:
        self._loop.close()


class NatsCircuitBreakerPool:
    """Circuit breaker pool backed by NATS JetStream KV state."""

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str = "cb.",
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
        *,
        bucket: str = _NATS_CB_BUCKET,
        settings: Any | None = None,
    ):
        self._breaker = NatsCircuitBreaker(
            kv_or_js,
            key_prefix,
            failure_threshold,
            cooldown_seconds,
            bucket=bucket,
            settings=settings,
        )
        self._providers: set[str] = set()
        self._providers_lock = threading.Lock()

    def _remember(self, provider: str) -> None:
        with self._providers_lock:
            self._providers.add(provider)

    async def is_allowed(self, provider: str) -> bool:
        self._remember(provider)
        return not await self._breaker.check_open(provider)

    async def record_success(self, provider: str) -> None:
        self._remember(provider)
        await self._breaker.record_success(provider)

    async def record_failure(self, provider: str) -> None:
        self._remember(provider)
        await self._breaker.record_failure(provider)

    def status(self) -> dict[str, dict[str, Any]]:
        with self._providers_lock:
            providers = sorted(self._providers)
        return {provider: self._breaker.status(provider) for provider in providers}

    def close(self) -> None:
        self._breaker.close()


class InProcessRateLimiter:
    """Sliding-window rate limiter for a single provider in one process."""

    def __init__(self, provider: str, rpm: int):
        self.provider = provider
        self.rpm = rpm
        self._window = 60.0
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            self._timestamps = [timestamp for timestamp in self._timestamps if timestamp > cutoff]
            if len(self._timestamps) >= self.rpm:
                logger.warning("[RL] %s: rate limit reached (%d rpm)", self.provider, self.rpm)
                return False
            self._timestamps.append(now)
            return True

    def current_rpm(self) -> int:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            return sum(1 for timestamp in self._timestamps if timestamp > cutoff)


class InProcessRateLimiterPool:
    """Pool of in-process rate limiters, one per provider."""

    def __init__(self, overrides: dict[str, int] | None = None):
        limits = {**_PROVIDER_RPM, **(overrides or {})}
        self._limiters: dict[str, InProcessRateLimiter] = {
            provider: InProcessRateLimiter(provider, rpm)
            for provider, rpm in limits.items()
        }

    def _get(self, provider: str) -> InProcessRateLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = InProcessRateLimiter(provider, _DEFAULT_RPM)
        return self._limiters[provider]

    def is_allowed(self, provider: str) -> bool:
        return self._get(provider).is_allowed()

    def status(self) -> dict[str, int]:
        return {provider: limiter.current_rpm() for provider, limiter in self._limiters.items()}


# NATS rate-limiter constants
_NATS_RATE_LIMITER_BUCKET = "MNEMOS_GRAEAE_DISPATCH"
_NATS_RATE_LIMITER_PREFIX = "rl:"
_NATS_RATE_LIMITER_RETRIES = 8


_NATS_VIS_EPOCH_BUCKET = "mnemos_vis_epoch"
_VIS_EPOCH_KEY = "vis:epoch"

# How coarse the degraded epoch is when the shared store is unreachable.
# Bounds how long a stale entry can survive; see NatsVisibilityEpoch.
_VIS_EPOCH_DEGRADED_BUCKET_SECS = 5


class VisibilityEpochBumpFailed(RuntimeError):
    """Raised when a visibility-narrowing mutation could not advance the epoch.

    Callers must treat this as "the cache may still serve pre-revocation
    results" -- it is a security-relevant failure, not a cache miss.
    """


class NatsVisibilityEpoch:
    """Monotonic visibility/ACL epoch shared across workers, backed by NATS KV.

    Every visibility-narrowing mutation (delete, archive, permission tighten,
    ACL revoke) bumps this counter, and search-cache keys embed the value read
    at request start. An in-flight write that lands after a bump is stored under
    the old epoch -- orphaned, never read -- which is what closes the
    write-after-invalidate window.

    Replaces a Redis INCR. Redis gives an atomic counter for free; JetStream KV
    does not, so the bump is a compare-and-swap retry loop against the entry
    revision, the same shape the other NATS primitives here use.

    DEGRADATION IS NOT `return 0`. The counter is a revocation-freshness
    control: if it stops moving, narrowed permissions keep being served from
    cache for the full TTL (300s) and the bug this exists to fix is silently
    back. A fixed fallback fails OPEN, which is the wrong direction for a
    security control.

    So when the shared store is unreachable we fall back to a coarse time
    bucket. Cache keys then rotate every few seconds regardless of any bump,
    which costs some hit rate and bounds staleness at
    ``_VIS_EPOCH_DEGRADED_BUCKET_SECS`` instead of the TTL. Degraded mode is
    strictly worse for performance and strictly safer for correctness, which is
    the correct trade for this particular counter.
    """

    def __init__(
        self,
        kv_or_js: Any | None,
        *,
        bucket: str = _NATS_VIS_EPOCH_BUCKET,
        settings: Any | None = None,
    ):
        self.bucket = bucket
        self._settings = settings
        self._loop = _NatsLoopThread(name="mnemos-nats-epoch")
        self._kv_future = self._loop.submit(self._ensure_kv(kv_or_js))
        # High-water mark across BOTH substrates, so neither a degraded->live
        # transition nor a live->degraded one can move the epoch backward.
        self._floor = 0
        self._floor_lock = threading.Lock()

    def _degraded(self) -> int:
        # Derived from the clock so every worker agrees without coordinating.
        #
        # OFFSET so a degraded value can never collide with, or fall below, a
        # persisted counter. Without this, recovery moves the epoch BACKWARD:
        # degraded returns ~3.5e8 (a time bucket) while the persisted counter is
        # a small integer, so the first successful read after NATS returns hands
        # out a LOWER epoch than callers already saw -- and cache entries written
        # under the higher degraded epoch become readable again. Monotonicity is
        # the whole contract of this counter.
        bucket = int(time.time()) // _VIS_EPOCH_DEGRADED_BUCKET_SECS
        with self._floor_lock:
            self._floor = max(self._floor, bucket)
            return self._floor

    def _observe(self, value: int) -> int:
        """Record a persisted epoch and return a never-decreasing view of it.

        A persisted counter that is lower than a degraded value already handed
        out would otherwise un-orphan cache entries written during the outage.
        """
        with self._floor_lock:
            self._floor = max(self._floor, value)
            return self._floor

    async def _kv(self) -> Any | None:
        try:
            return await asyncio.wrap_future(self._kv_future)
        except Exception:
            return None

    async def _ensure_kv(self, source: Any | None) -> Any | None:
        if source is None:
            source = await self._connect_jetstream()
        if source is None:
            return None
        try:
            return await _nats_maybe_await(source.key_value(self.bucket))
        except Exception:
            pass
        try:
            return await _nats_maybe_await(source.create_key_value(bucket=self.bucket, history=1))
        except Exception:
            pass
        try:
            return await _nats_maybe_await(source.key_value(self.bucket))
        except Exception:
            return None

    async def _connect_jetstream(self) -> Any | None:
        if not _nats_configured(self._settings):
            return None
        try:
            import nats  # noqa: PLC0415

            url = getattr(getattr(self._settings, "nats", None), "url", None)
            conn = await nats.connect(url) if url else await nats.connect()
            return conn.jetstream()
        except Exception:
            return None

    async def current(self) -> int:
        """Read the epoch without bumping it."""
        kv = await self._kv()
        if kv is None:
            return self._degraded()
        try:
            entry = await _nats_maybe_await(kv.get(_VIS_EPOCH_KEY))
            return self._observe(int(json.loads(_nats_entry_value(entry).decode("utf-8"))["epoch"]))
        except Exception as exc:
            if _nats_missing_key(exc):
                return 0
            return self._degraded()

    async def bump(self, *, attempts: int = 8) -> int:
        """Increment the epoch and return the new value.

        CAS-with-retry: read entry + revision, write back only if the revision
        still matches. A concurrent bump loses the race and retries, so two
        workers revoking at once cannot both write the same new value.
        """
        kv = await self._kv()
        if kv is None:
            return self._degraded()
        try:
            start_epoch = await self.current()
        except Exception:
            start_epoch = -1
        for _ in range(attempts):
            try:
                entry = await _nats_maybe_await(kv.get(_VIS_EPOCH_KEY))
            except Exception as exc:
                if not _nats_missing_key(exc):
                    return self._degraded()
                try:
                    await _nats_kv_create(kv, _VIS_EPOCH_KEY, _nats_json({"epoch": 1}))
                    return self._observe(1)
                except Exception:
                    continue  # lost the create race; re-read and CAS
            revision = _nats_entry_revision(entry)
            try:
                nxt = int(json.loads(_nats_entry_value(entry).decode("utf-8"))["epoch"]) + 1
            except Exception:
                nxt = 1
            if revision is None:
                try:
                    await _nats_kv_put(kv, _VIS_EPOCH_KEY, _nats_json({"epoch": nxt}))
                    return nxt
                except Exception:
                    return self._degraded()
            try:
                await _nats_kv_update(kv, _VIS_EPOCH_KEY, _nats_json({"epoch": nxt}), revision)
                return self._observe(nxt)
            except Exception as exc:
                if _nats_wrong_revision(exc):
                    continue
                return self._degraded()
        # Retry budget exhausted while KV is otherwise HEALTHY. This is the
        # dangerous case, and returning a degraded value silently was wrong:
        # the caller discards the return, later reads call current() which
        # succeeds against the still-healthy store, and they get the OLD
        # persisted epoch -- so a stale search result written before the
        # revocation stays readable. That is the exact TOCTOU bug this class
        # exists to close, reappearing under contention.
        #
        # Re-read and only accept advancement. If the epoch moved (a competing
        # bump won the race), the invalidation we wanted has effectively
        # happened and we can return it. If it did NOT move, we could not
        # invalidate, and the caller must not be told everything is fine.
        try:
            observed = await self.current()
        except Exception:
            observed = None
        if observed is not None and observed > start_epoch:
            return observed
        raise VisibilityEpochBumpFailed(
            "could not advance the visibility epoch after "
            f"{attempts} attempts; caches may still serve pre-revocation results"
        )


class NatsRateLimiter:
    """Sliding-window rate limiter backed by NATS JetStream KV.

    Uses CAS-with-retry to serialize counter updates across workers.
    Each provider has a dedicated KV key; the entry is a JSON object
    carrying the request count and window-start timestamp.  When the
    configured RPM is reached, subsequent requests within the window
    are rejected until the window advances.

    When NATS KV is unavailable (connection error, missing bucket, etc.)
    the limiter degrades gracefully (returns True).
    """

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str,
        rpm: int,
        *,
        bucket: str = _NATS_RATE_LIMITER_BUCKET,
        settings: Any | None = None,
    ):
        self.key_prefix = key_prefix
        self.rpm = rpm
        self.bucket = bucket
        self._settings = settings
        self._loop = _NatsLoopThread(name="mnemos-nats-rl")
        self._kv_future = self._loop.submit(self._ensure_kv(kv_or_js))
        self._seen: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    async def _ensure_kv(self, source: Any | None) -> Any | None:
        if source is None:
            source = await self._connect_jetstream()
        if source is None:
            return None
        if all(hasattr(source, name) for name in ("get", "put")):
            return source
        try:
            return await _nats_maybe_await(source.key_value(self.bucket))
        except Exception as exc:
            if not _nats_missing_key(exc):
                logger.debug("NATS KV lookup for %s failed; attempting create: %s", self.bucket, exc)
        try:
            from nats.js.api import KeyValueConfig  # type: ignore[import-not-found]

            return await _nats_maybe_await(
                source.create_key_value(config=KeyValueConfig(bucket=self.bucket, history=1, ttl=120))
            )
        except ImportError:
            return await _nats_maybe_await(source.create_key_value(bucket=self.bucket, ttl=120))
        except TypeError:
            return await _nats_maybe_await(source.create_key_value(self.bucket))
        except Exception as exc:
            if _nats_missing_key(exc):
                raise
            return await _nats_maybe_await(source.key_value(self.bucket))

    async def _connect_jetstream(self) -> Any | None:
        if self._settings is not None:
            settings = self._settings
        else:
            from mnemos.core.config import get_settings

            settings = get_settings()
        nats_settings = getattr(settings, "nats", None)
        url = getattr(nats_settings, "url", None)
        if not url:
            return None
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("nats-py not installed; NATS rate-limiter backend unavailable")
            return None
        try:
            kwargs: dict[str, Any] = {"servers": [url]}
            token = getattr(nats_settings, "token", None)
            if token:
                kwargs["token"] = token
            nc = await nats.connect(**kwargs)
            self._loop.add_connection(nc)
            return nc.jetstream()
        except Exception as exc:
            logger.warning("NATS rate-limiter backend connect failed: %s", exc)
            return None

    async def _kv(self) -> Any | None:
        return await asyncio.wrap_future(self._kv_future)

    def _key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}"

    async def acquire(self, provider: str) -> bool:
        with self._lock:
            self._seen[provider] += 1
        kv = await self._kv()
        if kv is None:
            # Degraded: allow request when KV unavailable
            return True
        key = self._key(provider)
        return await self._acquire_with_retry(kv, key, provider)

    async def _acquire_with_retry(self, kv: Any, key: str, provider: str) -> bool:
        now = time.time()
        for _attempt in range(_NATS_RATE_LIMITER_RETRIES):
            try:
                entry = await _nats_maybe_await(kv.get(key))
            except Exception:
                entry = None

            if entry is None:
                # First call for this key — create it with count=1
                new_payload = {"count": 1, "window_start": now}
                revision = None
            else:
                payload = json.loads(_nats_entry_value(entry).decode("utf-8"))
                revision = _nats_entry_revision(entry)
                window_start = payload.get("window_start", now)
                count = payload.get("count", 0)
                # Reset window if expired (1 minute sliding window)
                if now - window_start >= 60:
                    window_start = now
                    count = 0
                count += 1
                new_payload = {"window_start": window_start, "count": count}

            try:
                if revision is None:
                    try:
                        await _nats_kv_create(kv, key, _nats_json(new_payload))
                    except AttributeError:
                        await _nats_kv_put(kv, key, _nats_json(new_payload))
                else:
                    await _nats_kv_update(kv, key, _nats_json(new_payload), revision)
            except Exception as exc:
                if not _nats_wrong_revision(exc):
                    raise
                continue  # retry with fresh revision
            # CAS succeeded — check if we exceeded the limit
            if new_payload.get("count", 0) > self.rpm:
                logger.warning("[RL] %s: NATS rate limit reached (%d rpm)", provider, self.rpm)
                return False
            return True
        logger.warning("[RL] %s: CAS retries exhausted; allowing request", provider)
        return True

    def status(self) -> dict[str, int]:
        return dict(self._seen)

    def close(self) -> None:
        self._loop.close()


class NatsRateLimiterPool:
    """Rate limiter pool backed by NATS JetStream KV state.

    Shares a single NatsRateLimiter instance so that all pool methods
    atomically read/write the same KV key.
    """

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str = "rl:",
        overrides: dict[str, int] | None = None,
        *,
        bucket: str = _NATS_RATE_LIMITER_BUCKET,
        settings: Any | None = None,
    ):
        self._limiter = NatsRateLimiter(
            kv_or_js,
            key_prefix,
            _DEFAULT_RPM,
            bucket=bucket,
            settings=settings,
        )
        self._providers: set[str] = set()
        self._providers_lock = threading.Lock()
        self._overrides = overrides or {}

    def _remember(self, provider: str) -> None:
        with self._providers_lock:
            self._providers.add(provider)

    async def is_allowed(self, provider: str) -> bool:
        self._remember(provider)
        # Delegate directly to the shared limiter — no per-call instance
        # creation. The limiter's KV is shared across all pool instances
        # that share the same key_prefix.
        self._limiter._seen[provider] += 1
        kv = await self._limiter._kv()
        if kv is None:
            return True
        limiter = NatsRateLimiter(
            None,
            self._limiter.key_prefix,
            self._get_rpm(provider),
            bucket=self._limiter.bucket,
            settings=self._limiter._settings,
        )
        limiter._seen = self._limiter._seen
        limiter._lock = self._limiter._lock
        limiter._loop = self._limiter._loop
        limiter._kv_future = self._limiter._kv_future
        return await limiter._acquire_with_retry(kv, limiter._key(provider), provider)

    def _get_rpm(self, provider: str) -> int:
        return self._overrides.get(provider, _PROVIDER_RPM.get(provider, _DEFAULT_RPM))

    async def acquire(self, provider: str) -> bool:
        return await self.is_allowed(provider)

    def status(self) -> dict[str, int]:
        return dict(self._limiter._seen)

    def close(self) -> None:
        self._limiter.close()


class InProcessProviderConcurrencyLimiter:
    """asyncio.Semaphore-backed slot limiter for one provider."""

    def __init__(self, provider: str, max_concurrent: int):
        self.provider = provider
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0

    def is_available(self) -> bool:
        return self._sem._value > 0  # type: ignore[attr-defined]

    async def acquire(self) -> bool:
        if self.is_available():
            await self._sem.acquire()
            self._in_flight += 1
            return True
        logger.info("[CONC] %s: all %d slots occupied; skipping", self.provider, self.max_concurrent)
        return False

    def release(self) -> None:
        self._sem.release()
        self._in_flight = max(0, self._in_flight - 1)

    def status(self) -> dict[str, int]:
        return {"in_flight": self._in_flight, "max": self.max_concurrent}


class InProcessConcurrencyLimiterPool:
    """Pool of in-process concurrency limiters, one per provider."""

    def __init__(self, overrides: dict[str, int] | None = None):
        slots = {**_PROVIDER_SLOTS, **(overrides or {})}
        self._limiters: dict[str, InProcessProviderConcurrencyLimiter] = {
            provider: InProcessProviderConcurrencyLimiter(provider, max_concurrent)
            for provider, max_concurrent in slots.items()
        }

    def _get(self, provider: str) -> InProcessProviderConcurrencyLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = InProcessProviderConcurrencyLimiter(provider, _DEFAULT_SLOTS)
        return self._limiters[provider]

    def is_available(self, provider: str) -> bool:
        return self._get(provider).is_available()

    async def acquire(self, provider: str) -> bool:
        return await self._get(provider).acquire()

    def release(self, provider: str) -> None:
        self._get(provider).release()

    @asynccontextmanager
    async def reserve(self, provider: str):
        acquired = await self.acquire(provider)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(provider)

    def status(self) -> dict[str, dict[str, int]]:
        return {provider: limiter.status() for provider, limiter in self._limiters.items()}


# NATS concurrency-limiter constants
_NATS_CONCURRENCY_LIMITER_BUCKET = "MNEMOS_GRAEAE_DISPATCH"
_NATS_CONCURRENCY_LIMITER_PREFIX = "conc:"
_NATS_CONCURRENCY_LIMITER_RETRIES = 8


class NatsConcurrencyLimiter:
    """Slot-based concurrency limiter backed by NATS JetStream KV.

    Each provider has a dedicated KV key whose value is an integer
    representing the number of *available* slots.  ``acquire`` reads the
    current count, and attempts to atomically decrement it via CAS.
    ``release`` atomically increments the count back.

    When NATS KV is unavailable, the limiter degrades gracefully by
    allowing requests (no-op).
    """

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str,
        max_concurrent: int,
        *,
        bucket: str = _NATS_CONCURRENCY_LIMITER_BUCKET,
        settings: Any | None = None,
    ):
        self.key_prefix = key_prefix
        self.max_concurrent = max_concurrent
        self.bucket = bucket
        self._settings = settings
        self._loop = _NatsLoopThread(name="mnemos-nats-conc")
        self._kv_future = self._loop.submit(self._ensure_kv(kv_or_js))
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = defaultdict(int)

    async def _ensure_kv(self, source: Any | None) -> Any | None:
        if source is None:
            source = await self._connect_jetstream()
        if source is None:
            return None
        if all(hasattr(source, name) for name in ("get", "put")):
            return source
        try:
            return await _nats_maybe_await(source.key_value(self.bucket))
        except Exception as exc:
            if not _nats_missing_key(exc):
                logger.debug("NATS KV lookup for %s failed; attempting create: %s", self.bucket, exc)
        try:
            from nats.js.api import KeyValueConfig  # type: ignore[import-not-found]

            return await _nats_maybe_await(
                source.create_key_value(config=KeyValueConfig(bucket=self.bucket, history=1, ttl=300))
            )
        except ImportError:
            return await _nats_maybe_await(source.create_key_value(bucket=self.bucket, ttl=300))
        except TypeError:
            return await _nats_maybe_await(source.create_value(self.bucket))
        except Exception as exc:
            if _nats_missing_key(exc):
                raise
            return await _nats_maybe_await(source.key_value(self.bucket))

    async def _connect_jetstream(self) -> Any | None:
        if self._settings is not None:
            settings = self._settings
        else:
            from mnemos.core.config import get_settings

            settings = get_settings()
        nats_settings = getattr(settings, "nats", None)
        url = getattr(nats_settings, "url", None)
        if not url:
            return None
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("nats-py not installed; NATS concurrency-limiter backend unavailable")
            return None
        try:
            kwargs: dict[str, Any] = {"servers": [url]}
            token = getattr(nats_settings, "token", None)
            if token:
                kwargs["token"] = token
            nc = await nats.connect(**kwargs)
            self._loop.add_connection(nc)
            return nc.jetstream()
        except Exception as exc:
            logger.warning("NATS concurrency-limiter backend connect failed: %s", exc)
            return None

    async def _kv(self) -> Any | None:
        return await asyncio.wrap_future(self._kv_future)

    def _key(self, provider: str) -> str:
        return f"{self.key_prefix}{provider}"

    async def acquire(self, provider: str) -> str | None:
        with self._lock:
            self._in_flight[provider] += 1
        kv = await self._kv()
        if kv is None:
            # Degraded: allow request when KV unavailable
            return str(uuid.uuid4().hex)
        key = self._key(provider)
        token = str(uuid.uuid4().hex)
        for _attempt in range(_NATS_CONCURRENCY_LIMITER_RETRIES):
            try:
                entry = await _nats_maybe_await(kv.get(key))
            except Exception:
                entry = None
            if entry is None:
                # First acquire for this key — create with max_concurrent-1 available
                new_payload = {"available": self.max_concurrent - 1}
                revision = None
            else:
                payload = json.loads(_nats_entry_value(entry).decode("utf-8"))
                available = payload.get("available", self.max_concurrent)
                if available <= 0:
                    with self._lock:
                        self._in_flight[provider] -= 1
                    logger.info(
                        "[CONC] %s: all %d NATS slots occupied; skipping",
                        provider,
                        self.max_concurrent,
                    )
                    return None
                new_payload = {"available": available - 1}
                revision = _nats_entry_revision(entry)
            try:
                if revision is None:
                    try:
                        await _nats_kv_create(kv, key, _nats_json(new_payload))
                    except AttributeError:
                        await _nats_kv_put(kv, key, _nats_json(new_payload))
                    # CAS succeeded for new key
                    return token
                else:
                    await _nats_kv_update(kv, key, _nats_json(new_payload), revision)
                    return token
            except Exception as exc:
                if not _nats_wrong_revision(exc):
                    raise
                continue  # retry with fresh revision
        # Exhausted retries: read fresh state and decide
        try:
            entry = await _nats_maybe_await(kv.get(key))
        except Exception:
            return token
        payload = json.loads(_nats_entry_value(entry).decode("utf-8"))
        if payload.get("available", 0) > 0:
            return token
        with self._lock:
            self._in_flight[provider] -= 1
        return None

    async def release(self, provider: str, token: str) -> None:
        with self._lock:
            self._in_flight[provider] = max(0, self._in_flight.get(provider, 0) - 1)
        kv = await self._kv()
        if kv is None:
            return
        key = self._key(provider)
        for _attempt in range(_NATS_CONCURRENCY_LIMITER_RETRIES):
            try:
                entry = await _nats_maybe_await(kv.get(key))
            except Exception:
                return
            payload = json.loads(_nats_entry_value(entry).decode("utf-8"))
            new_payload = {"available": payload.get("available", 0) + 1}
            revision = _nats_entry_revision(entry)
            try:
                if revision is None:
                    try:
                        await _nats_kv_create(kv, key, _nats_json(new_payload))
                    except AttributeError:
                        await _nats_kv_put(kv, key, _nats_json(new_payload))
                else:
                    await _nats_kv_update(kv, key, _nats_json(new_payload), revision)
                return
            except Exception as exc:
                if not _nats_wrong_revision(exc):
                    raise

    @asynccontextmanager
    async def reserve(self, provider: str):
        token = await self.acquire(provider)
        try:
            yield token is not None
        finally:
            if token is not None:
                await self.release(provider, token)

    def close(self) -> None:
        self._loop.close()


class NatsConcurrencyLimiterPool:
    """Concurrency limiter pool backed by NATS JetStream KV state.

    Delegates all acquire/release/status calls to the shared limiter
    instance so that KV state is read consistently across all pool
    instances.
    """

    def __init__(
        self,
        kv_or_js: Any | None,
        key_prefix: str = "conc:",
        overrides: dict[str, int] | None = None,
        *,
        bucket: str = _NATS_CONCURRENCY_LIMITER_BUCKET,
        settings: Any | None = None,
    ):
        self._limiter = NatsConcurrencyLimiter(
            kv_or_js,
            key_prefix,
            _DEFAULT_SLOTS,
            bucket=bucket,
            settings=settings,
        )
        self._providers: set[str] = set()
        # provider -> outstanding acquire tokens, so release() gives back the
        # slot it actually took.
        self._tokens: dict[str, list[str]] = {}
        self._token_lock = threading.Lock()
        self._overrides = overrides or {}

    def _max_concurrent(self, provider: str) -> int:
        return self._overrides.get(provider, _PROVIDER_SLOTS.get(provider, _DEFAULT_SLOTS))

    def _remember(self, provider: str) -> None:
        self._providers.add(provider)

    def is_available(self, provider: str) -> bool:
        self._remember(provider)
        kv = asyncio.get_event_loop().run_until_complete(self._limiter._kv()) if True else None
        return kv is not None

    async def acquire(self, provider: str) -> bool:
        self._remember(provider)
        # Create a temporary limiter that shares KV with the pool's limiter
        limiter = NatsConcurrencyLimiter(
            None,
            self._limiter.key_prefix,
            self._max_concurrent(provider),
            bucket=self._limiter.bucket,
            settings=self._limiter._settings,
        )
        limiter._kv_future = self._limiter._kv_future
        limiter._loop = self._limiter._loop
        limiter._in_flight = self._limiter._in_flight
        limiter._lock = self._limiter._lock
        token = await limiter.acquire(provider)
        if token is None:
            return False
        # The underlying limiter is TOKEN-based: release(provider, token) is how
        # a slot is given back. Collapsing the token to a bool here and then
        # releasing with "" meant every release incremented the available-slot
        # count whether or not it matched an acquire, so a double release -- or
        # a release on a path that never acquired -- raised capacity above max
        # and quietly disabled the limiter.
        with self._token_lock:
            self._tokens.setdefault(provider, []).append(token)
        return True

    async def release(self, provider: str) -> None:
        self._remember(provider)
        limiter = NatsConcurrencyLimiter(
            None,
            self._limiter.key_prefix,
            self._max_concurrent(provider),
            bucket=self._limiter.bucket,
            settings=self._limiter._settings,
        )
        limiter._kv_future = self._limiter._kv_future
        limiter._loop = self._limiter._loop
        limiter._in_flight = self._limiter._in_flight
        limiter._lock = self._limiter._lock
        with self._token_lock:
            token = self._tokens.get(provider, []).pop() if self._tokens.get(provider) else None
        if token is None:
            # Nothing outstanding for this provider. Releasing anyway would
            # inflate the slot count past max. Stay silent-but-safe: the caller
            # double-released, which is a bug in the caller, not a reason to
            # corrupt the limiter for everyone else.
            return
        await limiter.release(provider, token)

    @asynccontextmanager
    async def reserve(self, provider: str):
        limiter = NatsConcurrencyLimiter(
            None,
            self._limiter.key_prefix,
            self._max_concurrent(provider),
            bucket=self._limiter.bucket,
            settings=self._limiter._settings,
        )
        limiter._kv_future = self._limiter._kv_future
        limiter._loop = self._limiter._loop
        limiter._in_flight = self._limiter._in_flight
        limiter._lock = self._limiter._lock
        # Yield the acquisition result. The previous version yielded None, so
        # `async with pool.reserve(p) as ok:` silently bound ok=None and every
        # caller testing it treated a refused slot as success.
        async with limiter.reserve(provider) as acquired:
            yield acquired

    def status(self) -> dict[str, dict[str, int]]:
        return {
            provider: {
                "in_flight": self._limiter._in_flight.get(provider, 0),
                "max": self._max_concurrent(provider),
            }
            for provider in sorted(self._providers)
        }

    def close(self) -> None:
        self._limiter.close()


def _nats_configured(settings: Any) -> bool:
    return bool(getattr(getattr(settings, "nats", None), "url", None))


def _fallback_warning_enabled(settings: Any) -> bool:
    resilience = getattr(settings, "resilience", None)
    return bool(getattr(resilience, "fallback_warning", True))


def _warn_fallback(settings: Any, reason: str) -> None:
    if _fallback_warning_enabled(settings):
        logger.warning(
            "%s; falling back to in-process resilience primitives.",
            reason,
        )


def make_circuit_breaker_pool(
    settings: Any,
    *,
    failure_threshold: int = 5,
    cooldown_seconds: int = 300,
    nats_kv: Any | None = None,
) -> InProcessCircuitBreakerPool | NatsCircuitBreakerPool:
    if _nats_configured(settings):
        return NatsCircuitBreakerPool(
            nats_kv,
            getattr(settings.resilience, "circuit_breaker_nats_prefix", "cb."),
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            settings=settings,
        )
    else:
        _warn_fallback(settings, "NATS not configured")
    return InProcessCircuitBreakerPool(
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )


def make_rate_limiter_pool(
    settings: Any,
    *,
    overrides: dict[str, int] | None = None,
    nats_kv: Any | None = None,
) -> InProcessRateLimiterPool | NatsRateLimiterPool:
    if _nats_configured(settings):
        prefix = getattr(settings.resilience, "rate_limiter_nats_prefix", "rl:")
        return NatsRateLimiterPool(
            nats_kv,
            prefix,
            overrides=overrides,
            settings=settings,
        )
    else:
        _warn_fallback(settings, "NATS not configured")
    return InProcessRateLimiterPool(overrides=overrides)


def make_concurrency_limiter(
    settings: Any,
    *,
    overrides: dict[str, int] | None = None,
    nats_kv: Any | None = None,
) -> InProcessConcurrencyLimiterPool | NatsConcurrencyLimiterPool:
    if _nats_configured(settings):
        prefix = getattr(settings.resilience, "concurrency_nats_prefix", "conc:")
        return NatsConcurrencyLimiterPool(
            nats_kv,
            prefix,
            overrides=overrides,
            settings=settings,
        )
    else:
        _warn_fallback(settings, "NATS not configured")
    return InProcessConcurrencyLimiterPool(overrides=overrides)


CircuitBreaker = InProcessCircuitBreaker
CircuitBreakerPool = InProcessCircuitBreakerPool
RateLimiter = InProcessRateLimiter
RateLimiterPool = InProcessRateLimiterPool
ProviderConcurrencyLimiter = InProcessProviderConcurrencyLimiter
ConcurrencyLimiterPool = InProcessConcurrencyLimiterPool
