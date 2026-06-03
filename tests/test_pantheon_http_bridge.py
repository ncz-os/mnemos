"""Tests for the httpx/gateway -> NormalizedError classify bridge."""

from __future__ import annotations

import httpx

from mnemos.domain.pantheon.errors import ErrorClass
from mnemos.domain.pantheon.http_bridge import classify, retry_after_seconds


class _GatewayErrorLike(Exception):
    """Duck-typed stand-in for PantheonGatewayError (status_code + message)."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def test_gateway_error_400_bad_request():
    err = classify(_GatewayErrorLike(400, "bad request"))
    assert err.error_class is ErrorClass.BAD_REQUEST
    assert err.retryable is False


def test_gateway_error_503_service_unavailable_retryable():
    err = classify(_GatewayErrorLike(503, "upstream down"))
    assert err.error_class is ErrorClass.SERVICE_UNAVAILABLE
    assert err.retryable is True


def test_gateway_error_context_window_via_body():
    err = classify(_GatewayErrorLike(400, "This model's maximum context length is 8192 tokens"))
    assert err.error_class is ErrorClass.CONTEXT_WINDOW_EXCEEDED
    assert err.retryable is False


def test_httpx_connect_error_is_api_connection():
    err = classify(httpx.ConnectError("connection refused"))
    assert err.error_class is ErrorClass.API_CONNECTION
    assert err.retryable is True
    assert err.cooldownable is False


def test_httpx_timeout_is_timeout_retryable():
    err = classify(httpx.ReadTimeout("timed out"))
    assert err.error_class is ErrorClass.TIMEOUT
    assert err.retryable is True


def test_httpx_status_error_429():
    req = httpx.Request("POST", "https://api.example/v1/chat/completions")
    resp = httpx.Response(429, text="rate limited", request=req)
    err = classify(httpx.HTTPStatusError("429", request=req, response=resp))
    assert err.error_class is ErrorClass.RATE_LIMIT
    assert err.retryable is True


def test_unknown_exception_is_generic_api_error():
    err = classify(RuntimeError("something odd"))
    assert err.error_class is ErrorClass.API_ERROR
    assert err.retryable is True


def test_retry_after_numeric():
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(429, headers={"retry-after": "30"}, request=req)
    assert retry_after_seconds(resp) == 30.0


def test_retry_after_absent():
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(200, request=req)
    assert retry_after_seconds(resp) is None
