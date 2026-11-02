"""An archive sweep must not rewrite a replica, and a source update must un-archive it.

FB100 (audit findings F1, F2). The F-3 replica fence already covers the
backend-neutral sweep (``worker_lifecycle.sweep_for_archival``) and ARTEMIS
dedup (``tests/test_federation_replica_fence.py``). What was left:

* the asyncpg PERSEPHONE sweep and the admin lifecycle sweep's own SELECT had
  no fence, so ``PERSEPHONE`` replaced a replica's body with ``ARCHIVED:<id>``
  and the source would never send it again;
* ``prepare_versioned_update`` cleared ``deleted_at`` and ``consolidated_*`` but
  not ``archived_at``, so a source update wrote fresh content onto a row that
  stayed hidden as an archive stub, and a later restore could put the stale
  payload back over it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

pytest.importorskip(
    "zstandard",
    reason="persephone subsystem not installed; pip install mnemos-core[persephone]",
)

from mnemos.core import config as core_config
from mnemos.domain import persephone
from mnemos.domain.persephone import runner
from mnemos.persistence.federation_journal import prepare_versioned_update
from mnemos.persistence.sqlite import SqliteBackend, _execute, _fetch_one

LOCAL_ID = "fed:peer:r1"


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMOS_FEDERATION_REPLICA_FENCE", raising=False)
    core_config.reload_settings()
    instance = SqliteBackend(
        tmp_path / "replica-archive.db",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _insert(tx, *, memory_id, content, created, archived_at=None, source=None):
    await _execute(
        tx.conn,
        "INSERT INTO memories "
        "(id, content, owner_id, namespace, created, updated, federation_source, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, content, "user-1", "default", created, created, source, archived_at),
    )


# ---------------------------------------------------------------------------
# The two throws of the switch are the whole difference.
# ---------------------------------------------------------------------------


def test_eligible_sql_carries_the_replica_fence(monkeypatch):
    monkeypatch.delenv("MNEMOS_FEDERATION_REPLICA_FENCE", raising=False)
    core_config.reload_settings()

    assert "federation_source IS NULL" in runner.eligible_sql()

    monkeypatch.setattr(core_config, "federation_replica_fence_enabled", lambda: False)
    assert "federation_source IS NULL" not in runner.eligible_sql()


@pytest.mark.asyncio
async def test_asyncpg_persephone_sweep_sends_the_fenced_statement(monkeypatch):
    monkeypatch.delenv("MNEMOS_FEDERATION_REPLICA_FENCE", raising=False)
    core_config.reload_settings()

    class _Txn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    class _Conn:
        def __init__(self):
            self.statements = []

        async def fetch(self, sql, *params):
            self.statements.append((" ".join(sql.split()), params))
            return []

        def transaction(self):
            return _Txn()

    class _Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *exc_info):
            return False

    class _Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return _Acquire(self.conn)

    conn = _Conn()
    assert await runner.sweep_for_archival(_Pool(conn), "default", 30, 5) == 0
    ((sql, _params),) = conn.statements
    assert "federation_source IS NULL" in sql
    assert sql.endswith("ORDER BY created ASC LIMIT $3 FOR UPDATE SKIP LOCKED")


# ---------------------------------------------------------------------------
# Executable proof on SQLite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_archive_sweep_skips_replicas(backend):
    from mnemos.domain.admin_lifecycle_repo import AdminLifecycleRepository

    old = datetime.now(timezone.utc) - timedelta(days=60)
    async with backend.transactional() as tx:
        await _insert(tx, memory_id="local-cold", content="local body", created=old)
        await _insert(tx, memory_id=LOCAL_ID, content="replica body", created=old, source="peer")

    async with backend.transactional() as tx:
        archived = await AdminLifecycleRepository().sweep_for_archival(
            tx,
            namespace="default",
            archive_after_days=30,
            batch_size=10,
        )

    assert archived == 1
    async with backend.transactional() as tx:
        local = await _fetch_one(
            tx.conn, "SELECT content, archived_at FROM memories WHERE id = ?", ("local-cold",)
        )
        replica = await _fetch_one(
            tx.conn, "SELECT content, archived_at FROM memories WHERE id = ?", (LOCAL_ID,)
        )
    assert local["content"] == "ARCHIVED:local-cold"
    assert local["archived_at"] is not None
    assert replica["content"] == "replica body"
    assert replica["archived_at"] is None


@pytest.mark.asyncio
async def test_source_update_un_archives_the_replica(backend):
    """A fresh event clears the local archive stub before it writes."""
    old = datetime.now(timezone.utc) - timedelta(days=60)
    async with backend.transactional() as tx:
        await _insert(
            tx,
            memory_id=LOCAL_ID,
            content="ARCHIVED:fed:peer:r1",
            created=old,
            archived_at=old,
            source="peer",
        )

    fresh = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await prepare_versioned_update(tx, "peer", "r1")
        changed = await backend.federation.update_federated_memory_if_newer(
            tx,
            local_id=LOCAL_ID,
            content="fresh text from the source",
            category="facts",
            subcategory=None,
            metadata_json='{"federation_remote_id": "r1"}',
            verbatim_content="fresh text from the source",
            quality_rating=3,
            namespace="default",
            remote_updated=fresh,
        )
        assert changed is True

    async with backend.transactional() as tx:
        row = await _fetch_one(
            tx.conn,
            "SELECT content, archived_at, deleted_at FROM memories WHERE id = ?",
            (LOCAL_ID,),
        )
    assert row["archived_at"] is None
    assert row["deleted_at"] is None
    assert row["content"] == "fresh text from the source"


def test_persephone_reexports_still_work():
    """The runner is the documented import path for the archive format."""
    assert persephone.runner.ARCHIVE_CONTENT_PREFIX == "ARCHIVED:"
