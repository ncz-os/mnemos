"""MNEMOS HTTP rate limiting.

On by default (`RATE_LIMIT_ENABLED=true`). Tune via environment variables:

    RATE_LIMIT_ENABLED=true          # opt-out by setting "false"
    RATE_LIMIT_DEFAULT=300/minute    # global ceiling per client
    RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1  # or memory:// (single-worker)
    RATE_LIMIT_TRUST_PROXY=false     # set true ONLY behind a trusted reverse proxy
                                     # that appends X-Forwarded-For.
    RATE_LIMIT_TRUSTED_PROXY_HOPS=1  # how many of YOUR proxies append XFF; the
                                     # client address is that many entries from
                                     # the RIGHT (client-supplied entries to its
                                     # left are ignored, never bucketed on)

Route-specific limits (e.g. on `/v1/consultations`) are applied via
`@limiter.limit()` in the relevant handler.
"""

import logging
import threading
import time

from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: F401 — re-exported
from slowapi.errors import RateLimitExceeded  # noqa: F401
from slowapi.middleware import SlowAPIMiddleware  # noqa: F401
from slowapi.util import get_remote_address
from starlette.requests import Request

from mnemos.core.config import get_settings

logger = logging.getLogger(__name__)

_RATE_LIMIT_SETTINGS = get_settings().rate_limit
RATE_LIMIT_ENABLED = _RATE_LIMIT_SETTINGS.enabled
RATE_LIMIT_DEFAULT = _RATE_LIMIT_SETTINGS.default
RATE_LIMIT_STORAGE = _RATE_LIMIT_SETTINGS.storage_uri
RATE_LIMIT_TRUST_PROXY = _RATE_LIMIT_SETTINGS.trust_proxy
RATE_LIMIT_TRUSTED_PROXY_HOPS = _RATE_LIMIT_SETTINGS.trusted_proxy_hops


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate-limit bucketing.

    By default we trust only the direct TCP peer (safe anywhere).

    When ``RATE_LIMIT_TRUST_PROXY=true``, a proxy we control sits in front of
    us and appends the address it actually saw to ``X-Forwarded-For``. The
    header is a list of hops, and only the right-most ``hops`` entries were
    written by infrastructure: a proxy APPENDS to whatever the client sent, so
    anything further left is arbitrary client-supplied text. Bucketing on the
    left-most entry -- as this used to -- let any client mint a fresh bucket
    per request by rotating a made-up value, i.e. it disabled rate limiting
    for exactly the deployments that turned the trust switch ON (CF-23).

    So we count in from the right by ``RATE_LIMIT_TRUSTED_PROXY_HOPS`` (the
    number of proxies of ours that append; default 1). If the header carries
    fewer entries than that chain, it did not traverse our full proxy path (or
    the client sent it itself), so every entry is client-influenced and the
    request is bucketed on the direct peer -- the one address the client cannot
    choose.
    """
    if RATE_LIMIT_TRUST_PROXY:
        client = _forwarded_client_ip(request)
        if client:
            return client
    return get_remote_address(request)


def _forwarded_client_ip(request: Request) -> str:
    """The address our own proxies saw, or "" when no header value is trustworthy."""
    hops = max(int(RATE_LIMIT_TRUSTED_PROXY_HOPS or 1), 1)

    # `headers.getlist` keeps repeated header lines as separate entries; split
    # each on commas and drop blanks so a malformed ", ," cannot shift the hop.
    entries: list[str] = []
    for raw in request.headers.getlist("x-forwarded-for"):
        entries.extend(part.strip() for part in raw.split(",") if part.strip())

    if len(entries) >= hops:
        return entries[-hops]

    # The chain is shorter than the number of proxies we trust, so this request
    # did not traverse the whole path, or the client sent the header itself.
    # Either way its contents are client-influenced and unusable: fall through
    # to the direct peer, the one address the client cannot choose.
    logger.debug(
        "x-forwarded-for has %d usable entr%s but %d trusted proxy hop(s) are configured; bucketing on the direct peer",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        hops,
    )
    return ""


class _FailOpenLimiter(Limiter):
    """A Limiter that degrades to "unlimited" when its store is unreachable.

    This has to be fixed at the limiter, not at the exception handler, because
    of how SlowAPIMiddleware routes errors. ``slowapi.middleware._check_limits``
    does:

        except Exception as e:
            exception_handler = app.exception_handlers.get(
                type(e), _rate_limit_exceeded_handler
            )

    That is an EXACT-TYPE lookup. ``app.add_exception_handler(RateLimitExceeded,
    ...)`` therefore only ever matches a real limit violation. When the store is
    down the limiter raises its backend's error -- ``ConnectionError`` from
    redis -- which matches nothing, so slowapi falls back to its own
    ``_rate_limit_exceeded_handler``, which reads ``exc.detail``. That attribute
    exists only on RateLimitExceeded, so the handler itself raises

        AttributeError: 'ConnectionError' object has no attribute 'detail'

    out of ``dispatch`` -- an unhandled 500 on EVERY route, ``/health``
    included, from a container that started cleanly. Registering the handler
    for ``Exception`` does not help either: the lookup is by exact type, so it
    is not subclass-aware, and the set of errors an arbitrary storage backend
    can raise is not enumerable.

    Catching here means slowapi never sees a non-RateLimitExceeded exception at
    all, and it covers the ``@limiter.limit()`` decorator path as well as the
    middleware path.

    Rate limiting is a protective measure: losing its store must degrade to
    "unlimited", never to "the API is broken". Genuine violations still raise
    RateLimitExceeded and still return 429.

    Reproduced on an arm64 build of 6.1.6, where the `server` profile defaults
    ``rate_limit.storage_uri`` to ``redis://localhost:6379/1`` and the image
    ships no redis.
    """

    # While the store is down this path runs on every request, so the warning
    # itself is rate-limited: log the first failure, then at most one summary
    # per interval (with the number of requests swallowed since the last one).
    FAIL_OPEN_LOG_INTERVAL_SECONDS = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_open_lock = threading.Lock()
        self._fail_open_last_log = 0.0
        self._fail_open_suppressed = 0

    def _log_fail_open(self, exc: BaseException) -> None:
        now = time.monotonic()
        with self._fail_open_lock:
            first = self._fail_open_last_log == 0.0
            due = now - self._fail_open_last_log >= self.FAIL_OPEN_LOG_INTERVAL_SECONDS
            if not first and not due:
                self._fail_open_suppressed += 1
                return
            suppressed = self._fail_open_suppressed
            self._fail_open_last_log = now
            self._fail_open_suppressed = 0
        suffix = f" ({suppressed} similar failures suppressed)" if suppressed else ""
        logger.warning(
            "rate-limit store unavailable (%s: %s); allowing the request through. "
            "Rate limiting is DISABLED until the store recovers. storage_uri=%s%s",
            type(exc).__name__,
            exc,
            RATE_LIMIT_STORAGE,
            suffix,
        )

    def _check_request_limit(self, request, endpoint_func, in_middleware=True) -> None:  # type: ignore[override]
        try:
            return super()._check_request_limit(request, endpoint_func, in_middleware)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            self._log_fail_open(exc)
            # Swallowing the error is not enough on its own. SlowAPIMiddleware
            # treats "no exception" as "limits were evaluated" and goes on to
            # `_inject_headers(response, request.state.view_rate_limit)` -- and
            # that state is only set by the check we just aborted. Leaving it
            # unset trades the AttributeError for a different one, still 500 on
            # every route. Clear it explicitly and make injection a no-op below.
            try:
                request.state.view_rate_limit = None
            except Exception:  # pragma: no cover - request without a mutable state
                pass
            return None

    def _inject_headers(self, response, current_limit):  # type: ignore[override]
        """Skip X-RateLimit headers when no limit was actually evaluated.

        Pairs with the fail-open above: there are no meaningful numbers to
        report when the store never answered, and slowapi would raise
        unpacking ``None``.
        """
        if not current_limit:
            return response
        try:
            return super()._inject_headers(response, current_limit)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "could not inject rate-limit headers (%s: %s); serving the response as-is.",
                type(exc).__name__,
                exc,
            )
            return response


limiter = _FailOpenLimiter(
    key_func=_client_ip,
    default_limits=[RATE_LIMIT_DEFAULT] if RATE_LIMIT_ENABLED else [],
    storage_uri=RATE_LIMIT_STORAGE,
    enabled=RATE_LIMIT_ENABLED,
)


def rate_limit_exception_handler(request: Request, exc: Exception):
    """Handle limiter exceptions, failing OPEN when the store is unreachable.

    SlowAPI's middleware routes every exception raised while checking limits to
    the registered handler, and `_rate_limit_exceeded_handler` reads
    ``exc.detail`` unconditionally. That attribute exists only on
    ``RateLimitExceeded``. When the configured store cannot be reached the
    limiter raises the backend's error instead -- e.g. ``ConnectionError`` from
    redis -- and the handler dies with ``AttributeError: 'ConnectionError'
    object has no attribute 'detail'``, turning a *storage* outage into an
    unhandled 500 on EVERY route, ``/health`` included.

    An unreachable rate-limit store is an availability problem for the limiter,
    not for the API: rate limiting is a protective measure, so losing it must
    degrade to "unlimited", never to "everything is broken". Genuine limit
    violations still return 429 through SlowAPI's own handler.
    """
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    logger.warning(
        "rate-limit store unavailable (%s: %s); allowing the request through. "
        "Rate limiting is DISABLED until the store recovers.",
        type(exc).__name__,
        exc,
    )
    return None
