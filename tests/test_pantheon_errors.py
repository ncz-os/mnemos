"""Tests for PANTHEON provider-error normalization + retry/fall-over gate.

Locks the two behaviors the module exists to guarantee:
  * the retryable rule (only 408/409/429/>=500 retry),
  * a non-retryable 400 falls over instantly and is NEVER retried (the outage).
"""

from __future__ import annotations

import pytest

from mnemos.domain.pantheon.errors import (
    ErrorClass,
    NormalizedError,
    RetryAction,
    decide,
    is_retryable_status,
    normalize_error,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        (None, False),
        (200, False),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (422, False),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
    ],
)
def test_is_retryable_status(status, expected):
    assert is_retryable_status(status) is expected


@pytest.mark.parametrize(
    "status,cls,retryable,cooldownable",
    [
        (400, ErrorClass.BAD_REQUEST, False, False),
        (401, ErrorClass.AUTHENTICATION, False, True),
        (403, ErrorClass.PERMISSION_DENIED, False, False),
        (404, ErrorClass.NOT_FOUND, False, True),
        (408, ErrorClass.TIMEOUT, True, True),
        (422, ErrorClass.BAD_REQUEST, False, False),
        (429, ErrorClass.RATE_LIMIT, True, True),
        (500, ErrorClass.SERVICE_UNAVAILABLE, True, True),
        (503, ErrorClass.SERVICE_UNAVAILABLE, True, True),
    ],
)
def test_normalize_by_status(status, cls, retryable, cooldownable):
    err = normalize_error(status_code=status, provider="openai")
    assert err.error_class is cls
    assert err.retryable is retryable
    assert err.cooldownable is cooldownable
    assert err.provider == "openai"


def test_context_window_detected_inside_400():
    err = normalize_error(
        status_code=400,
        body="This model's maximum context length is 8192 tokens, however you requested 9000",
    )
    assert err.error_class is ErrorClass.CONTEXT_WINDOW_EXCEEDED
    assert err.is_context_window is True
    assert err.retryable is False
    assert err.cooldownable is False  # a 400 never poisons the deployment


def test_content_policy_detected_inside_400():
    err = normalize_error(status_code=400, body="content_policy_violation: blocked by safety system")
    assert err.error_class is ErrorClass.CONTENT_POLICY
    assert err.is_content_policy is True
    assert err.retryable is False


def test_rate_limit_detected_without_status():
    err = normalize_error(body="Rate limit reached for gpt-5.5; too many requests")
    assert err.error_class is ErrorClass.RATE_LIMIT
    assert err.retryable is True
    assert err.cooldownable is True


def test_api_connection_never_cools_down():
    err = normalize_error(body="APIConnectionError: Connection refused")
    assert err.error_class is ErrorClass.API_CONNECTION
    assert err.retryable is True
    assert err.cooldownable is False


# ── The non-retryable gate (decide) ─────────────────────────────────────────


def test_400_falls_over_never_retries_with_fallbacks():
    err = normalize_error(status_code=400, body="bad request")
    assert decide(err, num_deployments=3, has_fallbacks=True) is RetryAction.FALLOVER


def test_400_raises_when_no_fallbacks():
    err = normalize_error(status_code=400)
    assert decide(err, num_deployments=1, has_fallbacks=False) is RetryAction.RAISE


def test_the_outage_regression_400_missing_input_never_retries():
    """The real incident: a model returned 400 'missing_required_parameter:
    input'. It must fall over to the next deployment, never RETRY, so it cannot
    cascade and stall the group."""
    err = normalize_error(
        status_code=400,
        body="OpenAI Codex API error (400): missing_required_parameter: input",
        provider="openai",
    )
    assert err.retryable is False
    assert decide(err, num_deployments=2, has_fallbacks=True) is RetryAction.FALLOVER
    assert decide(err, num_deployments=2, has_fallbacks=True) is not RetryAction.RETRY


@pytest.mark.parametrize("status", [429, 500, 503, 408, 409])
def test_transient_retries_on_same_deployment(status):
    err = normalize_error(status_code=status)
    assert decide(err, num_deployments=2, has_fallbacks=True) is RetryAction.RETRY


def test_context_window_falls_over_then_raises():
    err = normalize_error(status_code=400, body="exceeds the maximum number of tokens")
    assert decide(err, has_fallbacks=True) is RetryAction.FALLOVER
    assert decide(err, has_fallbacks=False) is RetryAction.RAISE


def test_auth_single_deployment_raises_multi_falls_over():
    err = normalize_error(status_code=401)
    assert decide(err, num_deployments=1, has_fallbacks=False) is RetryAction.RAISE
    assert decide(err, num_deployments=3, has_fallbacks=False) is RetryAction.FALLOVER


def test_normalized_error_is_frozen():
    err = NormalizedError(ErrorClass.BAD_REQUEST, 400, False, False, "x")
    with pytest.raises(Exception):
        err.status_code = 500  # type: ignore[misc]
