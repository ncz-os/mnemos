"""API key loader for GRAEAE providers.

Key file resolution order (first found wins):
  1. $MNEMOS_KEYS_PATH          — explicit override
  2. ~/.api_keys_master.json    — standard location (shared across machines)
  3. ~/.config/mnemos/api_keys.json — legacy fallback

File format (same as OpenClaw):
  {
    "llm_providers": {
      "perplexity": { "api_key": "pplx-..." },
      "groq":       { "api_key": "gsk_..." },
      "anthropic":  { "api_key": "sk-ant-..." },
      "xai":        { "api_key": "xai-..." },
      "openai":     { "api_key": "sk-..." },
      "google_gemini": { "api_key": "AIza..." }
    }
  }
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_SEARCH_PATHS = [
    os.getenv("MNEMOS_KEYS_PATH", ""),
    os.path.expanduser("~/.api_keys_master.json"),
    os.path.expanduser("~/.config/mnemos/api_keys.json"),
]

# Canonical names in ~/.api_keys_master.json may differ from GRAEAE provider names.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude-opus": "anthropic",
    "claude":      "anthropic",
    "gemini":      "google_gemini",
}


def _find_key_file() -> Path | None:
    for p in _SEARCH_PATHS:
        if p and Path(p).exists():
            return Path(p)
    return None


def load_api_keys() -> dict:
    key_file = _find_key_file()
    if key_file is None:
        logger.warning("[GRAEAE] no API key file found — providers will have empty keys")
        return {}
    try:
        with open(key_file) as f:
            data = json.load(f)
        logger.debug(f"[GRAEAE] loaded API keys from {key_file}")
        return data
    except Exception as e:
        logger.error(f"[GRAEAE] failed to load API keys from {key_file}: {e}")
        return {}


# Loaded once at module import; refresh by calling load_api_keys() again if needed.
_LLM_PROVIDERS: dict = load_api_keys().get("llm_providers", {})


def get_key(provider: str) -> str:
    """Return the api_key for a provider name, resolving aliases."""
    canonical = _PROVIDER_ALIASES.get(provider, provider)
    return _LLM_PROVIDERS.get(canonical, {}).get("api_key", "")
