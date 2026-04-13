# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""Prometheus Metrics Module for MNEMOS/GRAEAE Production Monitoring"""

import asyncio
import functools
import inspect
import time
from typing import Any, Callable

from fastapi import APIRouter
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest,
    CONTENT_TYPE_LATEST,
)
from prometheus_client import CollectorRegistry
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Slow query threshold (seconds)
SLOW_QUERY_THRESHOLD_SEC = 0.1

# Custom registry to avoid conflicts
metrics_registry = CollectorRegistry()

# ============================================================================
# MNEMOS Metrics (Core Memory System)
# ============================================================================

# Request metrics
mnemos_request_latency = Histogram(
    'mnemos_request_latency_seconds',
    'Request latency in seconds',
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    labelnames=['endpoint', 'method'],
    registry=metrics_registry
)

mnemos_request_count = Counter(
    'mnemos_request_count_total',
    'Total requests',
    labelnames=['endpoint', 'method', 'status'],
    registry=metrics_registry
)

mnemos_active_requests = Gauge(
    'mnemos_active_requests',
    'Number of active requests',
    labelnames=['endpoint'],
    registry=metrics_registry
)

# Cache metrics
mnemos_cache_hits = Counter(
    'mnemos_cache_hits_total',
    'Total cache hits',
    labelnames=['cache_type'],
    registry=metrics_registry
)

mnemos_cache_misses = Counter(
    'mnemos_cache_misses_total',
    'Total cache misses',
    labelnames=['cache_type'],
    registry=metrics_registry
)

mnemos_cache_size = Gauge(
    'mnemos_cache_size_bytes',
    'Cache size in bytes',
    labelnames=['cache_type'],
    registry=metrics_registry
)

# Database metrics
mnemos_db_query_latency = Histogram(
    'mnemos_db_query_latency_seconds',
    'Database query latency',
    buckets=(0.001, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    labelnames=['query_type'],
    registry=metrics_registry
)

mnemos_db_slow_queries = Counter(
    'mnemos_db_slow_queries_total',
    'Total slow queries (>100ms)',
    labelnames=['query_type'],
    registry=metrics_registry
)

mnemos_db_connection_pool = Gauge(
    'mnemos_db_connection_pool_size',
    'Database connection pool utilization',
    labelnames=['status'],
    registry=metrics_registry
)

# Embedding metrics
mnemos_embedding_queue_size = Gauge(
    'mnemos_embedding_queue_size',
    'Current embedding generation queue size',
    registry=metrics_registry
)

mnemos_embedding_latency = Histogram(
    'mnemos_embedding_latency_seconds',
    'Embedding generation latency',
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=metrics_registry
)

mnemos_embedding_batch_size = Histogram(
    'mnemos_embedding_batch_size',
    'Embedding batch sizes processed',
    buckets=(10, 50, 100, 250, 500, 1000),
    registry=metrics_registry
)

# Worker metrics
mnemos_worker_utilization = Gauge(
    'mnemos_worker_utilization_percent',
    'Worker utilization percentage',
    labelnames=['worker_id'],
    registry=metrics_registry
)

mnemos_search_latency = Histogram(
    'mnemos_search_latency_seconds',
    'Memory search latency',
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    labelnames=['search_type'],
    registry=metrics_registry
)

# ============================================================================
# GRAEAE Metrics (Reasoning Engine)
# ============================================================================

# Provider metrics
graeae_provider_latency = Histogram(
    'graeae_provider_latency_seconds',
    'Provider response latency',
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    labelnames=['provider'],
    registry=metrics_registry
)

graeae_provider_success = Counter(
    'graeae_provider_success_total',
    'Successful provider responses',
    labelnames=['provider'],
    registry=metrics_registry
)

graeae_provider_failure = Counter(
    'graeae_provider_failure_total',
    'Failed provider responses',
    labelnames=['provider', 'error_type'],
    registry=metrics_registry
)

graeae_provider_last_success = Gauge(
    'graeae_provider_last_success_timestamp',
    'Timestamp of last successful response from provider',
    labelnames=['provider'],
    registry=metrics_registry
)

graeae_provider_health = Gauge(
    'graeae_provider_health_status',
    'Provider health status (1=healthy, 0=unhealthy)',
    labelnames=['provider'],
    registry=metrics_registry
)

# Consensus metrics
graeae_consensus_score = Histogram(
    'graeae_consensus_score',
    'Consensus decision score distribution',
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=metrics_registry
)

graeae_consensus_latency = Histogram(
    'graeae_consensus_latency_seconds',
    'Consensus voting latency',
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=metrics_registry
)

# Queue metrics
graeae_queue_depth = Gauge(
    'graeae_queue_depth',
    'Current depth of consultation queue',
    registry=metrics_registry
)

graeae_queue_wait_latency = Histogram(
    'graeae_queue_wait_latency_seconds',
    'Time spent waiting in queue',
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0),
    registry=metrics_registry
)

# Concurrent connection metrics
graeae_concurrent_connections = Gauge(
    'graeae_concurrent_connections',
    'Current concurrent connections',
    registry=metrics_registry
)

