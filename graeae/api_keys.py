"""API key loader for GRAEAE providers.

Key file resolution order (first found wins):
  1. $MNEMOS_KEYS_PATH          — explicit override
  2. ~/.api_keys_master.json    — standard location (shared across machines)
  3. ~/.config/mnemos/api_keys.json — legacy fallback

Three file shapes are accepted (first match wins):

  1. Canonical (MNEMOS-native, matches OpenClaw):
     {
       "llm_providers": {
         "perplexity":     { "api_key": "pplx-..." },
         "anthropic":      { "api_key": "sk-ant-..." },
         ...
       }
     }

  2. Flat with api_key wrapper:
     {
       "perplexity":    { "api_key": "..." },
       "anthropic":     { "api_key": "..." },
       ...
     }

  3. Flat with raw string values (Triton-native):
     {
       "perplexity":    "pplx-...",
       "anthropic":     "sk-ant-...",
       ...
     }

The canonical shape is preferred. The flat shapes are accepted so
operators whose fleet already has Triton-style key files don't have
to maintain a second copy just for MNEMOS.
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
    """Load the key file. Returns the raw JSON dict or {} if no file
    is found / file is unparseable. Callers use `_extract_providers`
    to normalize across on-disk shapes."""
    key_file = _find_key_file()
    if key_file is None:
        logger.warning(
            "[GRAEAE] no API key file found in search paths (%s) — providers "
            "will have empty keys. Set MNEMOS_KEYS_PATH or create "
            "~/.api_keys_master.json.",
            [p for p in _SEARCH_PATHS if p],
        )
        return {}
    try:
        with open(key_file) as f:
            data = json.load(f)
        logger.debug(f"[GRAEAE] loaded API keys from {key_file}")
        return data
    except Exception as e:
        logger.error(f"[GRAEAE] failed to load API keys from {key_file}: {e}")
        return {}


def _extract_providers(data: dict) -> dict:
    """Normalize the on-disk shape to {provider_name: {api_key: ...}}.

    Accepts three shapes (see module docstring). Flat detection walks
    the top-level keys once and infers the shape from the first
    entry: dict-valued with `api_key` → shape 2; non-empty string
    value → shape 3 (wrapped into {"api_key": "..."}); anything else
    ignored. Avoids misreading a genuinely-empty / wrong-schema file
    as a flat provider map.
    """
    if not isinstance(data, dict):
        return {}

    if "llm_providers" in data:
        nested = data.get("llm_providers")
        return nested if isinstance(nested, dict) else {}

    flat: dict = {}
    for name, entry in data.items():
        if isinstance(entry, dict) and "api_key" in entry:
            # Shape 2: already wrapped.
            flat[name] = entry
        elif isinstance(entry, str) and entry:
            # Shape 3: raw string — wrap it so downstream get_key()
            # can treat all shapes uniformly.
            flat[name] = {"api_key": entry}
    if flat:
        logger.info(
            "[GRAEAE] key file in flat shape (no 'llm_providers' wrapper); "
            "loaded %d providers: %s", len(flat), sorted(flat.keys()),
        )
    return flat


# Loaded once at module import; refresh by calling load_api_keys() again if needed.
_LLM_PROVIDERS: dict = _extract_providers(load_api_keys())


def get_key(provider: str) -> str:
    """Return the api_key for a provider name, resolving aliases."""
    canonical = _PROVIDER_ALIASES.get(provider, provider)
    return _LLM_PROVIDERS.get(canonical, {}).get("api_key", "")
