"""Bridge raw transport failures into the PANTHEON error taxonomy.

``classify`` turns whatever a provider call raises — an httpx transport error, an
httpx ``HTTPStatusError``, or PANTHEON's own ``PantheonGatewayError`` (matched by
duck-typing on ``status_code`` so this module does not import the gateway and
create a cycle) — into a :class:`NormalizedError`. This is the ``classify``
callback :class:`~mnemos.domain.pantheon.runtime.RouterRuntime` needs to drive
retry/fall-over/cooldown over real provider calls.

Pure aside from reading attributes off the exception/response.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from mnemos.domain.pantheon.errors import ErrorClass, NormalizedError, normalize_error

# Synthetic status used for client-side timeouts so they map to ErrorClass.TIMEOUT.
_TIMEOUT_STATUS = 408


def _body_of(exc: BaseException) -> str | None:
    for attr in ("message", "body", "detail"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    text = str(exc)
    return text or None


def classify(exc: BaseException) -> NormalizedError:
    """Map a raised provider-call exception to a :class:`NormalizedError`."""
    # httpx response-bearing error (raise_for_status / status checks).
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return normalize_error(status_code=resp.status_code, body=_safe_text(resp))
    # httpx client-side timeout -> TIMEOUT (retryable).
    if isinstance(exc, httpx.TimeoutException):
        return NormalizedError(ErrorClass.TIMEOUT, _TIMEOUT_STATUS, True, True, str(exc) or "timeout")
    # httpx transport/connection faults -> API_CONNECTION (retryable, never cools).
    if isinstance(exc, httpx.TransportError):
        return NormalizedError(ErrorClass.API_CONNECTION, None, True, False, str(exc) or "connection error")
    # Anything carrying a status_code (e.g. PantheonGatewayError) -> map by status+body.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return normalize_error(status_code=status, body=_body_of(exc))
    # Unknown failure: treat as a generic, retryable server-side API error.
    return NormalizedError(ErrorClass.API_ERROR, None, True, False, _body_of(exc) or "api error")


def _safe_text(response: Any) -> str | None:
    try:
        return response.text
    except Exception:  # noqa: BLE001 — body may not be readable; classification still works on status
        return None


def retry_after_seconds(response: Any) -> float | None:
    """Parse a Retry-After header (numeric seconds or HTTP-date) into seconds."""
    try:
        raw = response.headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(when.tzinfo) if when.tzinfo else _dt.datetime.now()
    delta = (when - now).total_seconds()
    return delta if delta > 0 else None
