from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mnemos.domain.persephone.runner import sweep_for_archival
from mnemos.domain.admin_lifecycle_repo import AdminLifecycleRepository
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.worker_lifecycle import _Ops
from mnemos.workers.deletion_request_worker import (
    process_one_deletion_request,
    process_one_hard_deletion_request,
)


def test_db2_worker_sql_uses_driver_positional_markers():
    from mnemos.persistence.db2 import _adapt_oracle_to_db2

    rendered = _Ops(SimpleNamespace(conn=None), "db2").sql("UPDATE t SET a = ? WHERE b = ?")

    assert rendered == "UPDATE t SET a = ? WHERE b = ?"
    assert _adapt_oracle_to_db2(rendered, ("one", "two")) == (rendered, ("one", "two"))


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("sqlite", "a = ? AND b = ?"),
        ("mysql", "a = %s AND b = %s"),
        ("oracle", "a = :1 AND b = :2"),
        ("db2", "a = ? AND b = ?"),
    ],
)
def test_worker_sql_paramstyle_matches_each_driver(dialect, expected):
    assert _Ops(SimpleNamespace(conn=None), dialect).sql("a = ? AND b = ?") == expected


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


@pytest.mark.asyncio
async def test_sqlite_admin_lifecycle_repository_covers_crud_archive_restore_and_purge(tmp_path):
    backend = SqliteBackend(tmp_path / "admin-lifecycle.sqlite3", SimpleNamespace())
    repo = AdminLifecycleRepository()
    await backend.open()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    try:
        async with backend.transactional() as tx:
            await tx.conn.execute(
                "INSERT INTO memories (id, content, owner_id, namespace, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("m-admin", "payload", "user-1", "private", old, old),
            )
            request = await repo.create_deletion_request(
                tx,
                target_user_id="user-1",
                target_namespace="private",
                requested_by="root",
                notes="ticket",
            )
            request_id = request["id"]

        async with backend.transactional() as tx:
            assert (await repo.get_deletion_request(tx, request_id))["status"] == "requested"
            assert len(await repo.list_deletion_requests(tx, status="requested", target_user_id="user-1", limit=10)) == 1
            assert (await repo.confirm_deletion_request(tx, request_id))["status"] == "confirmed"
            assert (await repo.cancel_deletion_request(tx, request_id))["status"] == "cancelled"
            await repo.archive_memory(tx, "m-admin", "root")
            state = await repo.fetch_memory_archive_state(tx, "m-admin")
            assert state["archived_at"] is not None
            count, last_run, _oldest = await repo.fetch_persephone_status(tx, namespace="private")
            assert count == 1
            assert last_run is not None
            await repo.restore_memory(tx, "m-admin", "root")

        deleted_at = datetime.now(timezone.utc).replace(microsecond=0)
        async with backend.transactional() as tx:
            await tx.conn.execute("UPDATE memories SET deleted_at = ? WHERE id = ?", (deleted_at, "m-admin"))
            await tx.conn.execute(
                "INSERT INTO deletion_requests "
                "(id, target_user_id, target_namespace, requested_by, status, soft_deleted_at, restore_by) "
                "VALUES (?, ?, ?, ?, 'soft_deleted', ?, ?)",
                ("restore-request", "user-1", "private", "root", deleted_at, deleted_at + timedelta(days=1)),
            )
            existing = await repo.lock_deletion_request(tx, "restore-request")
            restored = await repo.restore_soft_deleted_request(tx, request_id="restore-request", existing=existing)
            assert restored["status"] == "restored"

        async with backend.transactional() as tx:
            await tx.conn.execute("UPDATE memories SET deleted_at = ? WHERE id = ?", (deleted_at, "m-admin"))
            await tx.conn.execute(
                "INSERT INTO deletion_requests "
                "(id, target_user_id, target_namespace, requested_by, status, soft_deleted_at, restore_by) "
                "VALUES (?, ?, ?, ?, 'soft_deleted', ?, ?)",
                ("purge-request", "user-1", "private", "root", deleted_at, deleted_at + timedelta(days=1)),
            )
            existing = await repo.lock_deletion_request(tx, "purge-request")
            purged = await repo.force_purge_soft_deleted_request(
                tx,
                request_id="purge-request",
                existing=existing,
                requested_by="root",
                reason="urgent",
            )
            assert purged["status"] == "hard_deleted"

        async with backend.transactional() as tx:
            assert await (await tx.conn.execute("SELECT 1 FROM memories WHERE id = 'm-admin'")).fetchone() is None
            audit = await (
                await tx.conn.execute("SELECT request_kind FROM deletion_log WHERE memory_id = 'm-admin'")
            ).fetchone()
            assert audit["request_kind"] == "admin_purge"
    finally:
        await backend.close()
