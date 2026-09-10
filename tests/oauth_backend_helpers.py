"""Real MCP OAuth backends; external DSNs MUST name disposable test databases.

SQLite runs by default. Set MNEMOS_TEST_OAUTH_{POSTGRES,ORACLE,MYSQL,MARIADB,DB2}_DSN
for additional engines. An explicitly configured engine failing to open is a test
failure, never a skip. PostgreSQL additionally isolates each case in a new schema.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

BACKENDS = ("sqlite", "postgres", "oracle", "mysql", "mariadb", "db2")


@dataclass
class OAuthDatabase:
    kind: str
    settings: Any
    dsn: str
    schema: str | None = None
    admin: Any = None
    opened: list[Any] = field(default_factory=list)

    async def open(self):
        if self.kind == "postgres":
            import asyncpg

            from mnemos.persistence.postgres import PostgresBackend

            pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=1,
                max_size=4,
                server_settings={"search_path": f"{self.schema},public"},
            )
            backend = PostgresBackend(pool, self.settings)
            try:
                await backend.open()
            except BaseException:
                await pool.close()
                raise
        else:
            from mnemos.core.lifecycle import build_configured_persistence_backend

            kind, backend = await build_configured_persistence_backend(self.settings)
            assert kind == self.kind
        self.opened.append(backend)
        return backend

    async def close(self, backend):
        await backend.close()
        self.opened.remove(backend)


@pytest_asyncio.fixture(params=BACKENDS)
async def oauth_database(request, tmp_path):
    from mnemos.core.config import _DatabaseSettings, get_settings

    kind = request.param
    env_name = f"MNEMOS_TEST_OAUTH_{kind.upper()}_DSN"
    dsn = f"sqlite:///{tmp_path / 'oauth.db'}" if kind == "sqlite" else os.environ.get(env_name, "").strip()
    if not dsn:
        pytest.skip(f"{env_name} not set; {kind} live persistence unverified")
    settings = get_settings().model_copy(
        update={"database": _DatabaseSettings(backend=kind, dsn=dsn, embedding_dim=768)}
    )
    database = OAuthDatabase(kind=kind, settings=settings, dsn=dsn)
    try:
        if kind == "postgres":
            import asyncpg

            database.admin = await asyncpg.connect(dsn=dsn)
            database.schema = f"oauth_test_{uuid.uuid4().hex[:12]}"
            await database.admin.execute(f'CREATE SCHEMA "{database.schema}"')
        yield database
    finally:
        for backend in list(database.opened):
            await database.close(backend)
        if database.admin is not None:
            try:
                if database.schema:
                    await database.admin.execute(f'DROP SCHEMA IF EXISTS "{database.schema}" CASCADE')
            finally:
                await database.admin.close()
