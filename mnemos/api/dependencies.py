"""FastAPI auth dependencies (Bearer + session-cookie).

Live home of the `get_current_user` dependency consumed by the route
modules. Existing Bearer flow preserved; session-cookie flow added
as a secondary path. Auth-disabled mode unchanged.

Auth lookups (API key + browser session) go through the backend-neutral
``OAuthRepository`` exposed by ``request.app.state.persistence_backend.oauth``
so the same path works on every supported backend (Postgres, SQLite,
Oracle, Db2). Backends that do not implement OAuthPersistence return a
clear 503 with the missing-capability name, instead of pretending the
credential check ran.

(Originally split out from a pre-v4 `api/auth.py` module that no
longer exists.)

Per-request cost (decision M5)
------------------------------
A bearer request used to cost a full ``lookup_api_key`` transaction plus a
background ``touch_api_key`` write, every request, forever. Both are now
bounded:

* a key hash -> ``UserContext`` cache with a short TTL
  (``MNEMOS_AUTH_CACHE_TTL_SECONDS``, default 30 s). A cached hit costs no
  database work at all, so :class:`AuthCacheStats` counts lookups that now
  bypass the database instead of tracing every one.
* ``api_keys.last_used`` is written at most once per
  ``MNEMOS_AUTH_TOUCH_INTERVAL_SECONDS`` (default 60 s) per key, and the write
  is no longer one background coroutine per request: at most one flusher task
  drains a dirty-key set.

**Revocation lag is bounded by the TTL, per worker.** Revoking a key
(``DELETE /apikeys/{key_id}``) clears *this* process's cache immediately, so
the next request on the same worker gets 401. Another worker — a second
uvicorn worker, a second replica behind a load balancer — keeps serving the
cached ``UserContext`` until its own entry expires, i.e. for at most the TTL.
Set a shorter TTL when that window matters more than the saved round trips,
or the longest acceptable lag when it does not.
"""
import collections
import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from mnemos.core.auth_context import UserContext

logger = logging.getLogger(__name__)

_auth_enabled: bool = False
_default_namespace: str = "default"
_personal_user_id: str = "default"

PERSONAL_SINGLETON: "Optional[UserContext]" = None   # set by configure_auth(); None before startup

_bearer = HTTPBearer(auto_error=False)

# ── Per-request authentication cost (decision M5) ───────────────────────────
#
# Two module-level knobs, configured once at startup from ``settings.auth``:
#
#   _cache_ttl     — a key-hash -> UserContext entry is reused for this many
#                    seconds (0 disables the cache). This is also the
#                    cross-worker revocation lag: worker A clears its own cache
#                    when it revokes, worker B only finds out when its entry
#                    expires.
#   _touch_interval — at most one api_keys.last_used write per key per this
#                    many seconds (0 touches on every request).
#
# Both are read at call time so ``configure_auth`` (tests, lifespan) can change
# them without re-importing the module.
_AUTH_CACHE_TTL_DEFAULT = 30.0
_AUTH_TOUCH_INTERVAL_DEFAULT = 60.0
_cache_ttl: float = _AUTH_CACHE_TTL_DEFAULT
_touch_interval: float = _AUTH_TOUCH_INTERVAL_DEFAULT
_AUTH_CACHE_MAX_ENTRIES = 4096
_AUTH_CACHE_SWEEP_EVERY = 256        # lookups between expired-entry sweeps
_TOUCH_SWEEP_EVERY = 1024            # touches between stale-key sweeps
_TOUCH_FLUSH_RETRY_SECONDS = 30.0

# Protect every mutation below: get_current_user runs on the event loop, but
# the touch flusher hops into a worker thread and revoke_api_key may be called
# from a sync context. The dicts are only touched under this lock, so the
# claim and the write it guards cannot interleave.
_auth_lock = threading.Lock()
_key_cache: dict[str, tuple["UserContext", float]] = {}     # key hash -> (ctx, expires)
_touch_times: dict[object, float] = {}                      # key id -> last write (monotonic)
_dirty_keys: "collections.OrderedDict[object, None]" = collections.OrderedDict()
_touch_task: "Optional[object]" = None                      # in-flight flusher task
_lookups_since_sweep = 0
_touches_since_sweep = 0


