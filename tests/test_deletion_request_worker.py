"""GDPR deletion-request Phase B worker tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.persistence import worker_lifecycle
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.workers import deletion_request_worker as worker


def _backend_mock():
    return SimpleNamespace(transactional=MagicMock())


def _target_labels() -> set[str]:
    return {
        label
        for label, _table, _sql in (
            *worker._OWNER_NAMESPACE_SOFT_DELETE_SQL,
            *worker._SOFT_DELETE_SQL,
        )
    }


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        for key in list(self.store):
            if match == "mnemos:search:*" and key.startswith("mnemos:search:"):
                yield key


@pytest.mark.asyncio
async def test_worker_delegates_soft_delete_to_backend_lifecycle_abc(monkeypatch):
    backend = _backend_mock()
    payload = {
        "request_id": "request-1",
        "target_user_id": "alice",
        "target_namespace": None,
        "status": "soft_deleted",
        "row_counts": {"memories": 1},
        "soft_deleted_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "restore_by": datetime(2026, 5, 31, tzinfo=timezone.utc),
        "verification_attempts": 1,
        "remaining_counts": {},
    }
    process_backend = AsyncMock(return_value=payload)
    monkeypatch.setattr(worker_lifecycle, "process_one_deletion_request", process_backend)

    result = await worker.process_one_deletion_request(backend)

    assert result == worker.DeletionRequestResult(**payload)
    process_backend.assert_awaited_once_with(
        backend,
        verify_attempts=worker.DEFAULT_VERIFY_ATTEMPTS,
        restore_days=worker.RESTORE_GRACE_DAYS,
    )


@pytest.mark.asyncio
async def test_worker_delegates_hard_delete_to_backend_lifecycle_abc(monkeypatch):
    backend = _backend_mock()
    payload = {
        "request_id": "request-1",
        "target_user_id": "alice",
        "target_namespace": "tenant-a",
        "status": "hard_deleted",
        "row_counts": {"memories": 1},
        "soft_deleted_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "restore_by": datetime(2026, 5, 31, tzinfo=timezone.utc),
        "hard_deleted_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    }
    process_backend = AsyncMock(return_value=payload)
    monkeypatch.setattr(worker_lifecycle, "process_one_hard_deletion_request", process_backend)

    result = await worker.process_one_hard_deletion_request(backend)

    assert result == worker.DeletionRequestResult(**payload)
    process_backend.assert_awaited_once_with(backend)


@pytest.mark.asyncio
async def test_main_builds_and_closes_configured_persistence_backend(monkeypatch):
    backend = SimpleNamespace(close=AsyncMock())
    build_backend = AsyncMock(return_value=("sqlite", backend))
    loop = AsyncMock()
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", build_backend)
    monkeypatch.setattr(worker, "deletion_request_worker_loop", loop)

    await worker.main(phase="hard_delete")

    build_backend.assert_awaited_once_with()
    loop.assert_awaited_once_with(backend, phase="hard_delete")
    backend.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_worker_soft_delete_is_namespace_scoped():
    from mnemos.persistence.deletion_ops import soft_delete_target

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")

    counts = await soft_delete_target(conn, "alice", "tenant-a")

    assert set(counts) == _target_labels()
    assert all(call.args[1] == "alice" for call in conn.execute.await_args_list)
    assert all(call.args[2] == "tenant-a" for call in conn.execute.await_args_list)
    assert all("namespace = $2::text" in call.args[0] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_worker_batch_uses_backend_for_each_request(monkeypatch):
    backend = _backend_mock()
    process = AsyncMock(
        side_effect=[
            worker.DeletionRequestResult(
                request_id="request-1",
                target_user_id="alice",
                target_namespace=None,
                status="soft_deleted",
                row_counts={"memories": 1},
                soft_deleted_at=None,
                restore_by=None,
            ),
            None,
        ]
    )
    monkeypatch.setattr(worker, "process_one_deletion_request", process)

    assert await worker.process_deletion_requests(backend, batch_size=2) == {
        "memories": 1,
        "requests": 1,
    }
    assert process.await_args_list[0].args == (backend,)


@pytest.mark.asyncio
async def test_restore_target_reverses_only_the_soft_delete_batch_timestamp():
    soft_deleted_at = datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    counts = await worker.restore_soft_deleted_target(
        conn,
        "alice",
        "tenant-a",
        soft_deleted_at,
    )

    assert counts == {label: 1 for label in _target_labels()}
    assert all("SET deleted_at = NULL" in call.args[0] for call in conn.execute.await_args_list)
    assert all("$3::timestamptz" in call.args[0] for call in conn.execute.await_args_list)
    assert all(call.args[3] == soft_deleted_at for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_restore_target_invalidates_search_and_stats_cache(monkeypatch):
    cache = _FakeCache()
    cache.store["mnemos:search:primed"] = '{"count":1,"memories":[]}'
    cache.store["stats:global"] = "{}"
    cache.store["stats:global:v2"] = "{}"
    monkeypatch.setattr(lifecycle, "_cache", cache)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    await worker.restore_soft_deleted_target(
        conn,
        "alice",
        "tenant-a",
        datetime(2026, 5, 1, 23, 5, 0, tzinfo=timezone.utc),
    )

    assert "mnemos:search:primed" in cache.deleted
    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted


@pytest.mark.asyncio
async def test_worker_hard_delete_invalidates_search_and_stats_cache(monkeypatch):
    cache = _FakeCache()
    cache.store["mnemos:search:primed"] = '{"count":1,"memories":[]}'
    cache.store["stats:global"] = "{}"
    cache.store["stats:global:v2"] = "{}"
    monkeypatch.setattr(lifecycle, "_cache", cache)
    payload = {
        "request_id": "request-1",
        "target_user_id": "alice",
        "target_namespace": "tenant-a",
        "status": "hard_deleted",
        "row_counts": {},
        "soft_deleted_at": None,
        "restore_by": None,
        "hard_deleted_at": None,
    }
    monkeypatch.setattr(
        worker_lifecycle,
        "process_one_hard_deletion_request",
        AsyncMock(return_value=payload),
    )

    await worker.process_one_hard_deletion_request(_backend_mock())

    assert "mnemos:search:primed" in cache.deleted
    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted


def test_hard_delete_sql_order_keeps_fk_children_before_parents():
    labels = [label for label, _table, _sql in worker._HARD_DELETE_SQL]

    assert labels[:5] == [
        "memory_versions",
        "memory_branches",
        "session_messages",
        "session_memory_injections",
        "graeae_audit_log",
    ]
    assert labels[5:9] == [
        "memory_archive",
        "memories",
        "sessions",
        "graeae_consultations",
    ]
    assert labels[9:13] == ["kg_triples", "journal", "entities", "state"]
    # Identity / credential tables come last: every soft-deleted row must
    # be gone before we revoke api_keys, drop oauth rows (raw claims, IP,
    # user agent), and (for all-namespace deletions) the user record
    # itself. The SQL for these does NOT filter on ``deleted_at IS NOT
    # NULL`` -- those tables don't carry that column -- but they only run
    # after the rest of the scope is already verified clean.
    assert labels[13:] == ["api_keys", "oauth_sessions", "oauth_identities", "user_groups", "users"]
    assert len(labels) == 18


def test_hard_delete_sql_revokes_api_keys_before_deleting_oauth_rows():
    """GDPR finding: the worker must revoke and delete credentials, then
    drop OAuth identities/sessions, before recording completion. An
    all-namespace hard delete that left these rows behind would keep the
    email, raw OAuth claims, IP addresses, and active API keys of a
    user that the audit log claims was deleted.
    """
    labels = [label for label, _table, _sql in worker._HARD_DELETE_SQL]

    api_keys_index = labels.index("api_keys")
    oauth_sessions_index = labels.index("oauth_sessions")
    oauth_identities_index = labels.index("oauth_identities")
    user_groups_index = labels.index("user_groups")
    users_index = labels.index("users")
    # Identity tables must come after the rest of the deletion scope is
    # already removed -- this protects any in-flight OAuth check from
    # racing past the revoke: the keys are revoked first, sessions/ids
    # are dropped next, and finally the user row.
    assert api_keys_index > labels.index("graeae_audit_log")
    assert api_keys_index < oauth_sessions_index < oauth_identities_index < user_groups_index < users_index

    # The api_keys statement must set revoked=TRUE so that any concurrent
    # auth check that already loaded the row sees the credential disabled
    # before the DELETE lands.
    api_keys_sql = next(sql for label, _table, sql in worker._HARD_DELETE_SQL if label == "api_keys")
    assert "revoked = TRUE" in api_keys_sql
    assert "user_id = $1" in api_keys_sql

    # The user row is only removed on an all-namespace deletion. A scoped
    # deletion must keep the user row so other namespaces keep working.
    users_sql = next(sql for label, _table, sql in worker._HARD_DELETE_SQL if label == "users")
    assert "id = $1" in users_sql
    assert "$2::text IS NULL" in users_sql


def test_hard_delete_live_row_count_covers_identity_tables():
    """Verification must check the identity tables too -- otherwise the
    sweep+verify loop can pass on the first pass and still leave active
    credentials / OAuth rows behind.
    """
    labels = {label for label, _sql in worker._LIVE_ROW_COUNT_SQL}
    assert "api_keys" in labels
    assert "oauth_sessions" in labels
    assert "oauth_identities" in labels
    assert "user_groups" in labels
    assert "users" in labels


@pytest.mark.asyncio
async def test_memory_read_path_filters_soft_deleted_rows():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    tx = PostgresTransaction(conn, MagicMock())
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )

    row = await PostgresMemoryRepository().get_memory(
        tx,
        "mem-soft-deleted",
        visibility=visibility,
    )

    assert row is None
    sql = conn.fetchrow.await_args.args[0]
    assert "FROM memories WHERE id=$1 AND deleted_at IS NULL" in sql
