"""Postgres hot-standby (replica) mode — detection, state, and guards.

Operator decision 2026-11-21/22: every MNEMOS host may run against a local
Postgres hot standby of the primary's database and serve a READ-ONLY API
from it. A physical hot standby is read-only, so a replica-mode process
must:

* skip every startup write (migrations, CREATE EXTENSION, schema DDL);
* keep reads fully working (search, list, get, KG reads, stats, health);
* reject mutations with ``replica_read_only``;
* authenticate without writing (last-used touches skipped);
* report its role and replay lag on ``/health``;
* switch to primary behaviour on promotion only when the explicit switch
  is ``auto``.

The explicit switch is ``MNEMOS_REPLICA_MODE=auto|on|off`` (default
``auto``):

* ``auto`` — ask the server with ``SELECT pg_is_in_recovery()``; follow it.
* ``on``   — force replica mode regardless of the server state.
* ``off``  — never enter replica mode, even if the server reports recovery.

Replica mode is Postgres-only. SQLite, MySQL, MariaDB, Oracle and Db2
backends ignore the switch entirely — they never call the detection
path and always report the ``primary`` role.

This module lives in ``core`` and imports no persistence module, so the
persistence layer may import it for schema verification without creating
an import cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from mnemos.core import config as _core_config

logger = logging.getLogger(__name__)

# ── Env-var contract ───────────────────────────────────────────────────────────

REPLICA_MODE_ENV = "MNEMOS_REPLICA_MODE"
PRIMARY_URL_ENV = "MNEMOS_PRIMARY_URL"

MODE_AUTO = "auto"
MODE_ON = "on"
MODE_OFF = "off"

#: ``pg_last_xact_replay_timestamp`` is only meaningful while the server
#: is actually replaying WAL; probe it no more
#: often than this (health endpoints can be hammered).
LAG_CACHE_SECONDS = 5.0


def replica_mode_env() -> str:
    """Read ``MNEMOS_REPLICA_MODE``; default ``auto``. Unknown values warn
    and fall back to ``auto`` rather than failing startup.

    An explicitly empty value (``MNEMOS_REPLICA_MODE=``) also warns: it
    means the operator touched the variable but left it blank, which is
    not the same as "unset".

    The environment read itself lives in ``mnemos.core.config`` (see
    ``mnemos.core.config.replica_mode_env``); this wrapper keeps the module
    public API unchanged for the persistence layer and the API routes.
    """
    return _core_config.replica_mode_env()


def primary_url() -> Optional[str]:
    """Return ``MNEMOS_PRIMARY_URL`` when set (used for ``primary_hint``)."""
    return _core_config.replica_primary_url_env()


# ── Process-level state ────────────────────────────────────────────────────────

_state_lock = asyncio.Lock()
_replica: bool = False
_promotion_logged: bool = False


def is_replica() -> bool:
    """Current replica mode (read on every request; flip is lock-free)."""
    return _replica


def replica_mode_enabled() -> bool:
    """Alias kept for readability at guard call sites."""
    return _replica


def primary_hint() -> Optional[str]:
    """The configured primary URL for machine-readable 503 bodies, if any."""
    return primary_url()


async def set_replica_mode(value: bool, *, reason: str = "") -> bool:
    """Set the process-level replica flag.

    Returns ``True`` when the flag actually changed. A replica -> primary
    flip under ``auto`` is logged exactly once per process lifetime; an
    explicit ``on``/``off`` configuration is never "logged once" because it
    was operator-intended, not an observed promotion.

    The check-then-act sequence is atomic under ``_state_lock``.
    """
    global _replica, _promotion_logged
    async with _state_lock:
        if _replica == value:
            return False
        was_replica = _replica
        _replica = value
        if was_replica and not value and not _promotion_logged:
            _promotion_logged = True
            logger.info(
                "[REPLICA] promoted to PRIMARY: pg_is_in_recovery turned false; "
                "switching process to primary behaviour without restart%s",
                f" ({reason})" if reason else "",
            )
        elif value and not was_replica:
            logger.info("[REPLICA] entered replica mode%s", f" ({reason})" if reason else "")
        return True


async def check_and_set_replica_mode(value: bool, *, reason: str = "") -> bool:
    """Atomically check-then-set the replica flag.

    Returns ``True`` only when the flag changed. This closes the race
    window in health probes: two concurrent probes both see
    ``replica=True``, both read ``pg_is_in_recovery()=False``, and both
    try to flip — without this helper the second flip could log the
    promotion twice or leave callers with a stale view of the state.
    """
    global _replica, _promotion_logged
    async with _state_lock:
        if _replica == value:
            return False
        was_replica = _replica
        _replica = value
        if was_replica and not value and not _promotion_logged:
            _promotion_logged = True
            logger.info(
                "[REPLICA] promoted to PRIMARY: pg_is_in_recovery turned false; "
                "switching process to primary behaviour without restart%s",
                f" ({reason})" if reason else "",
            )
        elif value and not was_replica:
            logger.info("[REPLICA] entered replica mode%s", f" ({reason})" if reason else "")
        return True


def _reset_replica_state_for_tests() -> None:
    """Test helper: drop the flag and the once-only promotion latch."""
    global _replica, _promotion_logged, _lag_cache, _lag_failure_logged, _recovery_failure_logged
    _replica = False
    _promotion_logged = False
    _lag_cache = (0.0, None)
    _lag_failure_logged = False
    _recovery_failure_logged = False


# ── Detection ──────────────────────────────────────────────────────────────────


_recovery_failure_logged = False


async def probe_recovery(pool_or_backend: Any) -> Optional[bool]:
    """Return the server's recovery state, or None when it is unavailable."""
    global _recovery_failure_logged
    try:
        pool = pool_or_backend if hasattr(pool_or_backend, "acquire") else getattr(pool_or_backend, "_pool", None)
        if pool is None:
            raise RuntimeError("pool unavailable")
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT pg_is_in_recovery()")
            if result is None:
                raise RuntimeError("recovery state unavailable")
            return bool(result)
    except Exception as exc:  # noqa: BLE001 - probe failure must not kill startup
        level = logging.DEBUG if _recovery_failure_logged else logging.WARNING
        _recovery_failure_logged = True
        logger.log(level, "[REPLICA] pg_is_in_recovery probe failed: %s", exc)
        return None