@dataclass(frozen=True)
class AuthCacheStats:
    """Observability counters for the M5 cache, for operators and tests."""

    key_cache_entries: int
    pending_touches: int


def auth_cache_stats() -> AuthCacheStats:
    """Return the current size of the credential cache and the touch queue."""
    with _auth_lock:
        return AuthCacheStats(key_cache_entries=len(_key_cache), pending_touches=len(_dirty_keys))


def _reset_auth_caches() -> None:
    """Drop every cached credential and pending last_used touch.

    Called by ``configure_auth`` at startup and available to tests that
    reconfigure auth mid-process: entries minted under one configuration must
    not leak into the next one.
    """
    with _auth_lock:
        _key_cache.clear()
        _touch_times.clear()
        _dirty_keys.clear()


def _cache_get(key_hash: str, now: float) -> "Optional[UserContext]":
    with _auth_lock:
        entry = _key_cache.get(key_hash)
        if entry is None:
            return None
        context, expires_at = entry
        if expires_at <= now:
            del _key_cache[key_hash]
            return None
        return context


def _cache_put(key_hash: str, context: UserContext, now: float) -> None:
    global _lookups_since_sweep
    ttl = _cache_ttl
    if ttl <= 0:
        return
    with _auth_lock:
        _key_cache[key_hash] = (context, now + ttl)
        _lookups_since_sweep += 1
        if _lookups_since_sweep < _AUTH_CACHE_SWEEP_EVERY and len(_key_cache) <= _AUTH_CACHE_MAX_ENTRIES:
            return
        _lookups_since_sweep = 0
        expired = [known for known, (_, expires_at) in _key_cache.items() if expires_at <= now]
        for known in expired:
            del _key_cache[known]
        # Still over the bound with nothing expired to collect: drop the
        # oldest-inserted entries. Cache misses are safe, just slower.
        overflow = len(_key_cache) - _AUTH_CACHE_MAX_ENTRIES
        if overflow > 0:
            for known in list(_key_cache)[:overflow]:
                del _key_cache[known]


def _cache_clear(key_hash: "Optional[str]") -> None:
    """Drop one entry (or all of them when *key_hash* is None)."""
    with _auth_lock:
        if key_hash is None:
            _key_cache.clear()
        else:
            _key_cache.pop(key_hash, None)


def invalidate_api_key_cache(key_id=None, key_hash: "Optional[str]" = None) -> None:
    """Forget a credential after it was revoked, deleted or regenerated.

    Called by the revoke route. ``key_hash`` drops the exact entry; ``key_id``
    additionally drops any entry whose row carries that id (the cache is keyed
    by hash, so the id is only known from the cached context). Passing
    neither clears the whole cache, which is the safe answer when the caller
    cannot say which credential changed.

    This only clears the calling process's cache. Other workers keep serving
    their cached result until their entry expires — see the module docstring.
    """
    if key_id is None and key_hash is None:
        _cache_clear(None)
        return
    with _auth_lock:
        if key_hash is not None:
            _key_cache.pop(key_hash, None)
        if key_id is not None:
            stale = [
                known
                for known, (context, _) in _key_cache.items()
                if getattr(context, "api_key_id", None) == key_id
            ]
            for known in stale:
                del _key_cache[known]


def _note_touch(key_id) -> bool:
    """Claim the last_used write for *key_id*. True when the caller should write."""
    global _touches_since_sweep
    now = time.monotonic()
    with _auth_lock:
        last = _touch_times.get(key_id)
        if last is not None and now - last < _touch_interval:
            return False
        _touch_times[key_id] = now
        _dirty_keys[key_id] = None
        _touches_since_sweep += 1
        if _touches_since_sweep >= _TOUCH_SWEEP_EVERY:
            _touches_since_sweep = 0
            interval = _touch_interval
            stale = [known for known, stamp in _touch_times.items() if now - stamp >= interval]
            for known in stale:
                del _touch_times[known]
    return True


