"""An unreachable rate-limit store must not take the API down.

Reproduces a live failure: the published `Dockerfile.core` sets
`MNEMOS_PROFILE=server`, which resolves `rate_limit.storage_uri` to
`redis://localhost:6379/1`, but the single-container image ships no Redis. Every
request then raised `ConnectionError` inside SlowAPI's limit check, which routes
exceptions to the registered handler; `_rate_limit_exceeded_handler` reads
`exc.detail`, an attribute only `RateLimitExceeded` has. The result was
`AttributeError: 'ConnectionError' object has no attribute 'detail'` and **HTTP
500 on every route, including `/health`**, on a container that otherwise started
cleanly and reported "Application startup complete".

Rate limiting is a protective measure. Losing its store must degrade to
"unlimited", never to "the API is broken".
"""

from __future__ import annotations

import logging

import pytest
from slowapi.errors import RateLimitExceeded

from mnemos.core.rate_limit import rate_limit_exception_handler


class _Req:
    """Minimal stand-in; the handler only needs an object to pass through."""


def test_store_outage_fails_open_instead_of_raising():
    """The exact production exception must be handled, not propagated."""
    result = rate_limit_exception_handler(_Req(), ConnectionError("Error 111 connecting to localhost:6379"))
    assert result is None, "a store outage must fail open, allowing the request"


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("redis down"),
        TimeoutError("redis timeout"),
        OSError("socket error"),
        RuntimeError("storage backend exploded"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_any_store_failure_fails_open(exc):
    """Any backend error, not just redis's, degrades to unlimited."""
    assert rate_limit_exception_handler(_Req(), exc) is None


def test_store_outage_is_logged_loudly(caplog):
    """Silently disabling rate limiting would be its own incident."""
    with caplog.at_level(logging.WARNING, logger="mnemos.core.rate_limit"):
        rate_limit_exception_handler(_Req(), ConnectionError("redis down"))
    assert any("rate-limit store unavailable" in r.message for r in caplog.records), (
        "losing rate limiting must be visible in the log"
    )


def test_genuine_limit_violation_still_returns_429(monkeypatch):
    """Failing open must not disable rate limiting itself."""
    import mnemos.core.rate_limit as rl

    called = {}

    def fake_handler(request, exc):
        called["hit"] = True
        return "429-response"

    monkeypatch.setattr(rl, "_rate_limit_exceeded_handler", fake_handler)
    exc = RateLimitExceeded.__new__(RateLimitExceeded)
    exc.detail = "300 per 1 minute"
    assert rl.rate_limit_exception_handler(_Req(), exc) == "429-response"
    assert called.get("hit"), "real violations must still route to SlowAPI's handler"
