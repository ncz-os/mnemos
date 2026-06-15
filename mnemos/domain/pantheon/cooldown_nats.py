"""NATS JetStream KV-backed PANTHEON cooldown store.

The base :class:`~mnemos.domain.pantheon.cooldown.CooldownStore` contract is
synchronous for compatibility, but ``RouterRuntime`` uses this store's native
async methods on the request path so JetStream KV reads/increments never block
the running event loop on ``Future.result()``. JetStream KV remains the
authoritative shared state when NATS is configured, while the in-process
fallback remains unchanged when ``MNEMOS_NATS_URL`` is unset because gateway
wiring simply does not construct this store.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any

from mnemos.core.config import get_settings
from mnemos.domain.pantheon.cooldown import CooldownStore, InMemoryCooldownStore

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "MNEMOS_PANTHEON_COOLDOWN_COUNTERS"
DEFAULT_COOLDOWN_BUCKET = "MNEMOS_PANTHEON_COOLDOWNS"
_PENDING_LIMIT = 1024
_COUNTER_TTL_SECONDS = 180
_SYNC_TIMEOUT_SECONDS = 0.25
_CAS_ATTEMPTS = 16
_STABLE_FALLBACK_SECRET = b"mnemos-pantheon-nats-kv-key-v1"


def _secret() -> bytes:
    settings = get_settings()
    configured = getattr(getattr(settings, "pantheon", None), "nats_key_secret", "") or os.getenv(
        "MNEMOS_PANTHEON_NATS_KEY_SECRET", ""
    )
    if configured:
        return str(configured).encode("utf-8")
    token = getattr(getattr(settings, "nats", None), "token", None)
    if token:
        return str(token).encode("utf-8")
    return _STABLE_FALLBACK_SECRET


def _opaque(value: str) -> str:
    return hmac.new(_secret(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _cooldown_key(tenant: str, deployment: str) -> str:
    return f"cooldown.{_opaque(tenant)}.{_opaque(deployment)}"


def _counter_key(tenant: str, deployment: str, minute: int) -> str:
    return f"counter.{int(minute)}.{_opaque(tenant)}.{_opaque(deployment)}"


def _missing_key(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    return "notfound" in name or "not found" in msg or "no keys" in msg


def _wrong_revision(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    return "wronglast" in name or "wrong last" in msg or "wrong last sequence" in msg


def _entry_value(entry: Any) -> bytes:
    value = getattr(entry, "value", entry)
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _entry_revision(entry: Any) -> int | None:
    revision = getattr(entry, "revision", None)
    if revision is None:
        revision = getattr(entry, "rev", None)
    return int(revision) if revision is not None else None


def _loads(entry: Any) -> dict[str, Any]:
    return json.loads(_entry_value(entry).decode("utf-8"))


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _kv_put(kv: Any, key: str, value: bytes) -> Any:
    # nats-py KV operations do not accept per-key TTL. Expiry is configured
    # on the bucket via KeyValueConfig(ttl=...).
    return await _maybe_await(kv.put(key, value))


async def _kv_create(kv: Any, key: str, value: bytes) -> Any:
    return await _maybe_await(kv.create(key, value))


async def _kv_update(kv: Any, key: str, value: bytes, revision: int) -> Any:
    return await _maybe_await(kv.update(key, value, last=revision))


class _NatsLoopThread:
    """Owns one asyncio loop and any NATS connection created by this store."""

    def __init__(self, *, name: str = "pantheon-nats-kv") -> None:
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
                raise RuntimeError("PANTHEON NATS loop is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def add_connection(self, nc: Any) -> None:
        with self._lock:
            if self._closed:
                close = getattr(nc, "close", None)
                if close is not None:
                    result = close()
                    if hasattr(result, "__await__"):
                        self.submit(result)
                return
            self._owned_connections.append(nc)

    async def _shutdown(self) -> None:
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task(self._loop)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for nc in self._owned_connections:
            try:
                drain = getattr(nc, "drain", None)
                if drain is not None:
                    await _maybe_await(drain())
                close = getattr(nc, "close", None)
                if close is not None:
                    await _maybe_await(close())
            except Exception:
                logger.exception("NATS connection close failed")
        self._owned_connections.clear()

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=timeout)
        except Exception as exc:
            logger.debug("NATS loop shutdown did not finish cleanly: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("NATS loop did not stop within %.1fs", timeout)
            return
        self._loop.close()


class NatsJetStreamCooldownStore(CooldownStore):
    """CooldownStore backed by authoritative shared JetStream KV state."""

    def __init__(
        self,
        *,
        js: Any | None = None,
        bucket: str = DEFAULT_BUCKET,
        cooldown_bucket: str = DEFAULT_COOLDOWN_BUCKET,
        l1: InMemoryCooldownStore | None = None,
        connect_timeout: float = 1.0,
        sync_timeout: float = _SYNC_TIMEOUT_SECONDS,
        counter_ttl_seconds: int = _COUNTER_TTL_SECONDS,
    ) -> None:
        self._bucket_name = bucket
        self._cooldown_bucket_name = cooldown_bucket
        self._l1 = l1 if l1 is not None else InMemoryCooldownStore()
        self._loop = _NatsLoopThread()
        self._pending: set[Future] = set()
        self._pending_lock = threading.Lock()
        self._connect_timeout = connect_timeout
        self._sync_timeout = sync_timeout
        self._counter_ttl_seconds = counter_ttl_seconds
        self._ready_counter = self._track(self._loop.submit(self._ensure_kv(js, self._bucket_name, counter_ttl_seconds)))
        self._ready_cooldown = self._track(self._loop.submit(self._ensure_kv(js, self._cooldown_bucket_name, 86400)))

    async def _ensure_kv(self, js: Any | None, bucket: str, ttl_seconds: int) -> Any | None:
        if js is None:
            js = await self._connect_jetstream()
        if js is None:
            logger.warning("PANTHEON NATS cooldown store unavailable; durable sharing disabled")
            return None
        return await self._get_or_create_bucket(js, bucket, ttl_seconds)

    async def _connect_jetstream(self) -> Any | None:
        settings = get_settings()
        if not settings.nats.url:
            return None
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("nats-py not installed; PANTHEON NATS cooldown store disabled")
            return None
        try:
            kwargs: dict[str, Any] = {"servers": [settings.nats.url], "connect_timeout": self._connect_timeout}
            if settings.nats.token:
                kwargs["token"] = settings.nats.token
            nc = await nats.connect(**kwargs)
            self._loop.add_connection(nc)
            return nc.jetstream()
        except Exception as exc:
            logger.warning("PANTHEON NATS cooldown connect failed: %s", exc)
            return None

    async def _get_or_create_bucket(self, js: Any, bucket: str, ttl_seconds: int) -> Any:
        if all(hasattr(js, name) for name in ("get", "put")):
            return js
        try:
            return await _maybe_await(js.key_value(bucket))
        except Exception as exc:
            if not _missing_key(exc):
                logger.debug("NATS KV lookup for %s failed; attempting create: %s", bucket, exc)
        bucket_ttl = max(int(ttl_seconds), 1)
        try:
            from nats.js.api import KeyValueConfig  # type: ignore[import-not-found]

            return await _maybe_await(
                js.create_key_value(
                    config=KeyValueConfig(bucket=bucket, history=1, ttl=bucket_ttl)
                )
            )
        except ImportError:
            return await _maybe_await(js.create_key_value(bucket=bucket, ttl=bucket_ttl))
        except TypeError:
            return await _maybe_await(js.create_key_value(bucket))

    async def _counter_kv(self) -> Any | None:
        try:
            return await asyncio.wrap_future(self._ready_counter)
        except Exception as exc:
            logger.warning("PANTHEON NATS cooldown counter KV unavailable: %s", exc)
            return None

    async def _cooldown_kv(self) -> Any | None:
        try:
            return await asyncio.wrap_future(self._ready_cooldown)
        except Exception as exc:
            logger.warning("PANTHEON NATS cooldown KV unavailable: %s", exc)
            return None

    def _track(self, fut: Future) -> Future:
        with self._pending_lock:
            if len(self._pending) >= _PENDING_LIMIT:
                logger.warning("PANTHEON NATS cooldown pending queue full; durable op will not delay flush")
            else:
                self._pending.add(fut)
        fut.add_done_callback(self._done)
        return fut

    def _done(self, fut: Future) -> None:
        with self._pending_lock:
            self._pending.discard(fut)
        try:
            fut.result()
        except Exception as exc:
            logger.warning("PANTHEON NATS cooldown durable op failed: %s", exc)

    def _run_sync(self, coro: Any, timeout: float | None = None) -> Any:
        return self._track(self._loop.submit(coro)).result(timeout=timeout if timeout is not None else self._sync_timeout)

    async def aget_cooled_until(self, tenant: str, deployment: str) -> float | None:
        try:
            value = await asyncio.wait_for(
                asyncio.wrap_future(self._track(self._loop.submit(self._load_cooldown(tenant, deployment)))),
                timeout=self._sync_timeout,
            )
            if value is not None:
                return value
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("PANTHEON NATS cooldown async read timed out; using L1")
        return self._l1.get_cooled_until(tenant, deployment)

    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        try:
            value = self._run_sync(self._load_cooldown(tenant, deployment))
            if value is not None:
                return value
        except FutureTimeoutError:
            logger.warning("PANTHEON NATS cooldown read timed out; using L1")
        return self._l1.get_cooled_until(tenant, deployment)

    async def aset_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        self._l1.set_cooldown(tenant, deployment, cooled_until)
        try:
            merged = await asyncio.wait_for(
                asyncio.wrap_future(
                    self._track(self._loop.submit(self._set_cooldown_max(tenant, deployment, cooled_until)))
                ),
                timeout=self._sync_timeout,
            )
            self._l1.set_cooldown(tenant, deployment, merged)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("PANTHEON NATS cooldown async write timed out; using L1")

    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        self._l1.set_cooldown(tenant, deployment, cooled_until)
        try:
            merged = self._run_sync(self._set_cooldown_max(tenant, deployment, cooled_until))
            self._l1.set_cooldown(tenant, deployment, merged)
        except (FutureTimeoutError, TimeoutError):
            logger.warning("PANTHEON NATS cooldown write timed out; using L1")

    async def aincr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        try:
            counts = await asyncio.wait_for(
                asyncio.wrap_future(
                    self._track(self._loop.submit(self._increment_counter(tenant, deployment, minute, success=success)))
                ),
                timeout=self._sync_timeout,
            )
            self._set_l1_counts(tenant, deployment, minute, *counts)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("PANTHEON NATS counter async increment timed out; using L1")
            self._l1.incr(tenant, deployment, minute, success=success)

    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        try:
            counts = self._run_sync(self._increment_counter(tenant, deployment, minute, success=success))
            self._set_l1_counts(tenant, deployment, minute, *counts)
        except FutureTimeoutError:
            logger.warning("PANTHEON NATS counter increment timed out; using L1")
            self._l1.incr(tenant, deployment, minute, success=success)

    async def aget_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        try:
            loaded = await asyncio.wait_for(
                asyncio.wrap_future(self._track(self._loop.submit(self._load_counter(tenant, deployment, minute)))),
                timeout=self._sync_timeout,
            )
            if loaded is not None:
                return loaded
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("PANTHEON NATS counter async read timed out; using L1")
        return self._l1.get_counts(tenant, deployment, minute)

    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        try:
            loaded = self._run_sync(self._load_counter(tenant, deployment, minute))
            if loaded is not None:
                return loaded
        except FutureTimeoutError:
            logger.warning("PANTHEON NATS counter read timed out; using L1")
        return self._l1.get_counts(tenant, deployment, minute)

    def _set_l1_counts(self, tenant: str, deployment: str, minute: int, successes: int, failures: int) -> None:
        key = (tenant, deployment, minute)
        with self._l1._lock:  # bounded L1 mirror; authoritative value came from KV
            self._l1._counts[key] = [int(successes), int(failures)]
            cutoff = minute - 1
            for stale in [k for k in self._l1._counts if k[2] < cutoff]:
                del self._l1._counts[stale]

    async def _load_cooldown(self, tenant: str, deployment: str) -> float | None:
        kv = await self._cooldown_kv()
        if kv is None:
            return None
        try:
            payload = _loads(await _maybe_await(kv.get(_cooldown_key(tenant, deployment))))
        except Exception as exc:
            if not _missing_key(exc):
                raise
            return None
        value = float(payload["cooled_until"])
        self._l1.set_cooldown(tenant, deployment, value)
        return value

    async def _set_cooldown_max(self, tenant: str, deployment: str, cooled_until: float) -> float:
        kv = await self._cooldown_kv()
        if kv is None:
            return cooled_until
        key = _cooldown_key(tenant, deployment)
        for _attempt in range(_CAS_ATTEMPTS):
            try:
                entry = await _maybe_await(kv.get(key))
                payload = _loads(entry)
                revision = _entry_revision(entry)
                current = float(payload.get("cooled_until", 0.0))
            except Exception as exc:
                if not _missing_key(exc):
                    raise
                current = 0.0
                revision = None
            merged = max(current, float(cooled_until))
            try:
                if revision is None:
                    try:
                        await _kv_create(kv, key, _dumps({"cooled_until": merged}))
                    except AttributeError:
                        await _kv_put(kv, key, _dumps({"cooled_until": merged}))
                else:
                    await _kv_update(kv, key, _dumps({"cooled_until": merged}), revision)
                self._l1.set_cooldown(tenant, deployment, merged)
                return merged
            except Exception as exc:
                if not _wrong_revision(exc):
                    raise
        raise RuntimeError(f"NATS KV cooldown CAS failed for {key}")

    async def _load_counter(self, tenant: str, deployment: str, minute: int) -> tuple[int, int] | None:
        kv = await self._counter_kv()
        if kv is None:
            return None
        try:
            payload = _loads(await _maybe_await(kv.get(_counter_key(tenant, deployment, minute))))
        except Exception as exc:
            if not _missing_key(exc):
                raise
            return (0, 0)
        successes = int(payload.get("successes", 0))
        failures = int(payload.get("failures", 0))
        self._set_l1_counts(tenant, deployment, minute, successes, failures)
        return successes, failures

    async def _increment_counter(self, tenant: str, deployment: str, minute: int, *, success: bool) -> tuple[int, int]:
        kv = await self._counter_kv()
        if kv is None:
            self._l1.incr(tenant, deployment, minute, success=success)
            return self._l1.get_counts(tenant, deployment, minute)
        key = _counter_key(tenant, deployment, minute)
        field = "successes" if success else "failures"
        for _attempt in range(_CAS_ATTEMPTS):
            try:
                entry = await _maybe_await(kv.get(key))
                payload = _loads(entry)
                revision = _entry_revision(entry)
            except Exception as exc:
                if not _missing_key(exc):
                    raise
                payload = {"successes": 0, "failures": 0}
                revision = None
            payload[field] = int(payload.get(field, 0)) + 1
            payload["expires_at_epoch"] = max(float(payload.get("expires_at_epoch", 0.0)), (minute + 2) * 60.0)
            try:
                if revision is None:
                    try:
                        await _kv_create(kv, key, _dumps(payload))
                    except AttributeError:
                        await _kv_put(kv, key, _dumps(payload))
                else:
                    await _kv_update(kv, key, _dumps(payload), revision)
                return int(payload.get("successes", 0)), int(payload.get("failures", 0))
            except Exception as exc:
                if not _wrong_revision(exc):
                    raise
        raise RuntimeError(f"NATS KV counter CAS failed for {key}")

    def flush(self, timeout: float = 2.0) -> None:
        with self._pending_lock:
            pending = list(self._pending)
        for fut in pending:
            fut.result(timeout=timeout)

    def refresh_cooldown(self, tenant: str, deployment: str, timeout: float = 2.0) -> float | None:
        return self._run_sync(self._load_cooldown(tenant, deployment), timeout=timeout)

    def refresh_counts(self, tenant: str, deployment: str, minute: int, timeout: float = 2.0) -> tuple[int, int] | None:
        return self._run_sync(self._load_counter(tenant, deployment, minute), timeout=timeout)

    def close(self, timeout: float = 2.0) -> None:
        try:
            self.flush(timeout=timeout)
        except Exception:
            logger.debug("PANTHEON NATS cooldown flush failed during close", exc_info=True)
        self._loop.close(timeout=timeout)
