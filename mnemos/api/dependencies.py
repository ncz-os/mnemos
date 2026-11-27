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

import asyncio
import collections
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from mnemos.core.auth_context import UserContext
from mnemos.core.roles import KNOWN_ROLES

logger = logging.getLogger(__name__)

_auth_enabled: bool = False
_default_namespace: str = "default"
_personal_user_id: str = "default"

PERSONAL_SINGLETON: "Optional[UserContext]" = None  # set by configure_auth(); None before startup

_bearer = HTTPBearer(auto_error=False)
_MONITOR_ALLOWED_ROUTES = {("GET", "/stats"), ("GET", "/v1/federation/status")}


def _enforce_role_route_boundary(request: Request, user: UserContext) -> UserContext:
    """Reject invalid roles and keep monitor credentials on aggregate reads."""
    if user.role not in KNOWN_ROLES:
        raise HTTPException(status_code=403, detail="Role not permitted")
    if user.role == "monitor" and (request.method.upper(), request.url.path) not in _MONITOR_ALLOWED_ROUTES:
        raise HTTPException(status_code=403, detail="Monitor access is limited to status reads")
    return user

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
_AUTH_CACHE_SWEEP_EVERY = 256  # lookups between expired-entry sweeps
_TOUCH_SWEEP_EVERY = 1024  # touches between stale-key sweeps

