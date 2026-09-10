"""Rejected credential aliases must never reach collation-dependent SQL."""

from unittest.mock import AsyncMock

import pytest

from mnemos.persistence.db2 import Db2OAuthRepository
from mnemos.persistence.mysql import MysqlOAuthRepository
from mnemos.persistence.oracle import OracleOAuthRepository
from mnemos.persistence.postgres import PostgresOAuthRepository
from mnemos.persistence.sqlite import SqliteOAuthRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository",
    [
        SqliteOAuthRepository,
        PostgresOAuthRepository,
        OracleOAuthRepository,
        MysqlOAuthRepository,
        Db2OAuthRepository,
    ],
)
@pytest.mark.parametrize("invalid", ["canonical ", "canonical\n", "cánonical", ""])
async def test_credential_aliases_rejected_before_any_database_operation(repository, invalid):
    repo = repository()
    repo._mcp_fetch = AsyncMock()
    repo._mcp_execute = AsyncMock()
    tx = object()
    assert await repo.mcp_get_client(tx, invalid) is None
    assert await repo.mcp_consume_code(tx, invalid) is None
    assert await repo.mcp_rotate_refresh(tx, invalid, "canonical", {}) == "invalid"
    assert await repo.mcp_rotate_refresh(tx, "canonical", invalid, {}) == "invalid"
    repo._mcp_fetch.assert_not_awaited()
    repo._mcp_execute.assert_not_awaited()