def _requeue_touch(key_id) -> None:
    """Put *key_id* back after a failed write, so the next request retries.

    The last_used stamp is rewound by the retry interval rather than cleared,
    so a store that stays down does not turn into one failed write per
    request.
    """
    with _auth_lock:
        stamp = time.monotonic() - _touch_interval + _TOUCH_FLUSH_RETRY_SECONDS
        _touch_times[key_id] = stamp
        _dirty_keys[key_id] = None


async def _flush_dirty_touches(backend, oauth) -> None:
    """Write every queued last_used bump in one transaction, then clear the queue.

    One transaction for the whole batch, not one per request: the queue is
    drained per event-loop turn, so a burst of first-time key usage costs one
    round trip, not one per caller.
    """
    global _touch_task
    try:
        while True:
            with _auth_lock:
                if not _dirty_keys:
                    return
                key_ids = list(_dirty_keys)
                _dirty_keys.clear()
            try:
                async with backend.transactional() as tx:
                    for key_id in key_ids:
                        await oauth.touch_api_key(tx, key_id)
            except Exception as e:
                logger.warning(f"[AUTH] Failed to update last_used for keys {key_ids}: {e}")
                for key_id in key_ids:
                    _requeue_touch(key_id)
    finally:
        # No other coroutine can run between the empty check above and this
        # assignment, so a touch queued after it starts its own flusher.
        with _auth_lock:
            if not _dirty_keys:
                _touch_task = None


def _ensure_touch_flusher(backend, oauth) -> object:
    """Return the in-flight flusher task, starting one when the queue needs it."""
    global _touch_task
    with _auth_lock:
        if _touch_task is not None:
            return _touch_task
        try:
            from mnemos.core.lifecycle import _schedule_background

            _touch_task = _schedule_background(_flush_dirty_touches(backend, oauth))
        except Exception as e:  # no running loop (scripts, sync callers)
            logger.debug(f"[AUTH] last_used update not scheduled: {e}")
        return _touch_task


def _schedule_touch(backend, oauth, key_id) -> bool:
    """Debounced, single-flight last_used bump. Returns True when queued."""
    if _touch_interval <= 0 or not _note_touch(key_id):
        return False
    if _ensure_touch_flusher(backend, oauth) is None:
        # Nothing will drain the queue; hand the key back so the next request
        # retries after the retry interval instead of losing the touch.
        with _auth_lock:
            _dirty_keys.pop(key_id, None)
        _requeue_touch(key_id)
        return False
    return True


def configure_auth(config: dict | None = None) -> None:
    """Called once at startup from lifecycle lifespan."""
    from mnemos.core.config import get_settings

    global _auth_enabled, _default_namespace, _personal_user_id, PERSONAL_SINGLETON
    global _cache_ttl, _touch_interval
    settings = get_settings().auth
    if config is None:
        config = {}
        _auth_enabled = settings.enabled
    else:
        _auth_enabled = config.get("enabled", settings.enabled)
    _default_namespace = config.get("default_namespace", settings.default_namespace)
    _personal_user_id = config.get("personal_user_id", settings.personal_user_id)
    # The cache and touch-debounce knobs are read from settings.auth on every
    # call; configure_auth may override them (tests do) but a reconfiguration
    # must never inherit entries minted under the previous one.
    _cache_ttl = float(getattr(settings, "cache_ttl_seconds", _AUTH_CACHE_TTL_DEFAULT))
    _touch_interval = float(getattr(settings, "touch_interval_seconds", _AUTH_TOUCH_INTERVAL_DEFAULT))
    if config.get("cache_ttl_seconds") is not None:
        _cache_ttl = float(config["cache_ttl_seconds"])
    if config.get("touch_interval_seconds") is not None:
        _touch_interval = float(config["touch_interval_seconds"])
    _reset_auth_caches()
    PERSONAL_SINGLETON = UserContext(
        user_id=_personal_user_id,
        group_ids=[],
        role="root",
        namespace=_default_namespace,
        authenticated=False,
    )
    logger.info(
        f"Auth configured: enabled={_auth_enabled}, "
        f"namespace={_default_namespace}, personal_user={_personal_user_id}, "
        f"auth_cache_ttl={_cache_ttl}s, last_used_touch_interval={_touch_interval}s"
    )


