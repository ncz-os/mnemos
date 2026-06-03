"""PANTHEON provider-error normalization + retry/fall-over gate.

Adopts LiteLLM's exception-mapping and should-retry *patterns* (not its code),
reimplemented natively so PANTHEON's router can decide, provider-agnostically:

1. what CLASS an upstream failure is (``normalize_error``),
2. whether it is safe to retry the *same* deployment (``is_retryable_status``),
3. whether it must fall over to the next deployment immediately (``decide``).

The module is pure: no I/O, no persistence, no tenancy, no provider SDK imports.
It is MVP step 1-2 of the routing redesign (MNEMOS mem_1780459842961).

The bug this module exists to make impossible: a single ``400`` (e.g. a
malformed-request error from one model) being treated as retryable and
cascading — retried then re-led — until it stalled every coding job on the
fleet. Under this module a non-retryable error falls over to the next
deployment instantly and never consumes the retry budget.

Retryable rule (LiteLLM ``_should_retry``): a status is retryable iff it is one
of ``{408, 409, 429}`` or ``>= 500``. Everything else in ``4xx`` — notably
``400``, ``401``, ``403``, ``404``, ``422`` — is non-retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Statuses that are safe to retry on the SAME deployment (transient).
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 429})

# Statuses that should trip a deployment cooldown when they occur (consumed by
# the later circuit-breaker step, kept here so the taxonomy is single-sourced).
# Per LiteLLM ``_is_cooldown_required``: among 4xx only these cool down; a plain
# 400/403/422 falls over but does NOT poison the deployment; APIConnection never
# cools down (it is a local/transport fault, not the deployment's fault).
COOLDOWN_STATUSES_4XX: frozenset[int] = frozenset({401, 404, 408, 429})

# Substring cues that reveal the real cause buried inside a generic 400 body.
_CONTEXT_WINDOW_CUES: tuple[str, ...] = (
    "maximum context length",
    "exceed context limit",
    "longer than the model's context length",
    "exceeds the maximum number of tokens",
    "current length is",  # Cerebras phrasing: "...while limit is"
    "context_length_exceeded",
)
_CONTENT_POLICY_CUES: tuple[str, ...] = (
    "content_policy_violation",
    "responsibleaipolicyviolation",
    "safety system",
    "content_filter_policy",
    "content management policy",
)
_RATE_LIMIT_CUES: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "request too large",
    "service tier capacity exceeded",
    "too many requests",
)
_API_CONNECTION_CUES: tuple[str, ...] = (
    "apiconnectionerror",
    "connection error",
    "connection refused",
    "connection reset",
    "max retries exceeded",
)


class ErrorClass(str, Enum):
    """Provider-agnostic error taxonomy (the common shape every provider's raw
    error is mapped into)."""

    RATE_LIMIT = "rate_limit"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    CONTENT_POLICY = "content_policy"
    AUTHENTICATION = "authentication"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    TIMEOUT = "timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    API_CONNECTION = "api_connection"
    API_ERROR = "api_error"


class RetryAction(str, Enum):
    """What the router should do with a failed attempt."""

    RETRY = "retry"  # transient on this deployment: back off, retry here
    FALLOVER = "fallover"  # do not retry here: advance to next deployment/group
    RAISE = "raise"  # terminal: surface to the caller


def is_retryable_status(status_code: int | None) -> bool:
    """LiteLLM ``_should_retry``: retryable iff status in {408,409,429} or >=500."""
    if status_code is None:
        return False
    return status_code in RETRYABLE_STATUSES or status_code >= 500


@dataclass(frozen=True)
class NormalizedError:
    """The normalized verdict for one upstream failure."""

    error_class: ErrorClass
    status_code: int | None
    retryable: bool
    cooldownable: bool
    message: str
    provider: str | None = None

    @property
    def is_context_window(self) -> bool:
        return self.error_class is ErrorClass.CONTEXT_WINDOW_EXCEEDED

    @property
    def is_content_policy(self) -> bool:
        return self.error_class is ErrorClass.CONTENT_POLICY


def _contains(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def _cooldownable(error_class: ErrorClass, status_code: int | None) -> bool:
    """Whether this failure should trip a deployment cooldown.

    APIConnection never cools down (local/transport fault). Among 4xx only
    401/404/408/429 cool down; other 4xx (400/403/422) fall over without
    poisoning the deployment. 5xx and unknown-status server faults cool down.
    """
    if error_class is ErrorClass.API_CONNECTION:
        return False
    if status_code is None:
        # Substring-classified server/rate faults still warrant a cooldown.
        return error_class in (ErrorClass.RATE_LIMIT, ErrorClass.SERVICE_UNAVAILABLE)
    if 400 <= status_code < 500:
        return status_code in COOLDOWN_STATUSES_4XX
    return status_code >= 500


def normalize_error(
    *,
    status_code: int | None = None,
    body: str | None = None,
    provider: str | None = None,
) -> NormalizedError:
    """Map a raw provider failure (HTTP status + body text) to a NormalizedError.

    ``body`` is the raw error string/JSON text; substring cues inside it are used
    to recover the real cause when a provider buries it inside a generic 400
    (context-window, content-policy) or omits the status entirely (rate-limit,
    connection). Classification precedence: body cues that change retryability
    are checked before the bare status code.
    """
    text = (body or "").lower()

    # 1. Body cues that override a generic status (these are all non-retryable).
    if _contains(text, _CONTEXT_WINDOW_CUES):
        return NormalizedError(
            ErrorClass.CONTEXT_WINDOW_EXCEEDED,
            status_code,
            False,
            _cooldownable(ErrorClass.CONTEXT_WINDOW_EXCEEDED, status_code),
            body or "context window exceeded",
            provider,
        )
    if _contains(text, _CONTENT_POLICY_CUES):
        return NormalizedError(
            ErrorClass.CONTENT_POLICY,
            status_code,
            False,
            _cooldownable(ErrorClass.CONTENT_POLICY, status_code),
            body or "content policy violation",
            provider,
        )
    # APIConnection / rate-limit can arrive with no usable status code.
    if _contains(text, _API_CONNECTION_CUES):
        return NormalizedError(
            ErrorClass.API_CONNECTION,
            status_code,
            True,
            False,
            body or "API connection error",
            provider,
        )
    if status_code is None and _contains(text, _RATE_LIMIT_CUES):
        return NormalizedError(
            ErrorClass.RATE_LIMIT,
            None,
            True,
            True,
            body or "rate limit",
            provider,
        )

    # 2. Map by status code.
    cls = _classify_status(status_code)
    return NormalizedError(
        cls,
        status_code,
        is_retryable_status(status_code),
        _cooldownable(cls, status_code),
        body or (cls.value if status_code is None else f"HTTP {status_code}"),
        provider,
    )


def _classify_status(status_code: int | None) -> ErrorClass:
    if status_code is None:
        return ErrorClass.API_ERROR
    if status_code == 429:
        return ErrorClass.RATE_LIMIT
    if status_code == 401:
        return ErrorClass.AUTHENTICATION
    if status_code == 403:
        return ErrorClass.PERMISSION_DENIED
    if status_code == 404:
        return ErrorClass.NOT_FOUND
    if status_code in (408, 504):
        return ErrorClass.TIMEOUT
    if status_code in (400, 422):
        return ErrorClass.BAD_REQUEST
    if status_code == 409:
        return ErrorClass.API_ERROR  # conflict/lock — retryable but not a class of its own
    if status_code >= 500:
        return ErrorClass.SERVICE_UNAVAILABLE
    return ErrorClass.API_ERROR


def decide(
    err: NormalizedError,
    *,
    num_deployments: int = 1,
    has_fallbacks: bool = False,
) -> RetryAction:
    """The non-retryable gate: given a normalized failure, decide RETRY here vs
    FALLOVER to the next deployment vs RAISE.

    Mirrors LiteLLM ``should_retry_this_error``. The load-bearing rule for the
    real outage: a non-retryable error (a plain 400, 404, 422, 403) NEVER
    returns RETRY — it falls over instantly (or raises if there is nowhere to
    fall over to), so it cannot consume the retry budget or stall the group.
    """
    # Context-window / content-policy: a different model may succeed.
    if err.error_class in (ErrorClass.CONTEXT_WINDOW_EXCEEDED, ErrorClass.CONTENT_POLICY):
        return RetryAction.FALLOVER if has_fallbacks else RetryAction.RAISE

    # Auth/permission: retrying the same credential is pointless. Try another
    # deployment if one exists, else give up.
    if err.error_class in (ErrorClass.AUTHENTICATION, ErrorClass.PERMISSION_DENIED):
        if num_deployments > 1 or has_fallbacks:
            return RetryAction.FALLOVER
        return RetryAction.RAISE

    # Non-retryable (400/404/422/...): fall over instantly, never retry.
    if not err.retryable:
        return RetryAction.FALLOVER if has_fallbacks else RetryAction.RAISE

    # Retryable transient (408/409/429/5xx/connection): retry this deployment.
    return RetryAction.RETRY
