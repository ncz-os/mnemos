"""Cross-backend regression for OAuthRepository API-key creation.

Until v7.0.1 creating an API key was Postgres-only in two places while
*looking one up* was already backend-neutral:

  * ``mnemos/api/routes/admin.py``'s ``POST /admin/users/{user_id}/apikeys``
    called ``require_postgres_pool_or_503`` and then ran a raw asyncpg
    INSERT, so it 503'd on every non-Postgres backend; and
  * ``mnemos/installer/db.py``'s ``create_api_key`` tried asyncpg,
    psycopg, psycopg2 and the psql CLI — four Postgres-only drivers —
    so ``mnemos install`` on the SQLite edge profile finished with no
    usable credential.

Meanwhile ``lookup_api_key`` / ``touch_api_key`` had per-backend
implementations and worked fine. An edge-profile operator could
authenticate with a key they had no supported way to create.

These tests pin the write half of that contract to the same standard as
the read half: the same create → lookup → list → revoke cycle must hold
on every backend, using each backend's own ``api_keys`` physical schema
(they genuinely differ — SQLite and MySQL lacked ``key_prefix``
entirely, and Oracle/Db2 store ``owner_id`` / ``name`` / ``revoked_at``
where Postgres stores ``user_id`` / ``label`` / ``revoked``).

Live arms: SQLite always; Postgres via ``MNEMOS_TEST_DB``; MySQL and
MariaDB via ``MNEMOS_TEST_MYSQL_DSN`` / ``MNEMOS_TEST_MARIADB_DSN``.
Oracle and Db2 are covered by the static ABC-completeness test at the
bottom of this file only — there is no Oracle or Db2 instance available
to this suite, so their SQL is reviewed, not executed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio


# ── Fixture: one live backend per arm ────────────────────────────────────────


@pytest_asyncio.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def backend(request, tmp_path, monkeypatch):
    """Yield a live backend with a provisioned schema.

    Mirrors the fixture in tests/test_federation_journal.py, which is the
    established way this suite reaches real MySQL/MariaDB/Postgres
    instances: a throwaway database per test, dropped in the finally.
    """
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))

    if request.param == "sqlite":
        from mnemos.persistence.sqlite import SqliteBackend

        instance = SqliteBackend(tmp_path / "apikeys.db", settings)
        await instance.open()
        try:
            yield request.param, instance
        finally:
            await instance.close()

    elif request.param in {"mysql", "mariadb"}:
        dsn = os.getenv("MNEMOS_TEST_" + request.param.upper() + "_DSN")
        if not dsn:
            pytest.skip("set MNEMOS_TEST_" + request.param.upper() + "_DSN for live api-key tests")
        import aiomysql

        from mnemos.persistence.mariadb import MariadbBackend, create_mariadb_pool
        from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

        parsed = urlsplit(dsn)
        name = "mnemos_apikeys_" + uuid.uuid4().hex[:16]
        admin = await aiomysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password or "",
            autocommit=True,
        )
        instance = None
        try:
            async with admin.cursor() as cursor:
                await cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            factory, cls = (
                (create_mysql_pool, MysqlBackend) if request.param == "mysql" else (create_mariadb_pool, MariadbBackend)
            )
            pool = await factory(
                urlunsplit(parsed._replace(path="/" + name)), min_size=1, max_size=4, settings=settings
            )
            instance = cls(pool, settings)
            await instance.open()
            yield request.param, instance
        finally:
            if instance is not None:
                await instance.close()
            async with admin.cursor() as cursor:
                await cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            admin.close()

    else:
        dsn = os.getenv("MNEMOS_TEST_DB")
        if not dsn:
            pytest.skip("set MNEMOS_TEST_DB for live PostgreSQL api-key tests")
        import asyncpg

        from mnemos.persistence.postgres import PostgresBackend

        name = "mnemos_apikeys_" + uuid.uuid4().hex[:16]
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            target = urlunsplit(urlsplit(dsn)._replace(path="/" + name))
            pool = await asyncpg.create_pool(target, min_size=1, max_size=4)
            instance = PostgresBackend(pool, settings)
            await instance.open()
            yield request.param, instance
        finally:
            if pool:
                await pool.close()
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_user(arm: str, instance, user_id: str) -> None:
    """Insert a users row using that backend's own users columns.

    The six ``users`` tables are not column-compatible (SQLite requires a
    NOT NULL UNIQUE ``username`` and has no ``display_name``; MySQL has
    no ``created_at``), which is exactly why ``user_exists`` is a narrow
    id probe rather than a full user read.
    """
    async with instance.transactional() as tx:
        if arm == "sqlite":
            from mnemos.persistence.sqlite import _execute_count

            await _execute_count(
                instance.oauth._conn(tx),
                "INSERT OR IGNORE INTO users (id, username, role, namespace) VALUES (?, ?, 'root', 'default')",
                (user_id, user_id),
            )
        elif arm == "postgres":
            await tx.conn.execute(
                "INSERT INTO users (id, display_name, role, namespace) "
                "VALUES ($1, $1, 'root', 'default') ON CONFLICT (id) DO NOTHING",
                user_id,
            )
        else:
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT IGNORE INTO users (id, display_name, role, namespace) "
                    "VALUES (%s, %s, 'root', 'default')",
                    (user_id, user_id),
                )


def _mint() -> tuple[str, str, str]:
    raw = secrets.token_hex(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:8]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_created_key_authenticates(backend):
    """The core regression: a key created through the ABC must resolve
    through ``lookup_api_key`` — the same call ``get_current_user`` makes.

    Creating and looking up used to live on opposite sides of a
    Postgres-only boundary; this asserts they meet.
    """
    arm, instance = backend
    user_id = "apikey_user_" + uuid.uuid4().hex[:8]
    await _seed_user(arm, instance, user_id)
    raw, key_hash, key_prefix = _mint()

    async with instance.transactional() as tx:
        created = await instance.oauth.create_api_key(
            tx, user_id=user_id, key_hash=key_hash, key_prefix=key_prefix, label="ci-key"
        )

    assert created["user_id"] == user_id, f"[{arm}] created row must echo the user"
    assert created["key_prefix"] == key_prefix, f"[{arm}] key_prefix must round-trip"
    assert created["label"] == "ci-key", f"[{arm}] label must round-trip"
    assert created["revoked"] is False, f"[{arm}] a fresh key is not revoked"
    assert created["last_used"] is None, f"[{arm}] a fresh key has never been used"
    assert isinstance(created["created_at"], str) and created["created_at"], (
        f"[{arm}] created_at must be an ISO-8601 string, got {created['created_at']!r}"
    )
    assert isinstance(created["id"], str) and created["id"], f"[{arm}] id must be a non-empty string"

    async with instance.transactional() as tx:
        resolved = await instance.oauth.lookup_api_key(tx, hashlib.sha256(raw.encode()).hexdigest())

    assert resolved is not None, f"[{arm}] the key just created must be resolvable by hash"
    assert resolved["user_id"] == user_id, f"[{arm}] lookup resolved the wrong user"
    assert not resolved["revoked"], f"[{arm}] freshly created key must not read as revoked"


@pytest.mark.asyncio
async def test_touch_after_create_sets_last_used(backend):
    """``touch_api_key`` must accept the id ``create_api_key`` returned.

    Guards the id-type seam: Postgres returns a UUID rendered to text,
    SQLite/MySQL a generated hex string, Oracle/Db2 a VARCHAR.
    """
    arm, instance = backend
    user_id = "apikey_touch_" + uuid.uuid4().hex[:8]
    await _seed_user(arm, instance, user_id)
    _raw, key_hash, key_prefix = _mint()

    async with instance.transactional() as tx:
        created = await instance.oauth.create_api_key(
            tx, user_id=user_id, key_hash=key_hash, key_prefix=key_prefix, label=None
        )
    async with instance.transactional() as tx:
        await instance.oauth.touch_api_key(tx, created["id"])
    async with instance.transactional() as tx:
        rows = await instance.oauth.list_api_keys(tx, user_id)

    assert len(rows) == 1, f"[{arm}] expected exactly one key for the user"
    assert rows[0]["last_used"] is not None, f"[{arm}] touch_api_key did not record last_used"


@pytest.mark.asyncio
async def test_count_list_and_revoke(backend):
    """count → list → revoke, the three helpers the admin routes drive."""
    arm, instance = backend
    user_id = "apikey_cycle_" + uuid.uuid4().hex[:8]
    await _seed_user(arm, instance, user_id)

    async with instance.transactional() as tx:
        assert await instance.oauth.user_exists(tx, user_id) is True, f"[{arm}] seeded user must exist"
        assert await instance.oauth.user_exists(tx, "no-such-user") is False, f"[{arm}] unknown user must not exist"
        assert await instance.oauth.count_active_api_keys(tx, user_id) == 0, f"[{arm}] no keys yet"

    ids = []
    for _ in range(3):
        _raw, key_hash, key_prefix = _mint()
        async with instance.transactional() as tx:
            row = await instance.oauth.create_api_key(
                tx, user_id=user_id, key_hash=key_hash, key_prefix=key_prefix, label="k"
            )
        ids.append(row["id"])

    async with instance.transactional() as tx:
        assert await instance.oauth.count_active_api_keys(tx, user_id) == 3, f"[{arm}] three active keys expected"
        listed = await instance.oauth.list_api_keys(tx, user_id)
    assert {r["id"] for r in listed} == set(ids), f"[{arm}] list_api_keys must return every created key"

    async with instance.transactional() as tx:
        assert await instance.oauth.revoke_api_key(tx, ids[0]) is True, f"[{arm}] first revoke must report a change"
    async with instance.transactional() as tx:
        assert await instance.oauth.revoke_api_key(tx, ids[0]) is False, (
            f"[{arm}] re-revoking an already-revoked key must report no change so the route can 404"
        )
        assert await instance.oauth.revoke_api_key(tx, str(uuid.uuid4())) is False, (
            f"[{arm}] revoking an unknown id must report no change, not raise"
        )
        assert await instance.oauth.count_active_api_keys(tx, user_id) == 2, f"[{arm}] revoked key must leave the count"

    async with instance.transactional() as tx:
        listed = await instance.oauth.list_api_keys(tx, user_id)
    revoked_flags = {r["id"]: r["revoked"] for r in listed}
    assert revoked_flags[ids[0]] is True, f"[{arm}] revoked key must be listed and flagged, not hidden"
    assert revoked_flags[ids[1]] is False, f"[{arm}] untouched keys must stay active"


@pytest.mark.asyncio
async def test_revoked_key_no_longer_authenticates(backend):
    """A revoked key must fail the auth predicate the dependency applies.

    ``get_current_user`` rejects on ``row is None or row["revoked"]``, so
    the contract the repository owes is that ``lookup_api_key`` reports
    the revocation — not that it hides the row.
    """
    arm, instance = backend
    user_id = "apikey_revoked_" + uuid.uuid4().hex[:8]
    await _seed_user(arm, instance, user_id)
    raw, key_hash, key_prefix = _mint()

    async with instance.transactional() as tx:
        created = await instance.oauth.create_api_key(
            tx, user_id=user_id, key_hash=key_hash, key_prefix=key_prefix, label=None
        )
    async with instance.transactional() as tx:
        await instance.oauth.revoke_api_key(tx, created["id"])
    async with instance.transactional() as tx:
        resolved = await instance.oauth.lookup_api_key(tx, hashlib.sha256(raw.encode()).hexdigest())

    assert resolved is None or resolved["revoked"], (
        f"[{arm}] a revoked key must not pass the auth predicate"
    )


# ── Static parity gate (covers Oracle and Db2, which cannot run here) ────────


def test_every_oauth_repository_implements_the_api_key_contract():
    """Every concrete OAuthRepository must implement the new ABC methods.

    Oracle and Db2 have no instance available to this suite, so this is
    the gate that keeps them honest: an inherited ``@abstractmethod``
    would make the class un-instantiable, and a missing override would
    surface here rather than at an operator's first key creation.

    MariaDB is intentionally absent from the class list — ``MariadbBackend``
    reuses ``MysqlOAuthRepository`` verbatim (see mariadb.py), so it is
    covered by the MySQL entry and by the live ``mariadb`` fixture arm.
    """
    from mnemos.persistence.base import OAuthRepository
    from mnemos.persistence.db2 import Db2OAuthRepository
    from mnemos.persistence.mysql import MysqlOAuthRepository
    from mnemos.persistence.oracle import OracleOAuthRepository
    from mnemos.persistence.postgres import PostgresOAuthRepository
    from mnemos.persistence.sqlite import SqliteOAuthRepository

    contract = (
        "user_exists",
        "count_active_api_keys",
        "create_api_key",
        "list_api_keys",
        "revoke_api_key",
    )
    for name in contract:
        assert getattr(OAuthRepository, name, None) is not None, f"ABC is missing {name}"

    for cls in (
        PostgresOAuthRepository,
        SqliteOAuthRepository,
        MysqlOAuthRepository,
        OracleOAuthRepository,
        Db2OAuthRepository,
    ):
        assert not getattr(cls, "__abstractmethods__", frozenset()), (
            f"{cls.__name__} still has unimplemented abstract methods: "
            f"{sorted(getattr(cls, '__abstractmethods__', ()))}"
        )
        for name in contract:
            impl = getattr(cls, name, None)
            assert impl is not None, f"{cls.__name__} does not implement {name}"
            assert getattr(impl, "__isabstractmethod__", False) is False, (
                f"{cls.__name__}.{name} is still the abstract stub — creating an API key "
                f"on that backend would raise instead of writing a row"
            )


def test_build_api_key_row_normalises_timestamps():
    """The neutral Row must hand callers ISO strings, never datetimes.

    ``ApiKeyResponse.created_at`` is typed ``str``; Postgres and MySQL
    return ``datetime`` while SQLite returns TEXT. If the normaliser
    regressed, Postgres would 500 on response validation.
    """
    from datetime import datetime, timezone

    from mnemos.persistence.base import build_api_key_row

    row = build_api_key_row(
        key_id=uuid.uuid4(),
        user_id="alice",
        key_prefix="deadbeef",
        label=None,
        created_at=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
        last_used=None,
        revoked=0,
    )
    assert row["created_at"] == "2026-09-16T12:00:00+00:00"
    assert row["last_used"] is None
    assert row["revoked"] is False, "an integer 0 from SQLite must normalise to a bool"
    assert isinstance(row["id"], str), "a UUID id must be rendered to text for ApiKeyResponse.id"

    passthrough = build_api_key_row(
        key_id="k1",
        user_id="alice",
        key_prefix="deadbeef",
        label="x",
        created_at="2026-09-16 12:00:00",
        last_used="2026-09-16 13:00:00",
        revoked=1,
    )
    assert passthrough["created_at"] == "2026-09-16 12:00:00", "SQLite TEXT timestamps pass through unchanged"
    assert passthrough["revoked"] is True


# ── Installer: the SQLite/edge profile path ──────────────────────────────────


def test_installer_creates_a_working_key_on_the_sqlite_profile(tmp_path, monkeypatch):
    """mnemos install on an edge/dev profile must end with a usable key.

    mnemos/installer/db.py::create_api_key had four driver paths —
    asyncpg, psycopg, psycopg2, psql — and every one of them was
    Postgres-only. On a SQLite profile all four failed in turn, the
    function returned None, and the installer printed no credential. It
    now routes the SQLite profile through the same
    OAuthRepository.create_api_key the admin route uses.
    """
    import asyncio
    from types import SimpleNamespace

    from mnemos.installer.db import create_api_key as installer_create_api_key
    from mnemos.persistence.sqlite import SqliteBackend

    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    db_path = tmp_path / "edge.db"
    config = SimpleNamespace(profile="edge", sqlite_path=str(db_path), embedding_dim=3)

    raw_key = installer_create_api_key(config)

    assert raw_key is not None, "installer must mint a key on the edge profile, not return None"
    assert raw_key.startswith("mnemos_"), "installer keys keep their mnemos_ prefix"

    async def _verify() -> dict | None:
        backend = SqliteBackend(db_path, SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
        await backend.open()
        try:
            async with backend.transactional() as tx:
                return await backend.oauth.lookup_api_key(tx, hashlib.sha256(raw_key.encode()).hexdigest())
        finally:
            await backend.close()

    resolved = asyncio.run(_verify())
    assert resolved is not None, "the installer-minted key must authenticate against the same database"
    assert resolved["user_id"] == "default", "installer keys belong to the seeded default root user"
    assert not resolved["revoked"]
