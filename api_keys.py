import json
from pathlib import Path

API_KEYS_FILE = Path.home() / '.api_keys_master.json'

def load_api_keys():
    if API_KEYS_FILE.exists():
        with open(API_KEYS_FILE) as f:
            return json.load(f)
    return {}

API_KEYS = load_api_keys()

# Export for modules
def get_key(provider: str) -> str:
    return API_KEYS.get(provider, {}).get('api_key', '')
