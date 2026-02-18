"""Prometheus Metrics Module for MNEMOS/GRAEAE Production Monitoring"""

from prometheus_client import Counter, Histogram, Gauge, generate_latest, REGISTRY
from prometheus_client.core import CollectorRegistry
from flask import Blueprint, Response
import time
import functools

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

graeae_greenlet_count = Gauge(
    'graeae_greenlet_count',
    'Active gevent greenlets',
    registry=metrics_registry
)

# ============================================================================
# Utility Functions
# ============================================================================

def track_endpoint_latency(endpoint_name):
    """Decorator to track endpoint latency and requests"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            method = 'GET'  # Will be overridden by request context
            
            mnemos_active_requests.labels(endpoint=endpoint_name).inc()
            
            try:
                result = func(*args, **kwargs)
                status = 200
                return result
            except Exception as e:
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
        
        return wrapper
    return decorator


def track_db_query_latency(query_type):
    """Decorator to track database query latency"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                latency = time.time() - start_time
                mnemos_db_query_latency.labels(query_type=query_type).observe(latency)
                
                if latency > 0.1:  # 100ms threshold
                    mnemos_db_slow_queries.labels(query_type=query_type).inc()
        
        return wrapper
    return decorator


def track_provider_latency(provider_name):
    """Decorator to track GRAEAE provider latency"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
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
        
        return wrapper
    return decorator


def create_metrics_blueprint():
    """Create Flask blueprint for /metrics endpoint"""
    bp = Blueprint('metrics', __name__)
    
    @bp.route('/metrics', methods=['GET'])
    def metrics():
        return Response(generate_latest(metrics_registry), mimetype='text/plain')
    
    return bp


# ============================================================================
# Initialization
# ============================================================================

def setup_prometheus_middleware(app):
    """Setup Prometheus metrics middleware in Flask app"""
    
    # Register metrics blueprint
    metrics_bp = create_metrics_blueprint()
    app.register_blueprint(metrics_bp)
    
    # Before/after request hooks
    @app.before_request
    def before_request():
        import time
        from flask import request
        request.start_time = time.time()
        
        endpoint = request.endpoint or 'unknown'
        mnemos_active_requests.labels(endpoint=endpoint).inc()
    
    @app.after_request
    def after_request(response):
        import time
        from flask import request
        
        if hasattr(request, 'start_time'):
            latency = time.time() - request.start_time
            endpoint = request.endpoint or 'unknown'
            
            mnemos_active_requests.labels(endpoint=endpoint).dec()
            mnemos_request_latency.labels(
                endpoint=endpoint,
                method=request.method
            ).observe(latency)
            mnemos_request_count.labels(
                endpoint=endpoint,
                method=request.method,
                status=response.status_code
            ).inc()
        
        return response
    
    return app


if __name__ == '__main__':
    # Test metrics endpoint
    print(generate_latest(metrics_registry).decode('utf-8'))
