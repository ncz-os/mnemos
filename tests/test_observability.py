"""Request-ID middleware + log-correlation contract tests (v3.2).

Covers:
  * Middleware generates an ID when none is sent.
  * Middleware honors a sane inbound X-Request-ID.
  * Middleware REJECTS a hostile inbound ID and falls back to a fresh one
    (too long, control chars, empty).
  * Response echoes the ID in X-Request-ID.
  * `current_request_id()` returns the bound ID inside a handler AND
    returns None outside a request.
  * `_RequestIDLogFilter` injects request_id onto log records.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.observability import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    _RequestIDLogFilter,
    current_request_id,
    install_log_correlation,
)


def _app():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo-rid")
    def _echo():
        return {"request_id": current_request_id()}

    return app


# ---- ID generation / pass-through ------------------------------------------


def test_middleware_generates_id_when_none_sent():
    client = TestClient(_app())
    resp = client.get("/echo-rid")
    assert resp.status_code == 200

    rid = resp.headers.get(REQUEST_ID_HEADER)
    assert rid, "response missing X-Request-ID header"
    # Fresh UUID4 hex is 32 chars, lowercase hex
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)

    # Handler saw the same ID via current_request_id()
    assert resp.json()["request_id"] == rid


def test_middleware_honors_sane_inbound_id():
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: "client-abc-123"})
    assert resp.status_code == 200
    assert resp.headers.get(REQUEST_ID_HEADER) == "client-abc-123"
    assert resp.json()["request_id"] == "client-abc-123"


def test_middleware_rejects_inbound_with_control_chars():
    """Control chars in X-Request-ID could smuggle newlines into logs
    (log-injection attack). The middleware must discard such values
    and generate a fresh ID."""
    hostile = "abc\r\n[INJECTED]"
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: hostile})
    assert resp.status_code == 200
    echoed = resp.headers.get(REQUEST_ID_HEADER)
    assert echoed != hostile
    assert len(echoed) == 32  # fresh UUID4 hex


def test_middleware_rejects_overlong_inbound():
    """An absurdly long inbound ID inflates every log line for the
    request's lifetime. The middleware caps at 128 chars; anything
    longer is discarded."""
    too_long = "a" * 500
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: too_long})
    assert resp.status_code == 200
    echoed = resp.headers.get(REQUEST_ID_HEADER)
    assert echoed != too_long
    assert len(echoed) == 32


def test_middleware_rejects_empty_inbound():
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: ""})
    assert resp.status_code == 200
    assert len(resp.headers[REQUEST_ID_HEADER]) == 32


def test_middleware_each_request_gets_distinct_id():
    client = TestClient(_app())
    a = client.get("/echo-rid").headers[REQUEST_ID_HEADER]
    b = client.get("/echo-rid").headers[REQUEST_ID_HEADER]
    assert a != b


# ---- context-var behavior outside a request --------------------------------


def test_current_request_id_is_none_outside_request():
    # No middleware has run — context is bare.
    assert current_request_id() is None


# ---- log-correlation filter -------------------------------------------------


def test_log_filter_attaches_request_id_to_records():
    """Outside a request, filter stamps '-'. Inside (via ContextVar
    manipulation), filter stamps the bound value."""
    filt = _RequestIDLogFilter()

    record_out = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="outside", args=(), exc_info=None,
    )
    assert filt.filter(record_out) is True
    assert record_out.request_id == "-"

    from api.observability import _request_id_ctx
    token = _request_id_ctx.set("bound-id-xyz")
    try:
        record_in = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="inside", args=(), exc_info=None,
        )
        assert filt.filter(record_in) is True
        assert record_in.request_id == "bound-id-xyz"
    finally:
        _request_id_ctx.reset(token)


def test_install_log_correlation_is_idempotent():
    """Calling install_log_correlation twice (e.g. on dev-server
    reloads) must not double-install the filter — the resulting log
    records should have a single request_id attribute, not multiple."""
    root = logging.getLogger()
    install_log_correlation()
    install_log_correlation()
    filter_count = sum(
        1 for f in root.filters if isinstance(f, _RequestIDLogFilter)
    )
    assert filter_count == 1


# ---- handler access via current_request_id() ------------------------------


def test_handler_sees_id_via_current_request_id():
    """Handlers get the request_id via `current_request_id()`. Starlette's
    BaseHTTPMiddleware uses a distinct Request for its dispatch, so we
    deliberately don't rely on request.state — the ContextVar is the
    single source of truth."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    captured = {}

    @app.get("/check-ctx")
    def _check():
        captured["ctx_rid"] = current_request_id()
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/check-ctx", headers={REQUEST_ID_HEADER: "ctx-test-42"})
    assert resp.status_code == 200
    assert captured["ctx_rid"] == "ctx-test-42"
    assert resp.headers[REQUEST_ID_HEADER] == "ctx-test-42"


# ---- Prometheus /metrics (v3.2 observability slice 2) ---------------------


def _metrics_app():
    """Build a minimal app with the Prometheus middleware + endpoint
    wired, mirroring api_server.py's pattern."""
    from api.observability import PrometheusMiddleware, metrics_router

    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    @app.get("/widgets/{widget_id}")
    def _get_widget(widget_id: str):
        return {"id": widget_id}

    @app.get("/fail")
    def _fail():
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="boom")

    return app


def test_metrics_endpoint_serves_prometheus_text():
    client = TestClient(_metrics_app())
    resp = client.get("/metrics")
    assert resp.status_code == 200
    ct = resp.headers["content-type"]
    # Accept either the canonical text/plain or versioned variant
    assert "text/plain" in ct


def test_metrics_counter_bumps_per_request():
    """Each request increments mnemos_http_requests_total by 1 with
    the matched route template, not the concrete URL."""
    client = TestClient(_metrics_app())
    client.get("/widgets/abc-1")
    client.get("/widgets/abc-2")

    body = client.get("/metrics").text
    # Route template is /widgets/{widget_id}, not /widgets/abc-1
    assert 'route="/widgets/{widget_id}"' in body
    assert 'route="/widgets/abc-1"' not in body
    # Two 2xx responses bumped the counter
    import re
    m = re.search(
        r'mnemos_http_requests_total\{[^}]*route="/widgets/\{widget_id\}"[^}]*\}\s+([0-9.]+)',
        body,
    )
    assert m, "expected counter line for /widgets/{widget_id}"
    assert float(m.group(1)) >= 2.0


def test_metrics_records_status_class_label():
    """5xx responses land under status='5xx' label, not '500'. Keeps
    cardinality bounded; alerting off status class is what operators
    actually want."""
    client = TestClient(_metrics_app())
    client.get("/fail", headers={}, follow_redirects=False)
    body = client.get("/metrics").text
    assert 'status="5xx"' in body


def test_metrics_histogram_present():
    """The latency histogram must appear for every served route. We
    don't assert specific bucket values (timing-dependent) — just
    that the metric family and a _count observation exist."""
    client = TestClient(_metrics_app())
    client.get("/widgets/xyz")
    body = client.get("/metrics").text
    assert "mnemos_http_request_duration_seconds" in body
    assert "mnemos_http_request_duration_seconds_count" in body


def test_metrics_unknown_route_bucketed_as_no_route():
    """404s for URLs that don't match any route go under
    route='__no_route__' rather than creating one time series per
    probed path. Protects against adversarial cardinality blow-up."""
    client = TestClient(_metrics_app())
    client.get("/does-not-exist")
    client.get("/also-fake")
    body = client.get("/metrics").text
    assert 'route="__no_route__"' in body
    assert 'route="/does-not-exist"' not in body
