"""GET /admin/compression/status — F-15 read-only introspection.

Backlog item F-15 (docs/FEATURES_TO_BE_DONE.md): the only ways to
learn the effective ``MNEMOS_CONTEST_ENABLED`` setting, the compression
queue depth, or the variant count were the *mutating* enqueue routes or
log scraping. This route closes that gap with a strictly read-only
admin endpoint in the same shape ``GET /v1/federation/status`` uses:

  * effective contest / apollo settings from the live config object;
  * engines installed (apollo / artemis import probes — never
    constructs an engine);
  * queue stats via the persistence ABC
    (``CompressionQueueRepository.get_compression_queue_status``);
  * variant count, ``null`` + reason when the backend cannot supply it
    (``-1`` sentinel), never a fabricated zero.

The route MUST NOT mutate anything and MUST NOT call the enqueue
routes. Per the §0b governing rule the route calls the ABC; no raw
``_lc._pool`` access.

SQLite is the live arm: it exercises the real
``SqliteCompressionQueueRepository`` against a real schema, so the
documented wire shape is pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import mnemos.core.lifecycle as lifecycle
from mnemos.api.routes.admin import (
    CompressionStatusResponse,
    compression_status,
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    """Real SqliteBackend wired into lifecycle, schema ensured on open."""
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "f15.sqlite3", SimpleNamespace())
    await backend.open()
    old_backend = lifecycle._persistence_backend
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    yield backend
    lifecycle._persistence_backend = old_backend
    await backend.close()


def _root():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="root", group_ids=[], role="root", namespace="default",
        authenticated=True,
    )


# ── documented shape (SQLite arm) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_compression_status_returns_documented_shape(sqlite_backend):
    """The exact key set the API reference documents, on SQLite."""
    resp = await compression_status(_=_root())
    assert isinstance(resp, CompressionStatusResponse)
    dump = resp.model_dump()

    assert set(dump.keys()) == {
        "contest_enabled",
        "apollo_enabled",
        "engines",
        "queue",
        "variant_count",
        "variant_count_reason",
    }
    assert isinstance(dump["contest_enabled"], bool)
    assert isinstance(dump["apollo_enabled"], bool)
    assert set(dump["engines"].keys()) == {
        "apollo",
        "artemis",
        "apollo_reason",
        "artemis_reason",
    }
    for engine in ("apollo", "artemis"):
        assert set(dump["engines"][engine].keys()) == {"installed", "enabled"}
        assert isinstance(dump["engines"][engine]["installed"], bool)
        assert isinstance(dump["engines"][engine]["enabled"], bool)

    # On a freshly-migrated SQLite schema the queue is empty and the
    # variant count is a real zero (the table EXISTS — zero is a
    # measurement, not a fabrication), so no reason is attached.
    queue = dump["queue"]
    assert queue["reason"] is None
    assert queue["stats"] == {
        "total": 0,
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "variants": 0,
    }
    assert dump["variant_count"] == 0
    assert dump["variant_count_reason"] is None


@pytest.mark.asyncio
async def test_compression_status_reports_real_counts(sqlite_backend):
    """Queue depth + variant count reflect actual rows, via the ABC."""
    from mnemos.persistence.sqlite import _execute

    for mem_id in ("f15-m1", "f15-m2", "f15-m3"):
        async with sqlite_backend.transactional() as tx:
            await sqlite_backend.memories.insert_memory(
                tx,
                memory_id=mem_id,
                content="content that is long enough to matter",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=75,
                owner_id="alice",
                namespace="alice-ns",
                permission_mode=0,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="content that is long enough to matter",
                created=None,
                updated=None,
            )

    async with sqlite_backend.transactional() as tx:
        conn = tx.conn
        for mem_id, status in (
            ("f15-m1", "pending"),
            ("f15-m2", "running"),
            ("f15-m3", "done"),
        ):
            await _execute(
                conn,
                "INSERT INTO memory_compression_queue "
                "(memory_id, owner_id, reason, status, priority, scoring_profile) "
                "VALUES (?, 'alice', 'manual', ?, 0, 'balanced')",
                (mem_id, status),
            )
        for mem_id in ("f15-m1", "f15-m2"):
            await _execute(
                conn,
                "INSERT INTO memory_compressed_variants "
                "(memory_id, owner_id, engine_id, compressed_content, "
                " compressed_tokens, compression_ratio, quality_score, "
                " composite_score) "
                "VALUES (?, 'alice', 'artemis', 'x', 10, 0.5, 0.8, 0.7)",
                (mem_id,),
            )

    resp = await compression_status(_=_root())
    assert resp.queue.stats == {
        "total": 3,
        "pending": 1,
        "running": 1,
        "done": 1,
        "failed": 0,
        "variants": 2,
    }
    assert resp.variant_count == 2
    assert resp.variant_count_reason is None
    assert resp.queue.reason is None


# ── contest setting: disabled vs enabled ────────────────────────────────────


def _with_effective_settings(monkeypatch, **overrides):
    """Force the effective settings object for one test.

    ``config.reload_settings()`` alone is not enough here:
    pydantic-settings caches ``SettingsCustomizations`` per class, so a
    process whose environment was captured at first use would keep the
    stale env overrides. Mutating the already-built singleton is the
    deterministic way to pin the *effective* ``MNEMOS_CONTEST_ENABLED``
    value the route reports (the route reads the live singleton,
    which is exactly the operator-visible contract: what this
    process has loaded, not what the shell happened to export).

    The override is applied through ``monkeypatch.setitem`` on the
    sub-settings instance ``__dict__`` (where pydantic stores field
    values), which bypasses any frozen/validator guards the same way a
    direct ``object.__setattr__`` would, and is reverted by monkeypatch
    at teardown so no test leaks a changed setting to the next.
    """
    import mnemos.core.config as config

    settings = config.get_settings()
    for key, value in overrides.items():
        monkeypatch.setitem(settings.compression.__dict__, key, value)
    return settings


@pytest.mark.asyncio
async def test_compression_status_reports_contest_disabled(
    sqlite_backend, monkeypatch
):
    _with_effective_settings(monkeypatch, contest_enabled=False)
    resp = await compression_status(_=_root())
    assert resp.contest_enabled is False
    # APOLLO gates off when the contest is disabled. ARTEMIS is the
    # default contest engine: the distillation worker adds it when
    # the contest is on and the extra is installed, with no separate
    # per-engine toggle of its own — so it gates off with the
    # contest too. Installed state is reported regardless.
    assert resp.engines.apollo["enabled"] is False
    assert resp.engines.artemis["enabled"] is False
    assert resp.engines.artemis["installed"] is True


@pytest.mark.asyncio
async def test_compression_status_reports_contest_enabled(
    sqlite_backend, monkeypatch
):
    _with_effective_settings(monkeypatch, contest_enabled=True)
    resp = await compression_status(_=_root())
    assert resp.contest_enabled is True
    assert resp.engines.apollo["enabled"] is True
    # Contest on + artemis installed -> artemis enabled.
    assert resp.engines.artemis["enabled"] is True


@pytest.mark.asyncio
async def test_compression_status_reports_apollo_disabled(
    sqlite_backend, monkeypatch
):
    """MNEMOS_APOLLO_ENABLED=false disables apollo even though the
    contest itself is on. Artemis has no separate toggle — it rides
    the contest gate — so it stays enabled."""
    _with_effective_settings(
        monkeypatch, contest_enabled=True, apollo_enabled=False
    )
    resp = await compression_status(_=_root())
    assert resp.contest_enabled is True
    assert resp.apollo_enabled is False
    assert resp.engines.apollo["enabled"] is False
    assert resp.engines.artemis["enabled"] is True


# ── read-only contract ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compression_status_is_read_only(sqlite_backend, monkeypatch):
    """Only SELECTs reach the DB; no write verb appears in any statement.

    The SQLite repository helpers are module-level globals in
    ``mnemos.persistence.sqlite`` — patching them intercepts every
    statement the bundle issues, regardless of how the repository
    reaches the connection.
    """
    from mnemos.persistence import sqlite as sq

    seen: list[str] = []
    # Data-mutation verbs. Transaction control statements (BEGIN
    # IMMEDIATE / COMMIT / ROLLBACK) are part of the read-snapshot
    # machinery the SQLite backend uses inside ``transactional()`` and
    # are allowed: the contract is that no DATA statement mutates
    # anything, not that the transaction machinery is suppressed.
    write_verbs = (
        "insert ", "update ", "delete ", "create table", "drop ",
        "replace ", "alter ",
    )

    def _spy(fn):
        async def wrapper(conn, sql, params=()):
            seen.append(" ".join(sql.split()).upper())
            return await fn(conn, sql, params)

        return wrapper

    monkeypatch.setattr(sq, "_execute", _spy(sq._execute))
    monkeypatch.setattr(sq, "_fetch_val", _spy(sq._fetch_val))
    monkeypatch.setattr(sq, "_fetch_one", _spy(sq._fetch_one))
    monkeypatch.setattr(sq, "_fetch_all", _spy(sq._fetch_all))

    resp = await compression_status(_=_root())
    assert resp.queue.reason is None

    assert seen, "expected at least one DB statement (two aggregate reads)"
    for sql in seen:
        lowered = " ".join(sql.lower().split())
        assert not any(v in lowered for v in write_verbs), (
            f"mutating statement issued: {sql}"
        )
    # The two aggregate reads themselves must be SELECTs.
    selects = [s for s in seen if s.startswith("SELECT")]
    assert len(selects) >= 2, f"expected the two aggregate reads, saw {seen}"
    assert any("memory_compression_queue" in s.lower() for s in selects)
    assert any("memory_compressed_variants" in s.lower() for s in selects)


@pytest.mark.asyncio
async def test_compression_status_never_calls_enqueue_routes(sqlite_backend, monkeypatch):
    """Explicit: the two mutating ABC methods are never invoked."""
    repo = sqlite_backend.compression_queue
    called = []

    original_enqueue = repo.enqueue_compression
    original_enqueue_all = repo.enqueue_all_compression

    async def _boom_enqueue(tx, **kwargs):
        called.append("enqueue_compression")
        return await original_enqueue(tx, **kwargs)

    async def _boom_enqueue_all(tx, **kwargs):
        called.append("enqueue_all_compression")
        return await original_enqueue_all(tx, **kwargs)

    monkeypatch.setattr(repo, "enqueue_compression", _boom_enqueue)
    monkeypatch.setattr(repo, "enqueue_all_compression", _boom_enqueue_all)

    resp = await compression_status(_=_root())
    assert resp.queue.reason is None
    assert called == [], f"route invoked mutating methods: {called}"


# ── unavailable backend / unsuppliable fields ──────────────────────────────


@pytest.mark.asyncio
async def test_compression_status_absent_backend_503s(monkeypatch):
    """No persistence backend -> the shared 503 propagates.

    An absent persistence backend is a server misconfiguration the
    operator needs to see, not a silent "null queue with reason" that
    would fabricate a healthy-looking response. The 503 from
    ``backend_or_503`` is the same shape every other admin route uses.
    """
    from fastapi import HTTPException

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    with pytest.raises(HTTPException) as exc_info:
        await compression_status(_=_root())
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_compression_status_absent_variants_table_is_null_not_zero(
    tmp_path, monkeypatch
):
    """A backend that cannot supply the variant count (``-1`` sentinel)
    yields ``null`` + reason — never a fabricated zero."""
    from types import SimpleNamespace

    from mnemos.persistence.sqlite import SqliteBackend, _execute

    backend = SqliteBackend(tmp_path / "no_variants.sqlite3", SimpleNamespace())
    await backend.open()
    old_backend = lifecycle._persistence_backend
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    try:
        # The fresh SqliteBackend schema DOES create the variants table,
        # so drop it to simulate the pre-v3.1 layout.
        async with backend.transactional() as tx:
            await _execute(tx.conn, "DROP TABLE memory_compressed_variants")

        # The ABC method must report inability, not a fake zero.
        async with backend.transactional() as tx:
            count = await backend.compression_queue.get_compressed_variants_count(tx)
        assert count == -1

        resp = await compression_status(_=_root())
    finally:
        lifecycle._persistence_backend = old_backend
        await backend.close()

    assert resp.variant_count is None
    assert resp.variant_count_reason is not None
    assert "memory_compressed_variants" in resp.variant_count_reason
    # The queue snapshot is a monitoring depth read with a separate
    # contract from the ABC count: every field stays a non-negative
    # int even when the variants table is absent (a missing table is
    # indistinguishable from an empty one for the distillation worker
    # that logs these numbers), so the ``-1`` absence sentinel must
    # NOT leak into the snapshot's ``variants`` field.
    assert resp.queue.stats is not None
    assert resp.queue.stats["variants"] == 0


@pytest.mark.asyncio
async def test_compression_status_reports_apollo_installed(
    sqlite_backend, monkeypatch
):
    """Installed state is independent of the contest gates: apollo
    installed=True stays enabled=False while the contest is off."""
    _with_effective_settings(monkeypatch, contest_enabled=False)
    resp = await compression_status(_=_root())
    assert resp.engines.apollo["installed"] is True
    assert resp.engines.apollo["enabled"] is False


@pytest.mark.asyncio
async def test_compression_status_bundle_error_is_null_with_reason(
    monkeypatch
):
    """If the ABC bundle raises (e.g. driver down), both DB fields fall
    back to null + reason; config/engines still report."""
    from tests._fake_backend import install_fake_backend

    fake = install_fake_backend(monkeypatch)
    fake.compression_queue.configure_raise(
        "get_compression_queue_status", RuntimeError("driver down")
    )

    resp = await compression_status(_=_root())
    assert resp.queue.stats is None
    assert "driver down" in resp.queue.reason
    assert resp.variant_count is None
    assert resp.variant_count_reason == resp.queue.reason


# ── engines installed reporting ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compression_status_reports_missing_engine(
    sqlite_backend, monkeypatch
):
    """An engine module that fails to import reports installed=False
    with a reason — not an exception, not a fake installed=True."""
    import builtins
    import sys

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name.startswith("mnemos.domain.compression.artemis"):
            raise ImportError("simulated missing extra deps")
        return real_import(name, *args, **kwargs)

    # Block the module import AND drop the cached module so the probe
    # genuinely re-imports (and sees the block). The block is
    # restored *before the test body returns* — pytest's own teardown
    # (asyncio warnings filters, tmp-dir reporting) performs imports,
    # so a ``monkeypatch``-managed restore would run too late and
    # crash the session.
    builtins.__import__ = _blocking_import
    for mod in list(sys.modules):
        if mod.startswith("mnemos.domain.compression.artemis"):
            del sys.modules[mod]

    try:
        resp = await compression_status(_=_root())
    finally:
        # Restore the real builtin and refill the module cache so the
        # route raised or not, the session sees a healthy artemis
        # module after this test.
        builtins.__import__ = real_import
        real_import("mnemos.domain.compression.artemis", fromlist=["x"])

    # The probe result comes from the import-time block above, not from
    # the module cache: the route reports artemis missing.
    assert resp.engines.artemis["installed"] is False
    assert resp.engines.artemis["enabled"] is False
    assert resp.engines.artemis_reason is not None
    # Apollo is unaffected.
    assert resp.engines.apollo["installed"] is True
    assert resp.engines.apollo_reason is None


# ── fake-backend fault injection: absence sentinel isolation ────────────────


@pytest.mark.asyncio
async def test_compression_status_absent_variants_count_keeps_queue_snapshot(
    monkeypatch,
):
    """When the ABC count reports the ``-1`` absence sentinel, the
    route's ``variant_count`` field becomes null + reason, but the
    queue snapshot is untouched: the ``-1`` MUST NOT leak into the
    snapshot's ``variants`` field.

    Pinned through the fake backend so the test holds on every
    backend shape (the route is backend-neutral): a backend whose
    ``get_compressed_variants_count`` reports inability and whose
    ``get_queue_stats`` is fully available.
    """
    from tests._fake_backend import install_fake_backend

    fake = install_fake_backend(monkeypatch)
    fake.compression_queue.configure_return("get_compressed_variants_count", -1)

    resp = await compression_status(_=_root())

    assert resp.variant_count is None
    assert resp.variant_count_reason is not None
    assert "memory_compressed_variants" in resp.variant_count_reason
    # The snapshot itself is a real measurement and stays intact.
    assert resp.queue.stats == {
        "total": 0,
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "variants": 0,
    }
    assert resp.queue.reason is None


@pytest.mark.asyncio
async def test_fake_compression_queue_status_honours_count_faults(
    monkeypatch,
):
    """The fake ``get_compression_queue_status`` must route its
    ``variant_count`` through the same fault-injection path as
    ``get_compressed_variants_count``: a configured return or raise on
    the count method must propagate into the bundle, not be bypassed
    by a direct dict read. (Guard against a fake that drifts from the
    ABC default implementation.)"""
    from tests._fake_backend import install_fake_backend

    fake = install_fake_backend(monkeypatch)

    # Configured return propagates.
    fake.compression_queue.configure_return("get_compressed_variants_count", 42)
    async with fake.transactional() as tx:
        bundle = await fake.compression_queue.get_compression_queue_status(tx)
    assert bundle["variant_count"] == 42

    # Configured raise propagates too (not swallowed by a default).
    fake.compression_queue.configure_raise(
        "get_compressed_variants_count", RuntimeError("count fault")
    )
    with pytest.raises(RuntimeError, match="count fault"):
        async with fake.transactional() as tx:
            await fake.compression_queue.get_compression_queue_status(tx)


# ── infra-error propagation (not swallowed as "missing table") ─────────────


@pytest.mark.asyncio
async def test_compressed_variants_count_infra_error_propagates(
    sqlite_backend, monkeypatch,
):
    """A non-table-not-found error (e.g. lock timeout, connection loss)
    must propagate from ``get_compressed_variants_count`` — NOT be
    silently mapped to the -1 sentinel. The -1 sentinel is reserved
    for the specific "table absent" condition; masking a dead DB as
    "missing table" would hide a genuine infrastructure failure from
    the operator.

    Pinned on SQLite: inject a ``sqlite3.OperationalError`` with a
    "database is locked" message (the same driver error type a
    genuine lock timeout raises, but a message that does NOT match
    "no such table"). The helper must re-raise it, not swallow it
    into the -1 sentinel.
    """
    import sqlite3

    from mnemos.persistence import sqlite as sq

    async def _boom_fetch_val(conn, sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sq, "_fetch_val", _boom_fetch_val)

    repo = sqlite_backend.compression_queue
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        async with sqlite_backend.transactional() as tx:
            await repo.get_compressed_variants_count(tx)


@pytest.mark.asyncio
async def test_compressed_variants_count_non_operational_error_propagates(
    sqlite_backend, monkeypatch,
):
    """A non-OperationalError exception (e.g. a driver bug, a
    programming error) must also propagate — the helper only catches
    ``sqlite3.OperationalError`` specifically, not every Exception.
    A broad ``except Exception`` would mask genuine bugs as
    "missing table".
    """
    from mnemos.persistence import sqlite as sq

    async def _boom_fetch_val(conn, sql, params=()):
        raise ValueError("something unexpected")

    monkeypatch.setattr(sq, "_fetch_val", _boom_fetch_val)

    repo = sqlite_backend.compression_queue
    with pytest.raises(ValueError, match="something unexpected"):
        async with sqlite_backend.transactional() as tx:
            await repo.get_compressed_variants_count(tx)


@pytest.mark.asyncio
async def test_compressed_variants_count_missing_table_returns_sentinel(
    tmp_path, monkeypatch,
):
    """The specific "no such table" condition maps to -1 (the absence
    sentinel) on SQLite — the complement of the infra-error-propagation
    test above. Pinned with a real DROP TABLE, not a monkeypatch, so
    the actual sqlite3 error path is exercised."""
    from mnemos.persistence.sqlite import SqliteBackend, _execute

    backend = SqliteBackend(tmp_path / "sentinel.sqlite3", SimpleNamespace())
    await backend.open()
    old_backend = lifecycle._persistence_backend
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    try:
        async with backend.transactional() as tx:
            await _execute(tx.conn, "DROP TABLE memory_compressed_variants")

        async with backend.transactional() as tx:
            count = await backend.compression_queue.get_compressed_variants_count(tx)
        assert count == -1
    finally:
        lifecycle._persistence_backend = old_backend
        await backend.close()


# ── MySQL/MariaDB missing-table detection ──────────────────────────────────


def _mysql_conn(*, exc=None, row=None):
    """Minimal aiomysql-shaped connection for ``_count_compressed_variants``."""

    class _Cursor:
        description = [("variants",)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, sql, params=None):
            if exc is not None:
                raise exc

        async def fetchone(self):
            return row

    class _Conn:
        def cursor(self):
            return _Cursor()

    return _Conn()


async def _mysql_count(conn):
    from mnemos.persistence.mysql import MysqlCompressionQueueRepository

    # The helper does not use ``self``; call it unbound so no pool is needed.
    return await MysqlCompressionQueueRepository._count_compressed_variants(None, conn)


@pytest.mark.asyncio
async def test_mysql_variants_count_missing_table_errno_returns_sentinel():
    exc = Exception(1146, "Table 'mnemos.memory_compressed_variants' doesn't exist")
    assert await _mysql_count(_mysql_conn(exc=exc)) == -1


@pytest.mark.asyncio
async def test_mysql_variants_count_other_errno_with_doesnt_exist_text_propagates():
    """Only errno 1146 means a missing table; a different server error whose
    message happens to contain "doesn't exist" must not become the sentinel."""
    exc = Exception(1054, "Column 'x' doesn't exist in field list")
    with pytest.raises(Exception, match="doesn't exist in field list"):
        await _mysql_count(_mysql_conn(exc=exc))


@pytest.mark.asyncio
async def test_mysql_variants_count_connection_loss_propagates():
    exc = Exception(2013, "Lost connection to MySQL server during query")
    with pytest.raises(Exception, match="Lost connection"):
        await _mysql_count(_mysql_conn(exc=exc))


@pytest.mark.asyncio
async def test_mysql_variants_count_error_without_errno_propagates():
    """No numeric errno -> not provably a missing table -> propagate, even if
    the text says "doesn't exist" or contains "1146"."""
    with pytest.raises(RuntimeError):
        await _mysql_count(_mysql_conn(exc=RuntimeError("table doesn't exist (1146)")))


@pytest.mark.asyncio
async def test_mysql_variants_count_returns_count():
    assert await _mysql_count(_mysql_conn(row=(7,))) == 7
