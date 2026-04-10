"""
MNEMOS Configuration Module
Centralized settings for PostgreSQL, Ollama, and system parameters
"""

import os
from datetime import timedelta

# ============================================================================
# PostgreSQL Configuration
# ============================================================================

PG_CONFIG = {
    'host': os.getenv('PG_HOST', 'localhost'),
    'port': int(os.getenv('PG_PORT', 5432)),
    'database': os.getenv('PG_DATABASE', 'mnemos'),
    'user': os.getenv('PG_USER', 'mnemos_user'),
    'password': os.getenv('PG_PASSWORD', 'changeme')
}

# ============================================================================
# Ollama Configuration (Embeddings)
# ============================================================================

OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'http://localhost:11434')
# OLLAMA_EMBED_HOST is separate: embeddings stay on inference-server (nomic-embed-text, 768-dim)
# even when OLLAMA_HOST points to the local Phi inference server.
OLLAMA_EMBED_HOST = os.getenv('OLLAMA_EMBED_HOST', 'http://localhost:11434')
OLLAMA_EMBED_URL = f'{OLLAMA_EMBED_HOST}/api/embeddings'
OLLAMA_EMBED_MODEL = os.getenv('OLLAMA_EMBED_MODEL', 'nomic-embed-text')
OLLAMA_EMBED_TIMEOUT = 10  # seconds

# ============================================================================
# MNEMOS System Configuration
# ============================================================================

# Memory management
MAX_MEMORIES = int(os.getenv('MAX_MEMORIES', 10000))
MEMORY_TTL = timedelta(days=int(os.getenv('MEMORY_TTL_DAYS', 7)))

# JSON Shard storage (legacy persistence)
SHARD_DIR = os.getenv('MNEMOS_DATA_DIR', '/data/mnemos') + '/memories'
MAX_SHARD_SIZE = 10 * 1024 * 1024  # 10MB per shard file

# Background jobs
CLEANUP_INTERVAL = 3600  # Run cleanup every hour
EMBEDDING_CHECK_INTERVAL = 30  # Check for missing embeddings every 30 seconds
EMBEDDING_BATCH_SIZE = 500  # Generate embeddings in parallel batches
MAX_EMBEDDING_WORKERS = 8  # Parallel workers for batch embedding

# Search configuration
DEFAULT_SEARCH_LIMIT = 10
MIN_SIMILARITY_THRESHOLD = 0.1
IVFFLAT_PROBES = 10  # IVFFlat index probes for better recall

# ============================================================================
# API Configuration
# ============================================================================

API_HOST = os.getenv('API_HOST', '0.0.0.0')
API_PORT = int(os.getenv('API_PORT', 5000))
API_DEBUG = os.getenv('API_DEBUG', 'False').lower() == 'true'
API_THREADED = True
API_VERSION = '2.0-merged-modular'

# ============================================================================
# Feature Flags
# ============================================================================

ENABLE_TIER_SELECTION = True  # Task-based tier selection in search
ENABLE_JSON_SHARD_SYNC = True  # Sync JSON shards on startup
ENABLE_BACKGROUND_EMBEDDING = True  # Generate embeddings in background
ENABLE_GRAEAE = True  # Multi-LLM reasoning endpoints
ENABLE_DISTILLATION = True  # Memory distillation endpoints
ENABLE_AUTONOMY = False  # Scheduler/reflection (future)

# ============================================================================
# Tier Detection Keywords
# ============================================================================

TASK_TYPE_KEYWORDS = {
    'infrastructure': ['infrastructure', 'deploy', 'network', 'server', 'system', 'docker', 'kubernetes', 'cloud'],
    'reasoning': ['reasoning', 'think', 'design', 'architecture', 'plan', 'strategy', 'decision'],
    'code': ['code', 'debug', 'error', 'bug', 'fix', 'implement', 'function', 'class'],
    'project': ['project', 'research', 'work', 'personal', 'archive'],
    'complex': []  # Fallback: queries with >20 words
}

# Category tiers for different task types
TIER_SELECTION = {
    'infrastructure': ['documentation', 'facts', 'reasoning_outcome', 'session_consolidation', 'infrastructure', 'compression_tier1', 'compression_tier3', 'compression_tier2'],
    'reasoning': ['documentation', 'facts', 'reasoning_outcome', 'consultation', 'compression_tier1', 'compression_tier2', 'compression_tier4'],
    'code': ['documentation', 'facts', 'code', 'project', 'compression_tier1', 'compression_tier2'],
    'project': ['documentation', 'facts', 'project', 'reasoning_outcome', 'compression_tier1', 'compression_tier2', 'compression_tier4'],
    'complex': ['documentation', 'facts', 'reasoning_outcome', 'session_consolidation', 'consultation', 'infrastructure', 'projects', 'compression_tier1', 'compression_tier2', 'compression_tier3', 'compression_tier4'],
    'general': ['documentation', 'facts', 'reasoning_outcome', 'session_consolidation', 'consultation', 'infrastructure', 'projects', 'compression_tier1', 'compression_tier2']
}

# ============================================================================
# Logging Configuration
# ============================================================================

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '[%(asctime)s] [%(levelname)s] %(message)s'
REQUEST_ID_LENGTH = 8  # Show first 8 chars of request ID in logs

# ============================================================================
# TOML Configuration (config.toml overrides env-var defaults where present)
# ============================================================================

import tomllib as _tomllib
from pathlib import Path as _Path

def _load_toml() -> dict:
    """Load config.toml if present, return empty dict otherwise."""
    toml_path = _Path(__file__).parent / 'config.toml'
    if toml_path.exists():
        with open(toml_path, 'rb') as _f:
            return _tomllib.load(_f)
    return {}

_TOML = _load_toml()

# Compression configuration — sourced from config.toml, used by CompressionManager
COMPRESSION_CONFIG: dict = _TOML.get('compression', {})