async def detect_replica_mode(pool_or_backend: Any) -> bool:
    """Resolve the replica flag for a Postgres pool or backend.

    Runs ``SELECT pg_is_in_recovery()``. The explicit switch wins:
    ``on`` forces True, ``off`` forces False. On ``auto`` the server answer
    is used; an unreadable server leaves the current state unchanged.
    """
    mode = replica_mode_env()
    if mode == MODE_ON:
        return True
    if mode == MODE_OFF:
        return False
    result = await probe_recovery(pool_or_backend)
    if result is None:
        logger.warning("[REPLICA] replica state is unknown; keeping current state")
        return is_replica()
    return result


# ── Replay lag ─────────────────────────────────────────────────────────────────

_lag_cache: tuple[float, Optional[float]] = (0.0, None)
_lag_failure_logged = False


async def replay_lag_seconds(pool_or_backend: Any) -> Optional[float]:
    """Standby replay lag in seconds, or ``None`` when unavailable.

    The server computes the elapsed time since the last replayed transaction.
    Returns ``None`` when no timestamp is available or the query fails.
    """
    global _lag_cache, _lag_failure_logged
    now = time.monotonic()
    cached_at, cached = _lag_cache
    if now - cached_at < LAG_CACHE_SECONDS:
        return cached

    value: Optional[float] = None
    try:
        acquire_pool = pool_or_backend if hasattr(pool_or_backend, "acquire") else getattr(pool_or_backend, "_pool", None)
        if acquire_pool is not None:
            async with acquire_pool.acquire() as conn:
                lag = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::float8"
                )
                if lag is not None:
                    value = max(0.0, lag)
        _lag_cache = (now, value)
    except Exception as exc:  # noqa: BLE001 - health probes must never raise
        level = logging.DEBUG if _lag_failure_logged else logging.WARNING
        _lag_failure_logged = True
        logger.log(level, "[REPLICA] replay lag probe failed: %s", exc)
        _lag_cache = (now, None)
        return None
    return value


async def health_probe_refresh(pool_or_backend: Any) -> None:
    """Run by the ``/health`` endpoint under ``auto`` mode.

    Re-reads ``pg_is_in_recovery()``; if it turned false and we were
    serving as a replica, switches the process to primary behaviour
    without a restart (and logs it once). Under explicit ``on``/``off``
    the server state is ignored entirely.
    """
    if replica_mode_env() != MODE_AUTO:
        return
    current = await probe_recovery(pool_or_backend)
    if current is None:
        return
    # Atomic check-then-set: the flip and its "logged once" decision happen
    # under the same lock, so concurrent health probes cannot double-log a
    # promotion or leave a stale flag behind.
    await check_and_set_replica_mode(current, reason="health probe")
