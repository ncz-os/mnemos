"""MNEMOS HTTP rate limiting.

On by default (`RATE_LIMIT_ENABLED=true`). Tune via environment variables:

    RATE_LIMIT_ENABLED=true          # opt-out by setting "false"
    RATE_LIMIT_DEFAULT=300/minute    # global ceiling per client
    RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1  # or memory:// (single-worker)
    RATE_LIMIT_TRUST_PROXY=false     # set true ONLY behind a trusted reverse proxy
                                     # that rewrites X-Forwarded-For.

Route-specific limits (e.g. on `/v1/consultations`) are applied via
`@limiter.limit()` in the relevant handler.
"""
import logging

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


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate-limit bucketing.

    By default we trust only the direct TCP peer (safe anywhere). When
    RATE_LIMIT_TRUST_PROXY=true, we honour the left-most entry in
    X-Forwarded-For — only enable this when the server sits behind a proxy
    that you control and that strips client-supplied XFF headers, otherwise
    clients can spoof their IP and evade rate limits.
    """
    if RATE_LIMIT_TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Left-most is the original client per RFC convention.
            return xff.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return get_remote_address(request)


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

    def _check_request_limit(self, request, endpoint_func, in_middleware=True):  # type: ignore[override]
        try:
            return super()._check_request_limit(request, endpoint_func, in_middleware)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            logger.warning(
                "rate-limit store unavailable (%s: %s); allowing the request through. "
                "Rate limiting is DISABLED until the store recovers. storage_uri=%s",
                type(exc).__name__,
                exc,
                RATE_LIMIT_STORAGE,
            )
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
