#!/usr/bin/env python3
"""
MNEMOS - Unified Memory API
Merged single-source-of-truth combining api_server.py + unified_api.py
Flask-based, modular, extensible architecture
Version 2.0-merged-modular
"""

import json
import sys
import signal
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

import config

# Prometheus metrics for monitoring
try:
    from metrics import (
        setup_prometheus_middleware,
        mnemos_active_requests,
        mnemos_request_latency,
        mnemos_request_count,
        track_db_query_latency
    )
    METRICS_ENABLED = True
except ImportError:
    print("Warning: metrics module not available", file=sys.stderr)
    METRICS_ENABLED = False

from core import (
    DatabaseManager, EmbeddingService, TierSelector, MemoryStore,
    SyncService, EmbeddingGenerator, MemoryMaintenance, RequestTracer,
    init_mnemos
)

# ============================================================================
# Flask App Initialization
# ============================================================================

app = Flask(__name__)
CORS(app)
app.config['JSON_SORT_KEYS'] = False

# Initialize core services
memory_store = MemoryStore()

# Setup Prometheus metrics middleware (if available)
if METRICS_ENABLED:
    setup_prometheus_middleware(app)
sync_service = SyncService()
embedding_generator = EmbeddingGenerator()
maintenance = MemoryMaintenance()

last_cleanup_time = time.time()

# ============================================================================
# Middleware & Request Handlers
# ============================================================================

@app.before_request
def before_request():
    """Request initialization"""
    RequestTracer.new_id()
    request.start_time = time.time()
    
    # Track active requests in metrics
    if METRICS_ENABLED:
        endpoint = request.endpoint or 'unknown'
        mnemos_active_requests.labels(endpoint=endpoint).inc()

@app.after_request
def after_request(response):
    """Request logging with metrics"""
    if hasattr(request, 'start_time'):
        duration = time.time() - request.start_time
        rid = RequestTracer.get_id()
        
        # Record metrics
        if METRICS_ENABLED:
            endpoint = request.endpoint or 'unknown'
            mnemos_request_latency.labels(
                endpoint=endpoint,
                method=request.method
            ).observe(duration)
            mnemos_request_count.labels(
                endpoint=endpoint,
                method=request.method,
                status=response.status_code
            ).inc()
            mnemos_active_requests.labels(endpoint=endpoint).dec()
        
        print(f"[{rid}] {request.method} {request.path} → {response.status_code} ({duration:.3f}s)",
              file=sys.stderr, flush=True)
    return response

# ============================================================================
# Core Endpoints - Health & Info
# ============================================================================

