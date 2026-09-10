"""Driver-free race/error regressions; these are not live MySQL verification."""

from unittest.mock import AsyncMock

import pytest

from mnemos.persistence.mysql import MysqlOAuthRepository


@pytest.mark.asyncio
async def test_verified_email_identity_race_adopts_committed_winner():
    repo = MysqlOAuthRepository()
    repo._mcp_fetch = AsyncMock(
        side_effect=[None, {"id": "existing-user"}, {"id": "winner-identity", "user_id": "existing-user"}]
    )
    repo._mcp_execute = AsyncMock(side_effect=[RuntimeError("1062 Duplicate entry"), 1])
    result = await repo.provision_or_link_user(
        object(),
        provider="example",
        external_id="subject",
        claims={"email": "user@example.com", "email_verified": True},
    )
    assert result == ("existing-user", "winner-identity")
    # A consistent (snapshot) read would not see the concurrent winner under
    # REPEATABLE READ. The winning identity must be read with a current lock.
    assert repo._mcp_fetch.call_args.args[1].endswith("FOR UPDATE")
    assert repo._mcp_execute.call_args.args[2][-1] == "winner-identity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,winner",
    [
        (RuntimeError("database connection lost"), None),
        (RuntimeError("1062 Duplicate entry"), None),
    ],
)
async def test_identity_insert_does_not_hide_failure_without_a_winner(error, winner):
    repo = MysqlOAuthRepository()
    repo._mcp_fetch = AsyncMock(side_effect=[None, {"id": "existing-user"}, winner])
    repo._mcp_execute = AsyncMock(side_effect=error)
    with pytest.raises(RuntimeError) as caught:
        await repo.provision_or_link_user(
            object(),
            provider="example",
            external_id="subject",
            claims={"email": "user@example.com", "email_verified": True},
        )
    assert caught.value is error