graeae_concurrent_connections_peak = Gauge(
    'graeae_concurrent_connections_peak',
    'Peak concurrent connections',
    registry=metrics_registry
)

# Worker metrics
graeae_worker_active = Gauge(
    'graeae_worker_active_count',
    'Number of active workers',
    registry=metrics_registry
)

# asyncio tasks gauge (replaces gevent greenlet gauge)
asyncio_tasks_active = Gauge(
    'asyncio_tasks_active',
    'Number of active asyncio tasks',
    registry=metrics_registry
)

# ============================================================================
# Utility Functions
# ============================================================================

def track_endpoint_latency(endpoint_name: str) -> Callable:
    """Decorator to track endpoint latency and requests (sync and async)."""
    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                method = kwargs.get('method', 'unknown')
                mnemos_active_requests.labels(endpoint=endpoint_name).inc()
                status = 200
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception:
                    status = 500
                    raise
                finally:
                    latency = time.time() - start_time
                    mnemos_active_requests.labels(endpoint=endpoint_name).dec()
                    mnemos_request_latency.labels(
                        endpoint=endpoint_name,
                        method=method
                    ).observe(latency)
                    mnemos_request_count.labels(
                        endpoint=endpoint_name,
                        method=method,
                        status=status
                    ).inc()
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                method = kwargs.get('method', 'unknown')
                mnemos_active_requests.labels(endpoint=endpoint_name).inc()
                status = 200
                try:
                    result = func(*args, **kwargs)
                    return result
                except Exception:
                    status = 500
                    raise
                finally:
                    latency = time.time() - start_time
                    mnemos_active_requests.labels(endpoint=endpoint_name).dec()
                    mnemos_request_latency.labels(
                        endpoint=endpoint_name,
                        method=method
                    ).observe(latency)
                    mnemos_request_count.labels(
                        endpoint=endpoint_name,
                        method=method,
                        status=status
                    ).inc()
            return sync_wrapper
    return decorator


def track_db_query_latency(query_type: str) -> Callable:
    """Decorator to track database query latency (sync and async)."""
    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    return result
                finally:
                    latency = time.time() - start_time
                    mnemos_db_query_latency.labels(query_type=query_type).observe(latency)
                    if latency > SLOW_QUERY_THRESHOLD_SEC:
                        mnemos_db_slow_queries.labels(query_type=query_type).inc()
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    return result
                finally:
                    latency = time.time() - start_time
                    mnemos_db_query_latency.labels(query_type=query_type).observe(latency)
                    if latency > SLOW_QUERY_THRESHOLD_SEC:
                        mnemos_db_slow_queries.labels(query_type=query_type).inc()
            return sync_wrapper
    return decorator


def track_provider_latency(provider_name: str) -> Callable:
    """Decorator to track GRAEAE provider latency (sync and async)."""
    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    latency = time.time() - start_time
                    graeae_provider_latency.labels(provider=provider_name).observe(latency)
                    graeae_provider_success.labels(provider=provider_name).inc()
                    graeae_provider_last_success.labels(provider=provider_name).set_to_current_time()
                    return result
                except Exception as e:
                    graeae_provider_failure.labels(
                        provider=provider_name,
                        error_type=type(e).__name__
                    ).inc()
                    raise
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    latency = time.time() - start_time
                    graeae_provider_latency.labels(provider=provider_name).observe(latency)
                    graeae_provider_success.labels(provider=provider_name).inc()
                    graeae_provider_last_success.labels(provider=provider_name).set_to_current_time()
                    return result
                except Exception as e:
                    graeae_provider_failure.labels(
                        provider=provider_name,
                        error_type=type(e).__name__
                    ).inc()
                    raise
            return sync_wrapper
    return decorator


def create_metrics_router() -> APIRouter:
    """Create FastAPI APIRouter with GET /metrics endpoint."""
    router = APIRouter()

    @router.get('/metrics')
    async def metrics() -> Response:
        asyncio_tasks_active.set(len(asyncio.all_tasks()))
        return Response(
            generate_latest(metrics_registry),
            media_type=CONTENT_TYPE_LATEST
        )

    return router


# ============================================================================
# Initialization
# ============================================================================

def setup_metrics_middleware(app: Any) -> None:
    """Install Prometheus metrics middleware into a FastAPI app."""

    class _MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable) -> Any:
            start_time = time.time()
            endpoint = request.url.path
            method = request.method

            mnemos_active_requests.labels(endpoint=endpoint).inc()
            try:
                response = await call_next(request)
                status = response.status_code
            except Exception:
                status = 500
                raise
            finally:
                latency = time.time() - start_time
                mnemos_active_requests.labels(endpoint=endpoint).dec()
                mnemos_request_latency.labels(
                    endpoint=endpoint,
                    method=method
                ).observe(latency)
                mnemos_request_count.labels(
                    endpoint=endpoint,
                    method=method,
                    status=status
                ).inc()
            return response

    app.add_middleware(_MetricsMiddleware)