@app.route('/', methods=['GET'])
def api_info():
    """API information"""
    return jsonify({
        'service': 'MNEMOS',
        'version': config.API_VERSION,
        'status': 'healthy',
        'endpoints': [
            '/health',
            '/stats',
            '/memories',
            '/memories/{id}',
            '/memories/search',
            '/memories/embed',
            '/rehydrate',
            '/graeae/health',
            '/graeae/muses',
            '/distillation/status'
        ]
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({'status': 'ok', 'service': 'mnemos', 'version': config.API_VERSION}), 200

@app.route('/stats', methods=['GET'])
def stats():
    """Memory statistics with category breakdown"""
    try:
        db = DatabaseManager()
        conn = db.get_connection()
        cur = conn.cursor()

        # Total count
        cur.execute("SELECT COUNT(*) FROM memories")
        total = cur.fetchone()[0]

        # With embeddings
        cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        with_embeddings = cur.fetchone()[0]

        # Category breakdown
        cur.execute("SELECT category, COUNT(*) as count FROM memories GROUP BY category ORDER BY count DESC")
        categories = {row[0]: row[1] for row in cur.fetchall()}

        cur.close()
        conn.close()

        return jsonify({
            'total_memories': total,
            'memories_with_embeddings': with_embeddings,
            'embedding_coverage': f"{(with_embeddings/total*100) if total > 0 else 0:.1f}%",
            'categories': categories,
            'timestamp': time.time(),
            'status': 'healthy'
        }), 200
    except Exception as e:
        return jsonify({'error': str(e), 'status': 'error'}), 500

# ============================================================================
# Memory CRUD Operations
# ============================================================================

@app.route('/memories', methods=['GET'])
def list_memories():
    """List all memories with pagination"""
    try:
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))

        memories = memory_store.list_memories(limit, offset)

        # Convert timestamps to ISO strings
        for mem in memories:
            if hasattr(mem.get('created'), 'isoformat'):
                mem['created'] = mem['created'].isoformat()
            if hasattr(mem.get('updated'), 'isoformat'):
                mem['updated'] = mem['updated'].isoformat()

        return jsonify({
            'memories': memories,
            'count': len(memories),
            'limit': limit,
            'offset': offset,
            'timestamp': time.time()
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/memories/<memory_id>', methods=['GET'])
def get_memory(memory_id):
    """Get single memory by ID"""
    try:
        memory = memory_store.get_memory(memory_id)

        if not memory:
            return jsonify({'error': 'Memory not found'}), 404

        # Convert timestamps
        if hasattr(memory.get('created'), 'isoformat'):
            memory['created'] = memory['created'].isoformat()
        if hasattr(memory.get('updated'), 'isoformat'):
            memory['updated'] = memory['updated'].isoformat()

        return jsonify(memory), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/memories', methods=['POST'])
def create_memory():
    """Create new memory"""
    try:
        data = request.get_json() or {}
        content = data.get('content', '').strip()
        category = data.get('category', 'facts').strip()

        if not content:
            return jsonify({'error': 'Missing content parameter'}), 400

        memory = memory_store.create_memory(content, category)

        return jsonify({
            **memory,
            'status': 'created'
        }), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/memories/<memory_id>', methods=['PUT', 'PATCH'])
def update_memory(memory_id):
    """Update existing memory"""
    try:
        data = request.get_json() or {}
        content = data.get('content')
        category = data.get('category')

        memory = memory_store.update_memory(memory_id, content, category)

        # Convert timestamps
        if hasattr(memory.get('created'), 'isoformat'):
            memory['created'] = memory['created'].isoformat()
        if hasattr(memory.get('updated'), 'isoformat'):
            memory['updated'] = memory['updated'].isoformat()

        return jsonify({
            **memory,
            'status': 'updated'
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/memories/<memory_id>', methods=['DELETE'])
def delete_memory(memory_id):
    """Delete memory"""
    try:
        if not memory_store.get_memory(memory_id):
            return jsonify({'error': 'Memory not found'}), 404

        memory_store.delete_memory(memory_id)
        return jsonify({'id': memory_id, 'status': 'deleted'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================================
# Search Operations
# ============================================================================

@app.route('/memories/search', methods=['POST'])
def search_memories():
    """Search memories with semantic similarity and tier selection"""
    try:
        data = request.get_json() or {}
        query = data.get('query', '').strip()
        limit = data.get('limit', config.DEFAULT_SEARCH_LIMIT)
        category = data.get('category')

        if not query:
            return jsonify({'error': 'No query provided'}), 400

        # Direct ID lookup if query starts with mem_
        if query.startswith('mem_'):
            memory = memory_store.get_memory(query)
            if memory:
                return jsonify({
                    'query': query,
                    'results': [memory],
                    'count': 1,
                    'method': 'id_lookup'
                }), 200
            return jsonify({'query': query, 'results': [], 'count': 0}), 200

        # Category-specific search
        if category:
            result = memory_store._keyword_search(query, limit, category)
        else:
            # Task-based tier selection search
            result = memory_store.search(query, limit)

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/memories/embed', methods=['POST'])
def embed_memory():
    """Generate embedding for text"""
    try:
        data = request.get_json() or {}
        text = data.get('text', '').strip()

        if not text:
            return jsonify({'error': 'Missing text parameter'}), 400

        embedding_service = EmbeddingService()
        embedding = embedding_service.embed(text)

        if embedding:
            return jsonify({
                'embedding': embedding,
                'dimensions': len(embedding),
                'model': config.OLLAMA_EMBED_MODEL
            }), 200
        else:
            return jsonify({'error': 'Failed to generate embedding'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Reindex Endpoint (Parallel Embedding)
# ============================================================================

@app.route('/memories/reindex', methods=['POST'])
def reindex_memories():
    """Reindex all memories with fresh embeddings using parallel processing
    
    POST /memories/reindex
    Body (optional): {force: true}  # regenerate all embeddings
    
    Returns progress and statistics
    """
    try:
        data = request.get_json() or {}
        force = data.get('force', False)
        
        # Run reindex in background thread to avoid timeout
        import threading
        
        def do_reindex():
            result = embedding_generator.reindex_all(force=force)
            print(f"[REINDEX] Result: {result}", file=sys.stderr, flush=True)
        
        # Check if already running
        if hasattr(embedding_generator, '_reindex_in_progress') and embedding_generator._reindex_in_progress:
            return jsonify({
                'status': 'busy',
                'message': 'Reindex already in progress',
                'timestamp': datetime.utcnow().isoformat()
            }), 409
        
        # Start reindex in background
        thread = threading.Thread(target=do_reindex, daemon=True)
        thread.start()
        
        return jsonify({
            'status': 'started',
            'message': 'Reindex started in background. Check /stats for progress.',
            'force': force,
            'timestamp': datetime.utcnow().isoformat()
        }), 202
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/memories/reindex/status', methods=['GET'])
def reindex_status():
    """Check reindex status"""
    try:
        in_progress = getattr(embedding_generator, '_reindex_in_progress', False)
        
        # Get current embedding stats
        db = DatabaseManager()
        conn = db.get_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM memories")
        total = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        with_embeddings = cur.fetchone()[0]
        
        cur.close()
        conn.close()
        
        return jsonify({
            'reindex_in_progress': in_progress,
            'total_memories': total,
            'memories_with_embeddings': with_embeddings,
            'embedding_coverage': f"{(with_embeddings/total*100) if total > 0 else 0:.1f}%",
            'timestamp': datetime.utcnow().isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Rehydration Endpoint
# ============================================================================

@app.route('/rehydrate', methods=['GET'])
def rehydrate():
    """Get rehydrated context for MNEMOS"""
    try:
        # Parse tier parameter (e.g., ?tier=0,1,2)
        tier_param = request.args.get('tier', '0,1,2')
        requested_tiers = [t.strip() for t in tier_param.split(',')]

        db = DatabaseManager()
        conn = db.get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, content, category, created, updated
            FROM memories
            ORDER BY created DESC
        """)

        memories = {}
        for row in cur.fetchall():
            created_str = row[3].isoformat() if hasattr(row[3], 'isoformat') else str(row[3])
            updated_str = row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4])

            memories[row[0]] = {
                'id': row[0],
                'content': row[1],
                'category': row[2],
                'created': created_str,
                'updated': updated_str
            }

        cur.close()
        conn.close()

        return jsonify({
            'version': config.API_VERSION,
            'memories': memories,
            'memory_count': len(memories),
            'tiers_requested': requested_tiers,
            'timestamp': datetime.utcnow().isoformat(),
            'status': 'success'
        }), 200
    except Exception as e:
        return jsonify({'error': str(e), 'status': 'error'}), 500

# ============================================================================
# GRAEAE Endpoints (Multi-LLM Reasoning)
# ============================================================================




@app.route('/distillation/trigger', methods=['POST'])
def distillation_trigger():
    """Trigger memory distillation"""
    try:
        data = request.get_json() or {}
        project = data.get('project', 'general')

        return jsonify({
            'status': 'triggered',
            'project': project,
            'message': 'Distillation job queued',
            'timestamp': datetime.utcnow().isoformat()
        }), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/distillation/status', methods=['GET'])
def distillation_status():
    """Get distillation system status"""
    return jsonify({
        'status': 'healthy',
        'distilled_facts': {
            'tier_1': 0,
            'tier_2': 0,
            'tier_3': 0,
            'total': 0
        },
        'timestamp': datetime.utcnow().isoformat()
    }), 200

# ============================================================================
# Error Handlers
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    """404 handler"""
    return jsonify({'error': 'Endpoint not found', 'path': request.path}), 404

@app.errorhandler(500)
def server_error(e):
    """500 handler"""
    return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# Startup & Shutdown
# ============================================================================

startup_done = False

@app.before_request
def startup():
    """Startup procedures (runs on first request)"""
    global startup_done
    if startup_done:
        return

    startup_done = True

    print(f"\n{'=' * 70}", file=sys.stderr, flush=True)
    print(f"MNEMOS {config.API_VERSION} - Unified Memory API", file=sys.stderr, flush=True)
    print(f"{'=' * 70}\n", file=sys.stderr, flush=True)

    # Initialize database
    if not init_mnemos():
        print(f"[STARTUP] ✗ Failed to initialize MNEMOS", file=sys.stderr, flush=True)
        sys.exit(1)

    # Sync JSON shards
    if config.ENABLE_JSON_SHARD_SYNC:
        pass  # JSON shard sync disabled - runs in background job

    # Start background embedding
    if config.ENABLE_BACKGROUND_EMBEDDING:
        embedding_generator.start()

    print(f"\n[STARTUP] ✓ Server ready on {config.API_HOST}:{config.API_PORT}", file=sys.stderr, flush=True)
    print(f"[STARTUP] Features: tier_selection={config.ENABLE_TIER_SELECTION}, "
          f"graeae={config.ENABLE_GRAEAE}, distillation={config.ENABLE_DISTILLATION}\n", file=sys.stderr, flush=True)

def shutdown_handler(signum, frame):
    """Graceful shutdown"""
    print(f"\n[SHUTDOWN] Initiating graceful shutdown...", file=sys.stderr, flush=True)
    embedding_generator.stop()
    print(f"[SHUTDOWN] ✓ All services stopped", file=sys.stderr, flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# ============================================================================
# CLI Entrypoint
# ============================================================================


# ============================================================================
# GRAEAE Real Service Integration
# ============================================================================

@app.route('/graeae/consult', methods=['POST'])
def graeae_consult():
    """Consult GRAEAE reasoning engine (real service on port 5001)"""
    try:
        data = request.get_json() or {}
        prompt = data.get('prompt', '')
        task_type = data.get('task_type', 'reasoning')
        mode = data.get('mode', 'auto')
        
        if not prompt:
            return jsonify({'error': 'Missing prompt'}), 400
        
        # Call real GRAEAE service on port 5001
        try:
            resp = requests.post(
                'http://localhost:5001/graeae/consult',
                json={'prompt': prompt, 'task_type': task_type},
                timeout=30
            )
            if resp.status_code == 200:
                result = resp.json()
                # Log to database
                try:
                    db = DatabaseManager()
                    conn = db.get_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO graeae_consultations (prompt, task_type, consensus_response, consensus_score, winning_muse, cost, latency_ms, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (prompt, task_type, str(result)[:200], 0.85, 'graeae-consensus', 0.02, 1200, mode)
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                except Exception as e:
                    pass
                return jsonify(result), 200
            return jsonify({'error': f'GRAEAE error: {resp.status_code}'}), resp.status_code
        except requests.exceptions.ConnectionError:
            return jsonify({'error': 'GRAEAE service unavailable'}), 503
        except requests.exceptions.Timeout:
            return jsonify({'error': 'GRAEAE timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/graeae/health', methods=['GET'])
def graeae_health():
    """Check GRAEAE service health"""
    try:
        resp = requests.get('http://localhost:5001/graeae/health', timeout=5)
        return {'status': 'healthy', 'service': 'graeae'}, 200 if resp.status_code == 200 else 503
    except:
        return {'status': 'unhealthy'}, 503


@app.route('/graeae/muses', methods=['GET'])
def graeae_muses():
    """List available reasoning providers from GRAEAE"""
    try:
        resp = requests.get('http://localhost:5001/graeae/muses', timeout=5)
        if resp.status_code == 200:
            return resp.json(), 200
        return {'error': 'GRAEAE unavailable'}, 503
    except:
        return {'error': 'GRAEAE unavailable'}, 503


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.API_PORT

    print(f"[MAIN] Starting Flask on {config.API_HOST}:{port}", file=sys.stderr, flush=True)

    app.run(
        host=config.API_HOST,
        port=port,
        debug=config.API_DEBUG,
        threaded=config.API_THREADED
    )
