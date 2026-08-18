"""A dead rate-limit store must not 500 the API -- proven through the middleware.

There is already a test that calls ``rate_limit_exception_handler`` directly and
asserts it fails open. It passed while the service was still dying, because the
handler is never reached on the path that actually breaks.

``slowapi.middleware._check_limits`` resolves the handler like this:

    except Exception as e:
        exception_handler = app.exception_handlers.get(
            type(e), _rate_limit_exceeded_handler
        )

That is an EXACT-TYPE lookup, and ``main.py`` registers our handler only for
``RateLimitExceeded``. A store outage raises the backend's own error --
``ConnectionError`` for redis -- which matches nothing, so slowapi falls back to
its own handler, reads ``exc.detail``, and raises

    AttributeError: 'ConnectionError' object has no attribute 'detail'

straight out of ``dispatch``: an unhandled 500 on every route including
``/health``. Registering for ``Exception`` does not fix it either, because the
lookup is not subclass-aware.

So these tests drive a real request through SlowAPIMiddleware with a limiter
whose store raises. That is the path that failed on the arm64 6.1.6 image, where
the `server` profile points at redis://localhost:6379/1 and the image ships no
redis.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from mnemos.core.rate_limit import _FailOpenLimiter, rate_limit_exception_handler


def _app_with_store_error(exc: Exception) -> FastAPI:
    """An app whose limiter store always raises ``exc``."""
    app = FastAPI()
    limiter = _FailOpenLimiter(key_func=lambda *a, **k: "test-client", default_limits=["100/minute"])

    def _boom(*args, **kwargs):
        raise exc

    # Break it the way an unreachable store does: the rate-limiting strategy
    # itself raises when it touches storage.
    limiter._limiter = type(
        "_DeadStrategy", (), {"hit": _boom, "test": _boom, "get_window_stats": _boom}
    )()

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("Error 111 connecting to localhost:6379. Connection refused."),
        TimeoutError("redis timeout"),
        OSError("socket error"),
        RuntimeError("storage backend exploded"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_health_still_answers_when_the_store_is_down(exc):
    """The exact production symptom: /health must not 500."""
    client = TestClient(_app_with_store_error(exc), raise_server_exceptions=False)
    resp = client.get("/health")
    assert resp.status_code == 200, (
        f"a {type(exc).__name__} from the rate-limit store returned "
        f"{resp.status_code}; rate limiting must fail OPEN, not take the API down"
    )
    assert resp.json() == {"status": "ok"}


def test_the_outage_is_logged_loudly(caplog):
    """Silently disabling rate limiting would be its own incident."""
    import logging

    client = TestClient(
        _app_with_store_error(ConnectionError("redis down")), raise_server_exceptions=False
    )
    with caplog.at_level(logging.WARNING, logger="mnemos.core.rate_limit"):
        client.get("/health")
    assert any("rate-limit store unavailable" in r.message for r in caplog.records), (
        "losing rate limiting must be visible in the log"
    )


def test_a_real_violation_still_returns_429():
    """Failing open on store errors must not disable rate limiting itself."""
    app = FastAPI()
    limiter = _FailOpenLimiter(key_func=lambda *a, **k: "same-client", default_limits=["1/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 429, (
        "a genuine limit violation must still be rejected"
    )


def test_rate_limit_exceeded_is_not_swallowed_by_the_limiter():
    """The override must re-raise RateLimitExceeded, not treat it as an outage."""
    limiter = _FailOpenLimiter(key_func=lambda *a, **k: "c", default_limits=["1/minute"])
    exc = RateLimitExceeded.__new__(RateLimitExceeded)
    exc.detail = "1 per 1 minute"

    def _raise(*a, **k):
        raise exc

    import slowapi

    original = slowapi.Limiter._check_request_limit
    try:
        slowapi.Limiter._check_request_limit = _raise
        with pytest.raises(RateLimitExceeded):
            limiter._check_request_limit(None, None, True)
    finally:
        slowapi.Limiter._check_request_limit = original
