from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock


class _Tx:
    pass


class _FakeOAuthRepo:
    def __init__(self, api_key_row=None):
        self._api_key_row = api_key_row
        self.lookup_calls = 0
        self.touch_calls: list[str] = []

    async def lookup_api_key(self, _tx, key_hash):
        self.lookup_calls += 1
        return self._api_key_row

    async def touch_api_key(self, _tx, key_id):
        self.touch_calls.append(str(key_id))

    async def resolve_active_session(self, _tx, _sid, *, now):
        return None


def _backend(api_key_row):
    @asynccontextmanager
    async def _tx_cm():
        yield _Tx()

    oauth = _FakeOAuthRepo(api_key_row=api_key_row)
    return SimpleNamespace(
        oauth=oauth,
        transactional=_tx_cm,
        _supports_oauth_persistence=True,
    ), oauth


def test_api_key_auth_fetches_user_and_groups_in_one_query(monkeypatch):
    """The API-key auth path goes through ``OAuthRepository.lookup_api_key``,
    which returns a denormalised row (api_keys + users + aggregated
    user_groups) in a single round-trip on every supported backend.
    """
    import mnemos.api.dependencies as auth_mod
    from mnemos.api.dependencies import get_current_user

    api_key_row = {
        "id": "key-1",
        "user_id": "alice",
        "revoked": False,
        "role": "user",
        "namespace": "alice-ns",
        "group_ids": ["ops", "dev"],
    }
    backend, oauth = _backend(api_key_row)

    monkeypatch.setattr(auth_mod, "_auth_enabled", True)

    import mnemos.core.lifecycle as lc

    def _run_bg(coro):
        try:
            while True:
                coro.send(None)
        except StopIteration:
            pass
        finally:
            coro.close()
        return None

    monkeypatch.setattr(lc, "_schedule_background", _run_bg)

    request = MagicMock()
    request.app.state.persistence_backend = backend
    request.cookies = {}

    creds = MagicMock()
    creds.credentials = "test-key"

    user = asyncio.run(get_current_user(request, creds))

    assert user.user_id == "alice"
    assert user.group_ids == ["ops", "dev"]
    assert oauth.lookup_calls == 1
    assert oauth.touch_calls == ["key-1"]
