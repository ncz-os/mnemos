"""Item 13 — OAuthRepository.gc_expired_sessions on SQLite (in-memory).

Bypasses :meth:`SqliteOAuthRepository.create_session` (whose INSERT
references columns the canonical SQLite ``oauth_sessions`` migration does
not carry — pre-existing divergence tracked separately) by inserting rows
directly with only the columns the table actually has: ``id``, ``user_id``,
``provider_id``, ``expires_at``, ``revoked``, ``created_at``. The GC
contract is what we care about here: rows past their grace windows must
be deleted, rows inside their grace windows must remain.

Same shape as the post-fix expectation for Postgres/MySQL/Oracle/Db2 —
``gc_expired_sessions`` is the new ABC entry point that ``lifecycle.py``
drives on every backend (item 13 / LUNA-class).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.persistence.sqlite import _execute, _fetch_all, _fetch_one, SqliteBackend


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=768))


async def _insert_session(
    conn,
    *,
    session_id: str,
    expires_at: datetime,
    revoked: bool = False,
) -> None:
    await _execute(
        conn,
        "INSERT INTO oauth_sessions (id, user_id, provider_id, expires_at, revoked, created_at) "
        "VALUES (?, ?, 'oauth', ?, ?, CURRENT_TIMESTAMP)",
        (
            session_id,
            "alice",
            expires_at.isoformat(),
            1 if revoked else 0,
        ),
    )


async def _session_count(conn) -> int:
    row = await _fetch_one(conn, "SELECT COUNT(*) AS n FROM oauth_sessions")
    assert row is not None
    return int(row["n"])


async def _remaining_ids(conn) -> list[str]:
    rows = await _fetch_all(conn, "SELECT id FROM oauth_sessions ORDER BY id")
    return [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_gc_expired_sessions_deletes_only_stale_rows(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "mnemos.db", _settings())
    await backend.open()
    now = datetime.now(timezone.utc)

    expired_old = now - timedelta(days=30)          # past 7-day expired grace
    expired_recent = now - timedelta(days=1)        # inside 7-day grace
    active_future = now + timedelta(days=1)         # still valid
    revoked_recent = now - timedelta(days=1)        # revoked, but expires_at not yet 30d stale
    revoked_stale = now - timedelta(days=31)        # revoked AND expires_at past 30d revoked_grace

    try:
        async with backend.transactional() as tx:
            conn = backend._oauth._conn(tx)
            await _insert_session(conn, session_id="expired-old", expires_at=expired_old, revoked=False)
            await _insert_session(conn, session_id="expired-recent", expires_at=expired_recent, revoked=False)
            await _insert_session(conn, session_id="active-future", expires_at=active_future, revoked=False)
            await _insert_session(conn, session_id="revoked-recent", expires_at=revoked_recent, revoked=True)
            await _insert_session(conn, session_id="revoked-stale", expires_at=revoked_stale, revoked=True)
            assert await _session_count(conn) == 5

            deleted = await backend.oauth.gc_expired_sessions(
                tx,
                now=now,
                expired_grace=timedelta(days=7),
                revoked_grace=timedelta(days=30),
            )
            assert deleted == 2
            # expired-old is past 7d expired_grace -> gone. revoked-stale
            # has no revoked_at on the canonical SQLite schema, so it
            # mirrors Postgres's NULL-revoked_at fallback: only deleted
            # once expires_at is stale by revoked_grace (30d) -> gone.
            # revoked-recent is revoked but its expires_at is only 1 day
            # stale (< 30d revoked_grace) -> must be RETAINED, not
            # immediately purged just because revoked=1.
            assert set(await _remaining_ids(conn)) == {
                "expired-recent",
                "active-future",
                "revoked-recent",
            }
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_gc_expired_sessions_zero_when_table_empty(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "mnemos.db", _settings())
    await backend.open()
    now = datetime.now(timezone.utc)
    try:
        async with backend.transactional() as tx:
            deleted = await backend.oauth.gc_expired_sessions(
                tx,
                now=now,
                expired_grace=timedelta(days=7),
                revoked_grace=timedelta(days=30),
            )
        assert deleted == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_gc_expired_sessions_zero_when_nothing_stale(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "mnemos.db", _settings())
    await backend.open()
    now = datetime.now(timezone.utc)
    try:
        async with backend.transactional() as tx:
            conn = backend._oauth._conn(tx)
            await _insert_session(conn, session_id="young-1", expires_at=now + timedelta(days=1))
            await _insert_session(conn, session_id="young-2", expires_at=now + timedelta(days=30))

            deleted = await backend.oauth.gc_expired_sessions(
                tx,
                now=now,
                expired_grace=timedelta(days=7),
                revoked_grace=timedelta(days=30),
            )
        assert deleted == 0
        async with backend.transactional() as tx:
            conn = backend._oauth._conn(tx)
            assert await _session_count(conn) == 2
    finally:
        await backend.close()
