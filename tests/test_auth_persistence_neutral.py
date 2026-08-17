"""Backend-neutral auth lookups via ``OAuthRepository``.

These tests pin the contract introduced to fix the persistence-coupled
auth path: ``lookup_api_key``, ``touch_api_key``, and
``resolve_active_session`` must work on every OAuth-capable backend
(Postgres, SQLite, Oracle, Db2) so authentication does not require a
raw asyncpg pool. The SQLite backend is exercised here because it is
the default for the published images; the Postgres path shares the
same call shape.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mnemos.persistence.sqlite import SqliteBackend


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="compat"))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _exec(backend, sql: str, params: tuple = ()) -> None:
    """Run a raw SQL statement against the backend's transactional connection.

    Backends differ in whether ``execute`` is sync or async, so branch on what
    the call *returns* rather than on the callable. Inspecting the callable is
    unreliable: the SQLite backend uses ``aiosqlite``, whose ``execute`` is not
    a coroutine function -- it returns an awaitable ``Result`` wrapper. That
    made ``asyncio.iscoroutinefunction`` report False, the statement took the
    sync branch, and the coroutine was dropped un-awaited. Every INSERT here
    silently did nothing and the lookups under test then correctly found no
    rows, which read as five product failures rather than one test bug.
    """
    async with backend.transactional() as tx:
        result = tx.conn.execute(sql, params)
        if inspect.isawaitable(result):
            await result


async def _fetchone(conn, sql: str, params: tuple = ()):
    """Fetch a single row, tolerating sync and async drivers alike.

    Same reasoning as ``_exec``: branch on what the call returns, never on
    whether the callable looks like a coroutine function.
    """
    if hasattr(conn, "fetchone"):
        result = conn.fetchone(sql, params)
        if inspect.isawaitable(result):
            return await result
        return result
    cursor = conn.execute(sql, params)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    row = cursor.fetchone()
    return await row if inspect.isawaitable(row) else row


@pytest.mark.asyncio
async def test_sqlite_lookup_api_key_returns_user_context(tmp_path):
    backend = SqliteBackend(tmp_path / "auth.sqlite3", _settings())
    await backend.open()
    key_hash = _hash("mnemos_test_key")
    user_id = "auth-user"
    try:
        await _exec(
            backend,
            "INSERT INTO users (id, username, role, namespace) VALUES (?, ?, 'user', 'auth-ns')",
            (user_id, f"u-{user_id}"),
        )
        await _exec(backend, "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (user_id, "g-1"))
        await _exec(backend, "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (user_id, "g-2"))
        await _exec(
            backend,
            "INSERT INTO api_keys (id, user_id, key_hash, label) VALUES (?, ?, ?, ?)",
            ("key-uuid-1", user_id, key_hash, "test"),
        )

        async with backend.transactional() as tx:
            row = await backend.oauth.lookup_api_key(tx, key_hash)

        assert row is not None
        assert row["user_id"] == user_id
        assert row["revoked"] in (0, False)
        assert row["role"] == "user"
        assert row["namespace"] == "auth-ns"
        assert sorted(row["group_ids"]) == ["g-1", "g-2"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_lookup_api_key_returns_none_for_unknown_hash(tmp_path):
    backend = SqliteBackend(tmp_path / "auth.sqlite3", _settings())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            row = await backend.oauth.lookup_api_key(tx, _hash("nope"))
        assert row is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_touch_api_key_records_last_used(tmp_path):
    backend = SqliteBackend(tmp_path / "auth.sqlite3", _settings())
    await backend.open()
    key_id = "touch-key-1"
    try:
        await _exec(
            backend,
            "INSERT INTO users (id, username, role, namespace) VALUES (?, ?, 'user', 'default')",
            ("u1", "u1"),
        )
        await _exec(
            backend,
            "INSERT INTO api_keys (id, user_id, key_hash) VALUES (?, ?, ?)",
            (key_id, "u1", _hash("k")),
        )

        async with backend.transactional() as tx:
            await backend.oauth.touch_api_key(tx, key_id)

        async with backend.transactional() as tx:
            row = await _fetchone(tx.conn, "SELECT last_used FROM api_keys WHERE id=?", (key_id,))
        assert row["last_used"] is not None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_resolve_active_session_returns_user_and_touches(tmp_path):
    backend = SqliteBackend(tmp_path / "auth.sqlite3", _settings())
    await backend.open()
    sid = "active-session-id"
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    try:
        await _exec(
            backend,
            "INSERT INTO users (id, username, role, namespace) VALUES (?, ?, 'user', 'default')",
            ("u1", "u1"),
        )
        await _exec(
            backend,
            "INSERT INTO oauth_sessions "
            "(id, session_id, user_id, provider_id, expires_at, revoked) "
            "VALUES (?, ?, ?, 'oauth', ?, 0)",
            (sid, sid, "u1", expires),
        )

        now = datetime.now(timezone.utc)
        async with backend.transactional() as tx:
            row = await backend.oauth.resolve_active_session(tx, sid, now=now)

        assert row is not None
        assert row["user_id"] == "u1"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_resolve_active_session_rejects_revoked_and_expired(tmp_path):
    backend = SqliteBackend(tmp_path / "auth.sqlite3", _settings())
    await backend.open()
    sid_active = "active"
    sid_revoked = "revoked"
    sid_expired = "expired"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    now = datetime.now(timezone.utc)
    try:
        await _exec(
            backend,
            "INSERT INTO users (id, username, role, namespace) VALUES (?, ?, 'user', 'default')",
            ("u1", "u1"),
        )
        for sid, exp, rev in [
            (sid_active, future, 0),
            (sid_revoked, future, 1),
            (sid_expired, past, 0),
        ]:
            await _exec(
                backend,
                "INSERT INTO oauth_sessions "
                "(id, session_id, user_id, provider_id, expires_at, revoked) "
                "VALUES (?, ?, ?, 'oauth', ?, ?)",
                (sid, sid, "u1", exp, rev),
            )

        async with backend.transactional() as tx:
            assert await backend.oauth.resolve_active_session(tx, sid_active, now=now) is not None
            assert await backend.oauth.resolve_active_session(tx, sid_revoked, now=now) is None
            assert await backend.oauth.resolve_active_session(tx, sid_expired, now=now) is None
            assert await backend.oauth.resolve_active_session(tx, "missing", now=now) is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_auth_dependency_returns_user_context_via_persistence_backend(monkeypatch):
    """End-to-end: with a SQLite persistence backend in place,
    ``get_current_user`` returns a populated ``UserContext`` instead of
    the previous 503 caused by ``app.state.pool`` being ``None`` on
    non-Postgres backends.
    """
    import mnemos.api.dependencies as auth_mod
    from mnemos.api.dependencies import get_current_user

    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(":memory:", _settings())
    await backend.open()

    raw_key = "live-test-key"
    key_hash = _hash(raw_key)
    try:
        await _exec(
            backend,
            "INSERT INTO users (id, username, role, namespace) VALUES (?, ?, 'user', 'live-ns')",
            ("u-live", "u-live"),
        )
        await _exec(backend, "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", ("u-live", "g-live"))
        await _exec(
            backend,
            "INSERT INTO api_keys (id, user_id, key_hash) VALUES (?, ?, ?)",
            ("key-live", "u-live", key_hash),
        )

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

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(persistence_backend=backend)),
            cookies={},
        )
        creds = SimpleNamespace(credentials=raw_key)

        user = await get_current_user(request, creds)
        assert user.user_id == "u-live"
        assert user.role == "user"
        assert user.namespace == "live-ns"
        assert user.group_ids == ["g-live"]
        assert user.authenticated is True
    finally:
        await backend.close()
