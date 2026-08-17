"""Per-user namespace loaded from the users table.

This test module verifies the v3.x+ backend-neutral auth path:
  * The API-key auth lookup (`OAuthRepository.lookup_api_key`) returns
    a denormalised row that joins ``users.namespace`` so the auth layer
    can populate ``UserContext.namespace`` without a second round-trip.
  * The config default (`_default_namespace`) is reserved for the
    auth-disabled singleton path, never authenticated DB users.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock



class _Tx:
    pass


class _FakeOAuthRepo:
    """Backend-neutral stub used to verify the api-key branch's contract."""

    def __init__(self, *, api_key_row=None, group_ids=()):
        self._api_key_row = api_key_row
        self._group_ids = list(group_ids)
        self.lookup_calls: list[str] = []
        self.touch_calls: list[str] = []

    async def lookup_api_key(self, _tx, key_hash: str):
        self.lookup_calls.append(key_hash)
        if self._api_key_row is None:
            return None
        row = dict(self._api_key_row)
        row.setdefault("group_ids", list(self._group_ids))
        return row

    async def touch_api_key(self, _tx, key_id) -> None:
        self.touch_calls.append(str(key_id))

    async def resolve_active_session(self, _tx, _session_id, *, now):
        return None


def _backend(oauth: _FakeOAuthRepo) -> SimpleNamespace:
    @asynccontextmanager
    async def _tx_cm():
        yield _Tx()

    return SimpleNamespace(
        oauth=oauth,
        transactional=_tx_cm,
        _supports_oauth_persistence=True,
    )


# ─── API-key auth path ──────────────────────────────────────────────────────


def test_api_key_path_loads_namespace_from_joined_users_row(monkeypatch):
    """The /get_current_user Bearer branch delegates to ``lookup_api_key``,
    which joins ``api_keys`` to ``users`` and returns the user's namespace
    so ``UserContext.namespace`` reflects the DB value, not the config default.
    """
    import mnemos.api.dependencies as auth_mod
    from mnemos.api.dependencies import get_current_user

    oauth = _FakeOAuthRepo(
        api_key_row={
            "id": "key-1",
            "user_id": "bob",
            "revoked": False,
            "role": "user",
            "namespace": "bob-ns",
        },
        group_ids=["g-2"],
    )

    monkeypatch.setattr(auth_mod, "_auth_enabled", True)

    # Stub out _schedule_background so it doesn't try to hit the real event loop;
    # drain the coroutine inline so touch_api_key lands.
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
    request.app.state.persistence_backend = _backend(oauth)
    request.cookies = {}

    creds = MagicMock()
    creds.credentials = "test-key"

    user = asyncio.run(get_current_user(request, creds))

    assert user.user_id == "bob"
    assert user.role == "user"
    assert user.namespace == "bob-ns"
    assert user.group_ids == ["g-2"]
    assert user.authenticated is True

    # Verify the hash really flowed through the new repository call.
    assert oauth.lookup_calls
    assert oauth.touch_calls == ["key-1"]