# Protect every mutation below: get_current_user runs on the event loop, but
# the touch flusher hops into a worker thread and revoke_api_key may be called
# from a sync context. The dicts are only touched under this lock, so the
# claim and the write it guards cannot interleave.
_auth_lock = threading.RLock()
_key_cache: dict[str, tuple["UserContext", float]] = {}  # key hash -> (ctx, expires)
_touch_times: dict[object, float] = {}  # key id -> last write (monotonic)
_dirty_keys: "collections.OrderedDict[object, None]" = collections.OrderedDict()
_touch_task: "Optional[object]" = None  # in-flight flusher task
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

    Also drops the shared flusher handle so a task minted under the previous
    configuration is never mistaken for the current flusher: the orphaned
    task still drains whatever it snapshotted (harmless — the queue was just
    cleared), but the next touch mints a fresh flusher instead of joining a
    stale one. Never cancelled here: this runs on the event loop while the
    flusher may be mid-transaction, and cancelling mid-write would strand
    the batch; the orphan exits on its own when its snapshot drains.
    """
    global _touch_task
    with _auth_lock:
        _key_cache.clear()
        _touch_times.clear()
        _dirty_keys.clear()
        _touch_task = None


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
                known for known, (context, _) in _key_cache.items() if getattr(context, "api_key_id", None) == key_id
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
    """Allow *key_id* to be offered again after a failed write.

    The last_used stamp is set to "now", i.e. the normal debounce starts
    from the failure: the next request after ``_touch_interval`` queues a
    retry. Nothing is re-queued here on purpose — a store that stays down must
    not turn into a retry loop that outlives the requests that asked for it.
    The record is left in ``_touch_times`` so a revoked-then-recreated id
    cannot bypass the debounce; ``invalidate_api_key_cache`` drops it.
    """
    with _auth_lock:
        _touch_times[key_id] = time.monotonic()


def _task_live(task) -> bool:
    """True when *task* is a flusher that may still drain the queue.

    Unreadable is False, not True: the shared handle is the only gate, so a
    handle whose ``done()`` raises must not be mistaken for a live flusher,
    or every later touch would be dropped and the queue would never drain.
    A real Task never raises here, so treating it as dead costs nothing and
    lets the guard recover by scheduling a fresh flusher (the key that
    arrived just before was already queued in ``_dirty_keys``).
    """
    if task is None:
        return False
    done = getattr(task, "done", None)
    # A stubbed scheduler (tests, no loop) returns None: it drove the flush
    # synchronously, so nothing is in flight.
    if done is None:
        return False
    try:
        return not done()
    except Exception as e:
        logger.debug(f"[AUTH] unreadable flusher handle, scheduling recovery: {e}")
        return False


async def _write_touches(backend, oauth, key_ids) -> None:
    """One transaction per batch of last_used bumps."""
    async with backend.transactional() as tx:
        for key_id in key_ids:
            await oauth.touch_api_key(tx, key_id)


async def _flush_dirty_touches(backend, oauth) -> None:
    """Write every queued last_used bump in batches, one transaction a batch.

    Single-flight: while this coroutine runs it is the only flusher allowed
    to drain the queue. Keys queued while a batch is being written are
    picked up by the next turn of the loop. A failed write is *not* retried
    in the same task — a store that stays down must not spin here, it must
    wait for the debounce (or the next request).

    When this coroutine exits while keys are still queued (cancelled, or a
    failure), it hands the queue to exactly one successor flusher so a key
    is never stranded waiting for an unrelated later request. On
    cancellation the in-flight batch is re-offered with debounce backoff —
    through the same ``_requeue_touch`` path as the failure branch — so a
    shutdown-cancellation that leaves keys queued cannot turn into an
    immediate retry; the next write comes after the normal interval, on
    the next request.
    """
    global _touch_task
    cancelled = False
    try:
        while True:
            with _auth_lock:
                if not _dirty_keys:
                    return
                key_ids = list(_dirty_keys)
                _dirty_keys.clear()
            try:
                await _write_touches(backend, oauth, key_ids)
            except asyncio.CancelledError:
                cancelled = True
                with _auth_lock:
                    for key_id in key_ids:
                        _dirty_keys.setdefault(key_id, None)
                # Same debounce as the failure path: the cancelled batch is
                # offered again only after the touch interval, never hot.
                for key_id in key_ids:
                    _requeue_touch(key_id)
                raise
            except Exception as e:
                logger.warning(f"[AUTH] Failed to update last_used for keys {key_ids}: {e}")
                for key_id in key_ids:
                    _requeue_touch(key_id)
                return
    finally:
        # Only clear the shared handle when it still points at this task:
        # a successor scheduled near our exit owns the queue now, and
        # clearing its handle would fork a second flusher on the next touch.
        try:
            me = asyncio.current_task()
        except RuntimeError:  # no running loop (sync-driven stubs)
            me = None
        with _auth_lock:
            if _touch_task is me:
                _touch_task = None
            pending = bool(_dirty_keys)
        if pending and not cancelled and me is not None and not me.cancelling():
            # Keys arrived after our last snapshot (or we failed): hand
            # them to exactly one chained flusher instead of waiting for
            # an unrelated later request. Never chain out of a
            # cancellation — shutdown is tearing the loop down, and a
            # successor scheduled now would outlive the cancel sweep.
            # ``cancelled`` covers cancellation delivered at the
            # _write_touches await; me.cancelling() covers cancellation
            # requested but not yet delivered (cancel() landing during the
            # synchronous snapshot section, or during any other await in
            # this task, with no await point reached before finally runs
            # and the task was never marked cancelling by our own handler).
            # Both guards stay — removing either reopens a chain-on-cancel
            # path. A store failure racing shutdown could still chain here
            # if the sweep has not reached this task yet; the successor is
            # a tracked background task, so the sweep cancels it in turn.
            try:
                _ensure_touch_flusher(backend, oauth)
            except RuntimeError:
                # Loop is closing under us (shutdown): leave the keys
                # queued. They debounce-retry on the next request; minting
                # a task on a closing loop would raise anyway.
                pass


def _ensure_touch_flusher(backend, oauth) -> bool:
    """True when a flusher that will drain the queue is (now) running.

    Genuinely single-flight: a live flusher task is reused, never
    duplicated. A task is only started when no flusher is in flight; a
    newly created task cannot have started yet (the caller has not
    yielded), so its first drain is guaranteed to see the key queued by
    ``_schedule_touch`` just before. Because the running flusher loops
    until the queue is empty, a key queued while it is writing is picked
    up on its next turn — no second task is needed. A key queued after
    the flusher's final snapshot is handed to a single chained successor
    by the flusher's own exit path.
    """
    global _touch_task
    try:
        # Imported outside the lock: the module may still be initialising.
        from mnemos.core.lifecycle import _schedule_background
    except Exception as e:  # pragma: no cover - lifecycle is always importable
        logger.debug(f"[AUTH] last_used update not scheduled: {e}")
        return False
    with _auth_lock:
        if _task_live(_touch_task):
            return True
        current = _touch_task
        try:
            _touch_task = _schedule_background(_flush_dirty_touches(backend, oauth))
        except Exception as e:  # no running loop (scripts, sync callers)
            logger.debug(f"[AUTH] last_used update not scheduled: {e}")
            _touch_task = current
            return False
        if not _task_live(_touch_task):
            # The scheduler drove the flush synchronously (tests / sync
            # stubs): nothing is in flight, so drop the handle instead of
            # keeping a stale reference that would look "live" later.
            _touch_task = current
    return True


def _schedule_touch(backend, oauth, key_id) -> bool:
    """Debounced, single-flight last_used bump. Returns True when queued."""
    # Replica mode: authentication must work read-only. A Bearer lookup on
    # a hot standby must not queue a last_used write — the flusher would
    # fail with "read-only transaction" on the standby. Skip the queue
    # entirely, including the debounce registration.
    try:
        from mnemos.core.replica_mode import replica_mode_enabled

        if replica_mode_enabled():
            return False
    except Exception:
        pass
    if key_id is None or _touch_interval <= 0 or not _note_touch(key_id):
        return False
    if _ensure_touch_flusher(backend, oauth):
        return True
    # Nothing can drain the queue here (no running loop). Hand the key back so
    # the next request queues it again once the debounce allows.
    with _auth_lock:
        _dirty_keys.pop(key_id, None)
    return False


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
        raise HTTPException(status_code=503, detail="Persistence backend not available")
    if not getattr(backend, "_supports_oauth_persistence", False):
        raise HTTPException(
            status_code=503,
            detail=(
                "Authentication requires an OAuth-capable persistence backend; "
                "this backend does not implement OAuthPersistence."
            ),
        )
    return backend.oauth, backend


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
        return _enforce_role_route_boundary(request, PERSONAL_SINGLETON)

    oauth, backend = _auth_backend(request)

    # 1. API key (Bearer) — existing behaviour, now behind a short TTL cache
    #    and a debounced last_used touch (decision M5).
    if credentials is not None:
        raw_key = credentials.credentials
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        context = _cache_get(key_hash, time.monotonic())
        if context is None:
            async with backend.transactional() as tx:
                row = await oauth.lookup_api_key(tx, key_hash)
            if row is None or row.get("revoked"):
                raise HTTPException(status_code=401, detail="Invalid or revoked API key")
            fresh = UserContext(
                user_id=row["user_id"],
                group_ids=list(row.get("group_ids") or []),
                role=row["role"],
                namespace=row["namespace"],
                authenticated=True,
            )
            # The id is what revoke_api_key is given; keeping it on the context
            # lets the revoke route drop exactly this cache entry.
            fresh.api_key_id = row["id"]
            _cache_put(key_hash, fresh, time.monotonic())
            context = fresh
        # Never hand out the cached object itself — neither on a hit nor on
        # the miss that just stored it: a route that appended to
        # user.group_ids would edit every later request's identity for the
        # rest of the TTL (potentially on another task).
        context = replace(context, group_ids=list(context.group_ids))
        # Debounced: at most one last_used write per key per touch interval,
        # on cache hits as well as misses (a token used all day still shows a
        # recent last_used).
        _schedule_touch(backend, oauth, context.api_key_id)
        return _enforce_role_route_boundary(request, context)

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
            return _enforce_role_route_boundary(request, context)

    # 3. No credentials.
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_root(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency — raises 403 if caller is not root."""
    if user.role != "root":
        raise HTTPException(status_code=403, detail="Root access required")
    return user


async def require_root_or_monitor(user: UserContext = Depends(get_current_user)) -> UserContext:
    """Allow root and monitor roles on explicitly approved aggregate reads."""
    if user.role not in {"root", "monitor"}:
        raise HTTPException(status_code=403, detail="Root or monitor access required")
    return user
