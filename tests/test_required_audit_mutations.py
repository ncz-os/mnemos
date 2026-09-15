"""Audit-required rollback at actual route and worker transaction boundaries."""

from types import SimpleNamespace
import struct

import pytest

from tests.test_federation_journal import backend as _backend_fixture, insert, mutate

backend = _backend_fixture
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect, sweep_for_archival
from mnemos.audit.route_helper import AuditChainContinuityError, memory_id_to_audit_bytes


@pytest.fixture
def audit_policy(monkeypatch):
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    monkeypatch.setattr(
        config, "get_settings", lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32))
    )


async def state(backend):
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        row = await ops.fetchone("SELECT content, archived_at FROM memories WHERE id = ?", "journal_one")
        archive_count = await ops.scalar("SELECT COUNT(*) FROM memory_archive")
        audit_count = await ops.scalar("SELECT COUNT(*) FROM memory_audit_chain")
        return row, archive_count, audit_count


def fail_append(backend, monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError("injected append outage")

    monkeypatch.setattr(type(backend.audit_chain), "insert_audit_entry", failed)


def install_route(backend, monkeypatch):
    from mnemos.api.routes import admin

    monkeypatch.setattr(admin, "backend_or_503", lambda: backend)
    monkeypatch.setattr(admin, "_require_persephone_enabled", lambda: None)

    async def invalidate():
        pass

    monkeypatch.setattr(admin, "_invalidate_memory_read_caches", invalidate)
    return admin


@pytest.mark.asyncio
async def test_required_archive_route_rolls_back_append_failure(backend, audit_policy, monkeypatch):
    from fastapi import HTTPException

    await insert(backend)
    fail_append(backend, monkeypatch)
    route = install_route(backend, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await route.persephone_archive_memory("journal_one", SimpleNamespace(user_id="root"))
    assert error.value.status_code == 503
    row, archive_count, audit_count = await state(backend)
    assert row["content"] == "ordinary facts" and row["archived_at"] is None
    assert archive_count == audit_count == 0


@pytest.mark.asyncio
async def test_best_effort_archive_commits_append_failure(backend, audit_policy, monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    await insert(backend)
    fail_append(backend, monkeypatch)
    route = install_route(backend, monkeypatch)
    result = await route.persephone_archive_memory("journal_one", SimpleNamespace(user_id="root"))
    row, archive_count, audit_count = await state(backend)
    assert result.archived and row["archived_at"] is not None
    assert archive_count == 1 and audit_count == 0


@pytest.mark.asyncio
async def test_actual_archive_hash_covers_driver_vector(backend, audit_policy, monkeypatch):
    from mnemos.audit import route_helper

    await insert(backend)
    await mutate(backend, "UPDATE memories SET embedding = ? WHERE id = ?", "[1,0,0]", "journal_one")
    seen = []
    original = route_helper.canonical_payload_hash

    def spy(**kwargs):
        seen.append(kwargs["embedding"])
        return original(**kwargs)

    monkeypatch.setattr(route_helper, "canonical_payload_hash", spy)
    route = install_route(backend, monkeypatch)
    await route.persephone_archive_memory("journal_one", SimpleNamespace(user_id="root"))
    assert seen == [struct.pack("<3f", 1, 0, 0)]
    async with backend.transactional() as tx:
        row = await backend.audit_chain.get_latest_audit_entry(tx, memory_id_to_audit_bytes("journal_one"))
        assert row["op"] == "archive"


@pytest.mark.asyncio
async def test_required_archival_worker_rolls_back(backend, audit_policy, monkeypatch):
    await insert(backend)
    fail_append(backend, monkeypatch)
    with pytest.raises(AuditChainContinuityError):
        await sweep_for_archival(backend, namespace="A", archive_after_days=1, batch_size=5)
    row, archive_count, audit_count = await state(backend)
    assert row["archived_at"] is None and archive_count == audit_count == 0


@pytest.mark.asyncio
async def test_required_missing_secret_does_not_commit_archive(backend, audit_policy, monkeypatch):
    from mnemos.core import config
    from fastapi import HTTPException

    await insert(backend)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(server=SimpleNamespace(session_secret="")))
    route = install_route(backend, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await route.persephone_archive_memory("journal_one", SimpleNamespace(user_id="root"))
    assert error.value.status_code == 503
    row, archive_count, audit_count = await state(backend)
    assert row["archived_at"] is None and archive_count == audit_count == 0


@pytest.mark.asyncio
async def test_best_effort_sql_error_uses_savepoint(backend, audit_policy, monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    await insert(backend)

    async def sql_failure(self, tx, **kwargs):
        await _Ops(tx, transaction_dialect(tx)).execute("INSERT INTO audit_missing_table(x) VALUES (1)")

    monkeypatch.setattr(type(backend.audit_chain), "insert_audit_entry", sql_failure)
    route = install_route(backend, monkeypatch)
    await route.persephone_archive_memory("journal_one", SimpleNamespace(user_id="root"))
    row, archive_count, audit_count = await state(backend)
    assert row["archived_at"] is not None and archive_count == 1 and audit_count == 0


@pytest.mark.asyncio
async def test_required_soft_delete_outage_rolls_back_memory_and_journal(backend, audit_policy, monkeypatch):
    from datetime import datetime, timezone
    from mnemos.persistence.worker_lifecycle import _soft_delete

    await insert(backend)
    fail_append(backend, monkeypatch)
    with pytest.raises(AuditChainContinuityError):
        async with backend.transactional() as tx:
            await _soft_delete(_Ops(tx, transaction_dialect(tx)), "alice", "A", datetime.now(timezone.utc))
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        assert await ops.scalar("SELECT deleted_at FROM memories WHERE id = ?", "journal_one") is None
        assert await ops.scalar("SELECT COUNT(*) FROM federation_changes") == 1
