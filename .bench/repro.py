import asyncio
import sys
sys.path.insert(0, "tests")
from unittest.mock import MagicMock
import mnemos.api.dependencies as auth_mod

class _Tx:
    pass
class Repo:
    async def lookup_api_key(self, _tx, h):
        return {"id": "key-1", "user_id": "bob", "revoked": False, "role": "user", "namespace": "ns", "group_ids": ["g"]}
    async def touch_api_key(self, _tx, key_id):
        print("touch", key_id)
from contextlib import asynccontextmanager
@asynccontextmanager
async def _tx_cm():
    yield _Tx()
from types import SimpleNamespace
backend = SimpleNamespace(oauth=Repo(), transactional=_tx_cm, _supports_oauth_persistence=True)
auth_mod._auth_enabled = True
req = MagicMock()
req.app.state.persistence_backend = backend
req.cookies = {}
creds = MagicMock()
creds.credentials = "test-key"
print(asyncio.run(auth_mod.get_current_user(req, creds)))
