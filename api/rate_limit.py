"""MNEMOS HTTP rate limiting.

Disabled by default (LAN deployments need no rate limiting).
Enable and tune via environment variables before exposing to the public internet:

    RATE_LIMIT_ENABLED=true          # opt-in
    RATE_LIMIT_DEFAULT=300/minute    # global ceiling per IP
    RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1  # or memory:// (single-worker only)

Route-specific limits (e.g. on /graeae/consult) are applied with @limiter.limit().
"""
import os

from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: F401 — re-exported
from slowapi.errors import RateLimitExceeded  # noqa: F401
from slowapi.middleware import SlowAPIMiddleware  # noqa: F401
from slowapi.util import get_remote_address

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "300/minute")
RATE_LIMIT_STORAGE = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT] if RATE_LIMIT_ENABLED else [],
    storage_uri=RATE_LIMIT_STORAGE,
    enabled=RATE_LIMIT_ENABLED,
)
