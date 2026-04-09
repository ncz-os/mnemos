import json
from pathlib import Path

API_KEYS_FILE = Path.home() / '.api_keys_master.json'

# Aliases from graeae provider names to master JSON keys
_PROVIDER_ALIASES = {
    'claude-opus': 'anthropic',
    'claude': 'anthropic',
    'gemini': 'google_gemini',
}


def load_api_keys():
    if API_KEYS_FILE.exists():
        with open(API_KEYS_FILE) as f:
            return json.load(f)
    return {}


API_KEYS = load_api_keys()
_LLM_PROVIDERS = API_KEYS.get('llm_providers', {})


def get_key(provider: str) -> str:
    """Return api_key for a provider name (handles aliases)."""
    canonical = _PROVIDER_ALIASES.get(provider, provider)
    return _LLM_PROVIDERS.get(canonical, {}).get('api_key', '')
