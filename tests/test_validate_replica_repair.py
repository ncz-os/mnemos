from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mnemos.domain import anti_entropy as ae
from mnemos.domain.replication_digest import build_namespace_digest, diff_digests


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["absent", "unavailable", "incomplete"])
async def test_validate_replica_only_membership(monkeypatch, result):
    source = build_namespace_digest([])
    replica = build_namespace_digest([{"id": "remote", "content_hash": "hash"}])
    if result == "incomplete":
        source.pop("row_count")
    diff = diff_digests(source, replica)
    repo = SimpleNamespace(
        get_peer=AsyncMock(return_value={"name": "peer", "base_url": "https://example.test", "auth_token": "token"})
    )

    @asynccontextmanager
    async def transactional():
        yield object()

    backend = SimpleNamespace(federation=repo, transactional=transactional)
    fetch = AsyncMock(return_value=[], side_effect=RuntimeError("unavailable") if result == "unavailable" else None)
    monkeypatch.setattr(ae, "pull_memory_by_id", fetch)
    withdrawal = AsyncMock(return_value=1)
    monkeypatch.setattr("mnemos.domain.federation._apply_withdrawal", withdrawal)
    report = await ae.repair_namespace(backend, "peer-id", diff)
    if result == "absent":
        withdrawal.assert_awaited_once()
        assert withdrawal.await_args.args[2] == "peer"
        assert withdrawal.await_args.args[3]["id"] == "remote"
        assert report["withdrawn"] == 1
    else:
        withdrawal.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdrawal_retains_other_row_scopes(monkeypatch):
    import sqlite3
    from mnemos.persistence import sqlite as sq

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sq._dict_factory
    conn.executescript("""CREATE TABLE memories (id TEXT PRIMARY KEY, federation_source TEXT);
      INSERT INTO memories VALUES ('fed:peer:remote', 'peer'), ('fed:other:remote', 'other'),
      ('remote', NULL), ('fed:peer:related', 'other');""")
    repo = object.__new__(sq.SqliteFederationRepository)
    repo.get_peer = AsyncMock(return_value={"name": "peer", "base_url": "https://example.test", "auth_token": "token"})

    @asynccontextmanager
    async def transactional():
        yield sq.SqliteTransaction(conn)
        conn.commit()

    backend = SimpleNamespace(federation=repo, transactional=transactional)
    monkeypatch.setattr(ae, "pull_memory_by_id", AsyncMock(return_value=[]))
    monkeypatch.setattr("mnemos.workers.audit_sealer.audit_chain_enabled", lambda: False)
    audit = AsyncMock()
    monkeypatch.setattr("mnemos.audit.route_helper.write_transaction_audit", audit)
    diff = diff_digests(
        build_namespace_digest([]),
        build_namespace_digest([{"id": "remote", "content_hash": "hash"}, {"id": "related", "content_hash": "hash"}]),
    )
    try:
        report = await ae.repair_namespace(backend, "peer-id", diff)
        assert report["withdrawn"] == 1
        assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {
            "fed:other:remote",
            "remote",
            "fed:peer:related",
        }
        audit.assert_awaited_once()
    finally:
        conn.close()
