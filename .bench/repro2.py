import asyncio
import sys
sys.path.insert(0, "tests")
from unittest.mock import MagicMock
import mnemos.api.dependencies as auth_mod
import mnemos.core.lifecycle as lc
from tests.test_user_namespace import _FakeOAuthRepo, _backend

oauth = _FakeOAuthRepo(api_key_row={"id": "key-1","user_id": "bob","revoked": False,"role": "user","namespace": "bob-ns"}, group_ids=["g-2"])
auth_mod._auth_enabled = True
orig = lc._schedule_background
def _run_bg(coro):
    print("sched called")
    try:
        while True:
            coro.send(None)
    except StopIteration:
        pass
    finally:
        coro.close()
    return None
lc._schedule_background = _run_bg
req = MagicMock()
req.app.state.persistence_backend = _backend(oauth)
req.cookies = {}
creds = MagicMock()
creds.credentials = "test-key"
print("start")
print(asyncio.run(auth_mod.get_current_user(req, creds)))
print("done", oauth.touch_calls)