def _auth_backend(request: Request):
    """Return ``(oauth_repo, backend)`` from the active persistence backend.

    Backend-neutral authentication goes through ``OAuthRepository`` so the
    same call shape works on Postgres, SQLite, Oracle, and Db2. Backends
    that do not implement ``OAuthPersistence`` (MySQL/MariaDB today, since
    those backends do not carry the ``api_keys`` / ``oauth_sessions``
    tables) return a 503 with a clear message rather than a misleading
    pool-availability error.
    """
    backend = getattr(request.app.state, "persistence_backend", None)
    if backend is None:
        raise HTTPException(
            status_code=503, detail="Persistence backend not available"
        )
    if not getattr(backend, "_supports_oauth_persistence", False):
        raise HTTPException(
            status_code=503,
            detail=(
                "Authentication requires an OAuth-capable persistence backend; "
                "this backend does not implement OAuthPersistence."
            ),
        )
    return backend.oauth, backend


async def _touch_api_key(backend, oauth, key_id) -> None:
    """Bump ``api_keys.last_used`` via the backend-neutral OAuth repository."""
    try:
        async with backend.transactional() as tx:
            await oauth.touch_api_key(tx, key_id)
    except Exception as e:
        logger.warning(f"[AUTH] Failed to update last_used for key {key_id}: {e}")


async def get_current_user(
    request: Request,
    credentials=Depends(_bearer),
) -> UserContext:
    """Auth dependency — Bearer token first, session cookie second.

    Credential lookups go through the backend-neutral OAuth repository
    exposed by the active persistence backend, so the same code path serves
    Postgres, SQLite, Oracle, and Db2.
    """
    if not _auth_enabled:
        if PERSONAL_SINGLETON is None:
            raise HTTPException(status_code=503, detail="Auth not yet configured — startup incomplete")
        return PERSONAL_SINGLETON

    oauth, backend = _auth_backend(request)

    # 1. API key (Bearer) — existing behaviour, now behind a short TTL cache
    #    and a debounced last_used touch (decision M5).
    if credentials is not None:
        raw_key = credentials.credentials
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        cached = _cache_get(key_hash, time.monotonic())
        if cached is None:
            async with backend.transactional() as tx:
                row = await oauth.lookup_api_key(tx, key_hash)
            if row is None or row.get("revoked"):
                raise HTTPException(status_code=401, detail="Invalid or revoked API key")
            cached = UserContext(
                user_id=row["user_id"],
                group_ids=list(row.get("group_ids") or []),
                role=row["role"],
                namespace=row["namespace"],
                authenticated=True,
            )
            # The id is what revoke_api_key is given; keeping it on the context
            # lets the revoke route drop exactly this cache entry.
            cached.api_key_id = row["id"]
            _cache_put(key_hash, cached, time.monotonic())
        _schedule_touch(backend, oauth, cached.api_key_id)
        return cached

    # 2. Session cookie — v3.0.0+ path (only checked when no Bearer).
    from mnemos.core.oauth import SESSION_COOKIE_NAME

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        now = datetime.now(timezone.utc)
        async with backend.transactional() as tx:
            resolved = await oauth.resolve_active_session(tx, cookie_value, now=now)
        if resolved is not None:
            context = UserContext(
                user_id=resolved["user_id"],
                group_ids=[],
                role="user",
                namespace=_default_namespace,
                authenticated=True,
            )
            context.session_id = cookie_value
            return context

    # 3. No credentials.
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_root(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency — raises 403 if caller is not root."""
    if user.role != "root":
        raise HTTPException(status_code=403, detail="Root access required")
    return user
