"""NG2-MN-019: version list/get served on SQLite through the VersionRepository ABC.

Acceptance: SQLite-profile app creating a memory, updating it twice, then
``GET /v1/memories/{id}/versions`` returns 3 entries and
``GET /v1/memories/{id}/versions/2`` returns the second snapshot
(currently both answer 503 'requires a PostgreSQL pool').
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes import memories as memories_route
from mnemos.api.routes import versions as versions_route
from mnemos.persistence import sqlite as sqlite_mod
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


@pytest.fixture
def sqlite_client(monkeypatch, tmp_path):
    backend = SqliteBackend(tmp_path / "ng2-mn-019.db", SimpleNamespace())
    asyncio.run(backend.open())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)
    app = FastAPI()
    app.include_router(memories_route.router)
    app.include_router(versions_route.router)
    app.dependency_overrides[get_current_user] = _root
    with TestClient(app) as client:
        yield client, backend
    asyncio.run(backend.close())


def test_sqlite_versions_list_and_get_roundtrip(sqlite_client):
    client, backend = sqlite_client

    resp = client.post(
        "/v1/memories",
        json={"content": "v1 content", "category": "facts"},
    )
    assert resp.status_code == 201, resp.text
    mem_id = resp.json()["id"]

    for body in ("v2 content", "v3 content"):
        resp = client.patch(f"/v1/memories/{mem_id}", json={"content": body})
        assert resp.status_code == 200, resp.text

    resp = client.get(f"/v1/memories/{mem_id}/versions")
    assert resp.status_code == 200, resp.text
    versions = resp.json()
    assert [v["version_num"] for v in versions] == [1, 2, 3]

    resp = client.get(f"/v1/memories/{mem_id}/versions/2")
    assert resp.status_code == 200, resp.text
    # History reads honour the same framing gate as the live row
    # (_should_frame_data): the snapshot text rides inside the
    # untrusted-data frame rather than coming back verbatim.
    assert "v2 content" in resp.json()["content"]

    # dangling references: unknown memory and unknown version stay 404
    assert client.get("/v1/memories/does-not-exist/versions").status_code == 404
    assert client.get(f"/v1/memories/{mem_id}/versions/99").status_code == 404


def _bypass() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


def _collapsed(sql: str) -> str:
    return " ".join(sql.split())


class _StmtCounter:
    """Count data statements routed through the module-level SQLite helpers.

    Patches ``_execute``/``_fetch_one``/``_fetch_val``/``_fetch_all`` on the
    ``mnemos.persistence.sqlite`` module, so inserts/updates keep executing
    against the real connection while every emitted statement is recorded.
    Transaction-control statements (``BEGIN``/``COMMIT``/``ROLLBACK``,
    ``SAVEPOINT``/``RELEASE``, ``PRAGMA``) are excluded: the pins below
    assert on the statements the repository methods themselves emit, not
    on the transaction framing around them.
    """

    _IGNORED_PREFIXES = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA")

    def __init__(self, monkeypatch: Any) -> None:
        self.statements: list[str] = []
        orig_execute = sqlite_mod._execute

        def _record(sql: str) -> None:
            collapsed = _collapsed(sql)
            if not collapsed.startswith(self._IGNORED_PREFIXES):
                self.statements.append(collapsed)

        async def _rec_execute(conn: Any, sql: str, params: Any = ()) -> Any:
            _record(sql)
            return await orig_execute(conn, sql, params)

        # Patch only _execute: _fetch_one/_fetch_val/_fetch_all all funnel
        # through it, so patching them too would double-count every read.
        monkeypatch.setattr(sqlite_mod, "_execute", _rec_execute)


def _base_insert_kwargs(memory_id: str, content: str = "v1 content") -> dict:
    return dict(
        memory_id=memory_id,
        content=content,
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=75,
        owner_id="root",
        namespace="default",
        permission_mode=700,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        embedding=None,
        created=None,
        updated=None,
    )


def _root_persistence() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


@pytest.mark.asyncio
async def test_sqlite_insert_memory_emits_single_statement_when_suppressed(
    tmp_path, monkeypatch
) -> None:
    """Pin: a suppressed seed insert emits exactly one statement (the INSERT).

    Guards the emitted-SQL contract that
    tests/persistence/test_list_paging_tiebreaker_20261103.py golden-pins:
    with the snapshot suppressed, insert_memory must stay single-statement.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-pin.db", SimpleNamespace())
    await backend.open()
    try:
        counter = _StmtCounter(monkeypatch)
        async with backend.transactional() as tx:
            async with tx.savepoint():
                await backend.memories.set_suppress_version_snapshot(tx)
                await backend.memories.insert_memory(
                    tx, **_base_insert_kwargs("pin-insert")
                )
        assert len(counter.statements) == 1, counter.statements
        assert counter.statements[0].startswith("INSERT INTO memories"), (
            counter.statements
        )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_insert_memory_version_snapshot_bounded_statements(
    tmp_path, monkeypatch
) -> None:
    """Pin: an unsuppressed insert emits a bounded statement count.

    The counterpart to
    ``test_sqlite_update_memory_versioned_field_bounded_statements`` for
    the create path: ``insert_memory`` writes the memory row (1 INSERT),
    then ``_write_version_snapshot`` reads the live row, the MAX version
    num, the branch HEAD, and (no branch row yet) the latest version
    (4 SELECTs), then writes the version row and the branch UPSERT
    (2 INSERTs) — 1 INSERT + 4 SELECTs + 2 INSERTs = 7 statements.
    Unlike the update path there is no pre-write IS DISTINCT FROM gate
    SELECT: the trigger's INSERT branch snapshots unconditionally.
    KNOWN_LIMITATIONS.md documents the per-write cost; this locks the
    insert half of that contract so a future change that adds snapshot
    reads (or drops the branch UPSERT) cannot silently alter the
    transaction shape.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-inscost.db", SimpleNamespace())
    await backend.open()
    try:
        counter = _StmtCounter(monkeypatch)
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx, **_base_insert_kwargs("pin-inscost")
            )
        kinds = [s.split(" ", 2)[0] for s in counter.statements]
        assert len(counter.statements) == 7, counter.statements
        assert kinds[0] == "INSERT", counter.statements
        assert counter.statements[0].startswith("INSERT INTO memories"), (
            counter.statements
        )
        assert kinds.count("SELECT") == 4, counter.statements
        assert kinds.count("INSERT") == 3, counter.statements
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_update_memory_non_versioned_field_emits_no_snapshot(
    tmp_path, monkeypatch
) -> None:
    """Pin: recall-only bumps emit exactly one statement (gate works).

    ``update_memory`` must not write a snapshot when only non-versioned
    fields (e.g. recall_count/last_recalled_at via quality_rating-style
    metadata-adjacent writes) change — same rule as the Postgres trigger's
    UPDATE branch.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-gate.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            async with tx.savepoint():
                await backend.memories.set_suppress_version_snapshot(tx)
                await backend.memories.insert_memory(
                    tx, **_base_insert_kwargs("pin-gate")
                )
        counter = _StmtCounter(monkeypatch)
        async with backend.transactional() as tx:
            row = await backend.memories.update_memory(
                tx,
                "pin-gate",
                visibility=_bypass(),
                fields={"quality_rating": 10},
            )
            assert row is not None
        assert len(counter.statements) == 1, counter.statements
        assert counter.statements[0].startswith("UPDATE memories"), (
            counter.statements
        )
        async with backend.transactional() as tx:
            versions = await backend.memory_versions.list_memory_versions(
                tx, memory_id="pin-gate", branch="main", visibility=_root_persistence()
            )
        assert versions == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_update_memory_versioned_field_bounded_statements(
    tmp_path, monkeypatch
) -> None:
    """Pin: a versioned-field update emits a bounded statement count.

    Documents the per-update cost of the in-repo snapshot
    (NG2-MN-019): 1 pre-write SELECT (the trigger's OLD row for the IS
    DISTINCT FROM gate) + 1 UPDATE + 1 live-row SELECT + 1 MAX SELECT +
    1 HEAD SELECT + 1 latest-version SELECT (parent fallback when no
    branch row exists yet) + 1 version INSERT + 1 branch UPSERT = 8
    statements. This is the trade-off against the Postgres trigger
    (which fires server-side); if this count grows, the
    performance-visible change is deliberate.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-cost.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            async with tx.savepoint():
                await backend.memories.set_suppress_version_snapshot(tx)
                await backend.memories.insert_memory(
                    tx, **_base_insert_kwargs("pin-cost")
                )
        counter = _StmtCounter(monkeypatch)
        async with backend.transactional() as tx:
            row = await backend.memories.update_memory(
                tx,
                "pin-cost",
                visibility=_bypass(),
                fields={"content": "v2 content"},
            )
            assert row is not None
        kinds = [s.split(" ", 2)[0] for s in counter.statements]
        assert len(counter.statements) == 8, counter.statements
        assert kinds[0] == "SELECT", counter.statements
        assert kinds[1] == "UPDATE", counter.statements
        assert kinds.count("SELECT") == 5, counter.statements
        assert kinds.count("INSERT") == 2, counter.statements
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_update_memory_noop_versioned_field_writes_no_snapshot(
    tmp_path, monkeypatch
) -> None:
    """Regression: a versioned field set to its current value mints nothing.

    Parity with the Postgres trigger's UPDATE branch
    (``OLD.x IS DISTINCT FROM NEW.x``): resending unchanged content (or
    metadata, category, ...) must not grow ``memory_versions``. The
    gate still emits its 2 statements (pre-write SELECT + UPDATE) but
    no snapshot follows.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-noop.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            async with tx.savepoint():
                await backend.memories.set_suppress_version_snapshot(tx)
                await backend.memories.insert_memory(
                    tx, **_base_insert_kwargs("pin-noop")
                )
        async with backend.transactional() as tx:
            row = await backend.memories.update_memory(
                tx,
                "pin-noop",
                visibility=_bypass(),
                fields={"content": "v1 content"},
            )
            assert row is not None
            row = await backend.memories.update_memory(
                tx,
                "pin-noop",
                visibility=_bypass(),
                fields={"metadata": "{}"},
            )
            assert row is not None
            versions = await backend.memory_versions.list_memory_versions(
                tx, memory_id="pin-noop", branch="main", visibility=_root_persistence()
            )
            assert versions == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_version_snapshot_failure_rolls_back_memory_update(
    tmp_path, monkeypatch
) -> None:
    """Pin: a mid-transaction snapshot failure leaves no partial update.

    The memories UPDATE and the version INSERT ride the same transaction:
    when the version write raises, the caller-visible error must surface
    and neither the updated row nor a version row may persist.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-rollback.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            async with tx.savepoint():
                await backend.memories.set_suppress_version_snapshot(tx)
                await backend.memories.insert_memory(
                    tx, **_base_insert_kwargs("pin-rollback")
                )

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated version-write failure")

        monkeypatch.setattr(
            backend.memories, "_write_version_snapshot", _boom
        )
        with pytest.raises(RuntimeError, match="simulated version-write failure"):
            async with backend.transactional() as tx:
                await backend.memories.update_memory(
                    tx,
                    "pin-rollback",
                    visibility=_bypass(),
                    fields={"content": "v2 content"},
                )

        async with backend.transactional() as tx:
            row = await backend.memories.get_memory(
                tx, "pin-rollback", visibility=_bypass()
            )
            assert row is not None
            assert row["content"] == "v1 content"
            versions = await backend.memory_versions.list_memory_versions(
                tx, memory_id="pin-rollback", branch="main", visibility=_root_persistence()
            )
            assert versions == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_soft_deleted_versions_hidden_from_list_get_and_contents(
    tmp_path,
) -> None:
    """Soft-deleted snapshots must stay hidden on every ABC read path.

    NG2-MN-019 parity: every server backend filters ``deleted_at IS NULL``
    in list/get/diff version reads; SQLite must match. A tombstoned row
    is not listed, not fetched, and not diffed — on root bypass and on
    the non-root visibility-clause path alike.
    """
    import mnemos.persistence.sqlite as sqlite_mod

    backend = SqliteBackend(tmp_path / "ng2-mn-019-deleted.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id="del-mem",
                content="v1 content",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id="root",
                namespace="default",
                permission_mode=700,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="v1 content",
                embedding=None,
                created=None,
                updated=None,
            )
            # Tombstone the v1 snapshot directly (test-only raw UPDATE —
            # there is no version-delete ABC surface).
            raw = sqlite_mod._sqlite_tx(tx).conn
            await sqlite_mod._execute(
                raw,
                "UPDATE memory_versions SET deleted_at = CURRENT_TIMESTAMP "
                "WHERE memory_id = ? AND branch = ?",
                ("del-mem", "main"),
            )

        async with backend.transactional() as tx:
            root = _root_persistence()
            versions = await backend.memory_versions.list_memory_versions(
                tx, memory_id="del-mem", branch="main", visibility=root
            )
            assert versions == []
            row = await backend.memory_versions.get_memory_version(
                tx, "del-mem", 1, "main", root
            )
            assert row is None
            contents = await backend.memory_versions.fetch_version_contents(
                tx, "del-mem", [1], "main", root
            )
            assert contents == []
    finally:
        await backend.close()


def _member_visibility() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id="alice",
        group_ids=("team",),
        namespace="default",
        exclude_namespaces=("vault",),
    )


@pytest.mark.asyncio
async def test_sqlite_version_reads_bind_namespace_exactly_once(tmp_path) -> None:
    """The namespace pin is rendered INSIDE ``_version_visibility_clause``.

    Review fix (R1): the builder used to render the tenancy predicate
    WITHOUT the namespace pin, and every SQLite call site appended a
    second ``AND namespace = ?`` — duplicating the predicate and binding
    the same value twice, with the trailing bind no longer aligned to the
    placeholder it was meant for. The builder now owns the pin (mirroring
    the MySQL / Oracle / Db2 builders), so each version-read SQL must
    contain exactly one ``namespace = ?`` with exactly one matching bind
    in the clause-param tail.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-bind.db", SimpleNamespace())
    await backend.open()
    try:
        member = _member_visibility()
        fake_tx = sqlite_mod.SqliteTransaction(conn=object())

        captured: list[tuple[str, tuple]] = []

        async def _rec_all(conn, sql, params):
            captured.append((sql, tuple(params)))
            return []

        async def _rec_one(conn, sql, params):
            captured.append((sql, tuple(params)))
            return None

        monkeypatcher = pytest.MonkeyPatch()
        monkeypatcher.setattr(sqlite_mod, "_fetch_all", _rec_all)
        monkeypatcher.setattr(sqlite_mod, "_fetch_one", _rec_one)
        try:
            repo = backend.memory_versions
            await repo.list_memory_versions(fake_tx, "m1", "main", member)
            await repo.get_memory_version(fake_tx, "m1", 1, "main", member)
            await repo.fetch_version_contents(fake_tx, "m1", [1, 2], "main", member)
        finally:
            monkeypatcher.undo()

        assert len(captured) == 3, "all three ABC reads must execute"
        for sql, params in captured:
            assert sql.count("namespace = ?") == 1, (
                f"duplicated namespace pin in {sql!r}"
            )
            # Every placeholder is bound; the namespace value is in the
            # clause-param tail exactly once.
            assert sql.count("?") == len(params)
            tail = params[2:] if sql.startswith("SELECT version_num, content") else params[3:]
            assert tail.count("default") == 1, (
                f"namespace value must be bound exactly once: {params!r}"
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_update_memory_metadata_alias_skips_gate(tmp_path) -> None:
    """The snapshot gate sees only the exact column key (F4 contract).

    The Postgres trigger fires on the COLUMN regardless of client alias,
    but the SQLite gate derives ``touched`` from ``fields`` keys — so a
    caller writing ``metadata`` via an alias (``metadata_json``) bypasses
    the gate. The repository surface exposes only ``metadata`` (direct
    callers match); this test pins that the gate is key-named, not
    alias-aware, so a future alias-accepting caller cannot silently
    skip version history on SQLite while Postgres writes it.
    """
    backend = SqliteBackend(tmp_path / "ng2-mn-019-key.db", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id="key-mem",
                content="base content",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id="root",
                namespace="default",
                permission_mode=700,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="base content",
                embedding=None,
                created=None,
                updated=None,
            )
            root = _root_persistence()
            rows = await backend.memory_versions.list_memory_versions(
                tx, memory_id="key-mem", branch="main", visibility=root
            )
            assert len(rows) == 1, "create path emits exactly one snapshot"

            # Exact column key: gate fires, a second snapshot is written
            # (the SQLite binding layer stores metadata as TEXT, so bind
            # the JSON string — the canonical-comparison gate normalizes
            # both sides regardless of the stored form).
            row = await backend.memories.update_memory(
                tx,
                "key-mem",
                visibility=root,
                fields={"metadata": '{"k": "v"}'},
            )
            assert row is not None
            rows = await backend.memory_versions.list_memory_versions(
                tx, memory_id="key-mem", branch="main", visibility=root
            )
            assert len(rows) == 2, "versioned write via 'metadata' writes history"

            # Aliased key: filtered out of ``keys`` entirely — no UPDATE
            # column is applied, the gate never runs, and the caller gets
            # the read path back.
            row = await backend.memories.update_memory(
                tx,
                "key-mem",
                visibility=root,
                fields={"metadata_json": {"k": "w"}},
            )
            assert row is not None, "unknown key falls back to get_memory"
            rows = await backend.memory_versions.list_memory_versions(
                tx, memory_id="key-mem", branch="main", visibility=root
            )
            assert len(rows) == 2, (
                "aliased metadata write must NOT write history "
                "(gate is key-named; repository surface uses 'metadata')"
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_postgres_version_reads_apply_exclude_namespaces() -> None:
    """Postgres version reads must honour ``exclude_namespaces`` (vault).

    Parity with the SQLite / MySQL / Oracle / Db2 version repositories:
    non-root list/get/fetch_version_contents append the vault subtraction
    so a vault-namespace snapshot never reaches the caller. The non-root
    path always pins ``namespace = <visibility.namespace>`` (fail-closed
    when unset), so the subtraction is a bare ``namespace NOT IN (...)``:
    no NULL row can pass the pin and the shared-renderer ``IS NULL OR``
    form would be dead code on these three methods. Uses a fake
    connection capturing SQL + params (no live Postgres needed).

    Placeholder-index contract (Postgres ``$N`` binds are positional):
    placeholders must form the contiguous sequence 1..len(args) — no
    skipped index (which asyncpg would reject) — and the ``vault`` bind
    must sit at the position its ``$N`` placeholder names.
    """
    import re
    from unittest.mock import MagicMock

    from mnemos.persistence.postgres import (
        PostgresTransaction,
        PostgresVersionRepository,
    )

    class _FakeConn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def fetch(self, sql: str, *args):
            self.calls.append((sql, args))
            return []

        async def fetchrow(self, sql: str, *args):
            self.calls.append((sql, args))
            return None

    def _member_filter() -> VisibilityFilter:
        return VisibilityFilter(
            scope=VisibilityScope.READABLE,
            user_id="alice",
            group_ids=[],
            namespace="default",
            exclude_namespaces=("vault",),
        )

    def _assert_sequential(sql: str, args: tuple) -> None:
        """$N placeholders must be exactly 1..len(args), vault bound at its slot.

        Placeholder ``$N`` binds ``args[N-1]`` (1-based SQL vs 0-based
        tuple), so the vault value must sit at ``args[vault_slot - 1]``
        where ``vault_slot`` is the placeholder number inside the
        ``NOT IN (...)`` term — not at a list-index derived from it.
        """
        nums = sorted(int(n) for n in re.findall(r"\$(\d+)", sql))
        assert nums == list(range(1, len(args) + 1)), f"non-contiguous binds: {nums}"
        m = re.search(r"NOT IN \(([^)]*)\)", sql)
        assert m, "expected NOT IN (...) term in version-read SQL"
        # Bare NOT IN (no dead IS NULL): NULL rows are already ruled out
        # by the non-root ``namespace = $N`` pin on these methods.
        assert "IS NULL" not in m.group(0)
        vault_slot = min(int(n) for n in re.findall(r"\$(\d+)", m.group(1)))
        assert args[vault_slot - 1] == "vault", (
            f"vault bind mismatch: ${vault_slot} -> {args[vault_slot - 1]!r}"
        )

    repo = PostgresVersionRepository()
    filt = _member_filter()

    # list_memory_versions: vault subtraction present, placeholder==param.
    conn = _FakeConn()
    await repo.list_memory_versions(PostgresTransaction(conn, MagicMock()), "m1", "main", filt)
    sql, args = conn.calls[-1]
    assert "NOT IN" in sql
    assert "vault" in args
    assert sql.count("$") == len(args)
    _assert_sequential(sql, args)

    # get_memory_version: same contract on fetchrow.
    conn2 = _FakeConn()
    await repo.get_memory_version(PostgresTransaction(conn2, MagicMock()), "m1", 1, "main", filt)
    sql2, args2 = conn2.calls[-1]
    assert "NOT IN" in sql2
    assert "vault" in args2
    assert sql2.count("$") == len(args2)
    _assert_sequential(sql2, args2)

    # fetch_version_contents: same contract on the diff backing store.
    conn3 = _FakeConn()
    await repo.fetch_version_contents(PostgresTransaction(conn3, MagicMock()), "m1", [1, 2], "main", filt)
    sql3, args3 = conn3.calls[-1]
    assert "NOT IN" in sql3
    assert "vault" in args3
    assert sql3.count("$") == len(args3)
    _assert_sequential(sql3, args3)
