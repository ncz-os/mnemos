from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mnemos.domain.persephone.runner import sweep_for_archival
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.workers.deletion_request_worker import (
    process_one_deletion_request,
    process_one_hard_deletion_request,
)


@pytest.mark.asyncio
async def test_sqlite_deletion_worker_claims_and_soft_deletes_atomically(tmp_path):
    backend = SqliteBackend(tmp_path / "worker.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await tx.conn.execute(
                "INSERT INTO memories (id, content, owner_id, namespace) VALUES (?, ?, ?, ?)",
                ("m1", "secret", "user-1", "private"),
            )
            await tx.conn.execute(
                "INSERT INTO deletion_requests "
                "(id, target_user_id, target_namespace, requested_by, confirmed_at, status) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 'confirmed')",
                ("request-1", "user-1", "private", "admin"),
            )

        result = await process_one_deletion_request(backend)

        assert result is not None
        assert result.status == "soft_deleted"
        async with backend.transactional() as tx:
            memory = await (await tx.conn.execute("SELECT deleted_at FROM memories WHERE id = 'm1'")).fetchone()
            request = await (
                await tx.conn.execute(
                    "SELECT status, soft_deleted_at, restore_by FROM deletion_requests WHERE id = 'request-1'"
                )
            ).fetchone()
        assert memory["deleted_at"] is not None
        assert request["status"] == "soft_deleted"
        assert request["soft_deleted_at"] is not None
        assert request["restore_by"] is not None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_persephone_worker_archives_through_backend_transaction(tmp_path):
    backend = SqliteBackend(tmp_path / "persephone.sqlite3", SimpleNamespace())
    await backend.open()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    try:
        async with backend.transactional() as tx:
            await tx.conn.execute(
                "INSERT INTO memories (id, content, owner_id, namespace, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("m-cold", "cold payload", "user-1", "default", old, old),
            )

        archived = await sweep_for_archival(backend, "default", 30, 10)

        assert archived == 1
        async with backend.transactional() as tx:
            memory = await (
                await tx.conn.execute("SELECT content, archived_at FROM memories WHERE id = 'm-cold'")
            ).fetchone()
            archive = await (
                await tx.conn.execute("SELECT compression_algo, schema_version FROM memory_archive WHERE id = 'm-cold'")
            ).fetchone()
        assert memory["content"] == "ARCHIVED:m-cold"
        assert memory["archived_at"] is not None
        assert archive["compression_algo"] == "zstd"
        assert archive["schema_version"] == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_hard_delete_preserves_durable_audit_log(tmp_path):
    backend = SqliteBackend(tmp_path / "hard-delete.sqlite3", SimpleNamespace())
    await backend.open()
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        async with backend.transactional() as tx:
            await tx.conn.execute(
                "INSERT INTO memories (id, content, owner_id, namespace, deleted_at) VALUES (?, ?, ?, ?, ?)",
                ("m-expired", "erase me", "user-1", "private", yesterday),
            )
            await tx.conn.execute(
                "INSERT INTO deletion_requests "
                "(id, target_user_id, target_namespace, requested_by, status, soft_deleted_at, restore_by) "
                "VALUES (?, ?, ?, ?, 'soft_deleted', ?, ?)",
                ("request-hard", "user-1", "private", "admin", yesterday, yesterday),
            )

        result = await process_one_hard_deletion_request(backend)

        assert result is not None
        assert result.status == "hard_deleted"
        async with backend.transactional() as tx:
            memory_count = await (
                await tx.conn.execute("SELECT COUNT(*) AS n FROM memories WHERE id = 'm-expired'")
            ).fetchone()
            audit = await (
                await tx.conn.execute(
                    "SELECT memory_id, content_hash, request_kind FROM deletion_log WHERE memory_id = 'm-expired'"
                )
            ).fetchone()
        assert memory_count["n"] == 0
        assert audit["memory_id"] == "m-expired"
        assert len(audit["content_hash"]) == 64
        assert audit["request_kind"] == "tombstone_collected"
    finally:
        await backend.close()
