"""
MNEMOS Configuration Module
Centralized settings for PostgreSQL and system parameters.

Most constants here document available env-var overrides for deployers.
Only PG_CONFIG and COMPRESSION_CONFIG are imported by application code.
"""

import os

# ============================================================================
# PostgreSQL Configuration
# ============================================================================

# DB config: env vars take precedence, then config.toml [database], then defaults.
# _TOML is loaded below; we patch PG_CONFIG after loading it.
PG_CONFIG: dict = {}  # populated after _TOML is loaded (see bottom of file)

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
MEMORY_TTL_DAYS = int(os.getenv('MEMORY_TTL_DAYS', 7))

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
# Logging Configuration
# ============================================================================

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '[%(asctime)s] [%(levelname)s] %(message)s'
REQUEST_ID_LENGTH = 8  # Show first 8 chars of request ID in logs

# ============================================================================
# TOML Configuration (config.toml overrides env-var defaults where present)
# ============================================================================

import tomllib as _tomllib  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

def _load_toml() -> dict:
    """Load config.toml if present, return empty dict otherwise."""
    toml_path = _Path(__file__).parent / 'config.toml'
    if toml_path.exists():
        with open(toml_path, 'rb') as _f:
            return _tomllib.load(_f)
    return {}

_TOML = _load_toml()

# Populate PG_CONFIG: env > config.toml [database] > hardcoded defaults
_db_toml = _TOML.get('database', {})
PG_CONFIG = {
    'host':     os.getenv('PG_HOST',     str(_db_toml.get('host',     'localhost'))),
    'port':     int(os.getenv('PG_PORT', str(_db_toml.get('port',     5432)))),
    'database': os.getenv('PG_DATABASE', str(_db_toml.get('database', 'mnemos'))),
    'user':     os.getenv('PG_USER',     str(_db_toml.get('user',     'mnemos_user'))),
    'password': os.getenv('PG_PASSWORD', str(_db_toml.get('password', ''))),  # No default — service will fail loudly if PG_PASSWORD is not set
    'pool_min_size': int(os.getenv('PG_POOL_MIN', str(_db_toml.get('pool_min_size', 5)))),
    'pool_max_size': int(os.getenv('PG_POOL_MAX', str(_db_toml.get('pool_max_size', 20)))),
}

# Compression configuration — sourced from config.toml, used by CompressionManager
COMPRESSION_CONFIG: dict = _TOML.get('compression', {})

# GRAEAE configuration — provider registry and engine settings
GRAEAE_CONFIG: dict = _TOML.get('graeae', {})
