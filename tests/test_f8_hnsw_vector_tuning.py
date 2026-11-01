"""F-8: pgvector HNSW tuning — ``ef_search``, ``m``/``ef_construction``, iterative scan.

Source: Fable federation report (2026-10-31, commit 0073bb4), F-8 PERF vec:

    "F-8 PERF vec: ``SET LOCAL hnsw.ef_search = GREATEST(limit*4, 100)``
    per semantic query, declare ``m``/``ef_construction``, iterative scan
    where pgvector >= 0.8; HNSW is created with defaults
    (``schema.py:507``, ``migrations.sql:67``) and ``ef_search`` is never
    set, so each query sees 40 candidates which the namespace/owner/
    category WHERE then post-filters (``postgres.py:1588-1592``); Oracle
    has no vector index DDL, every search is an exact scan
    (``oracle.py:2326-2337``) (S)."

Re-verified against master 68d5891a (48 commits newer than the
report's 0073bb4). What the report said and what the tree actually
looked like:

* "``ef_search`` is never set" — CONFIRMED. ``semantic_search`` went
  straight from the dim guard to the ``SELECT``; no ``hnsw.*`` GUC was
  set anywhere in the repository (the only ``SET hnsw.ef_search`` in
  the tree was in ``scripts/bench_v4.py``, a benchmark).
* "HNSW is created with defaults" — CONFIRMED. Three separate DDL sites
  created the index with no options: the legacy PG bootstrap
  (``mnemos/db_migrations/migrations.sql``), the runtime shape repair
  (``mnemos/persistence/schema.py``) and the installer
  (``mnemos/installer/db.py``).
* "Oracle has no vector index DDL, every search is an exact scan" —
  CONFIRMED. ``mnemos/persistence/oracle.py`` has no vector index DDL
  at all; the semantic search orders by bare ``VECTOR_DISTANCE(...)``.

These tests are SQLite-safe: they drive the Postgres repository against
a fake asyncpg-shaped connection (the pattern
``tests/test_postgres_fetch_memory_context_sql.py`` uses), so no live
Postgres is needed and the SQLite backend is untouched.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.persistence.postgres import (
    PostgresMemoryRepository,
    PostgresTransaction,
)
from mnemos.persistence.visibility import VisibilityFilter
from mnemos.persistence.vector_tuning import (
    HNSW_EF_SEARCH_FLOOR,
    HNSW_EF_SEARCH_MULTIPLIER,
    HNSW_INDEX_EF_CONSTRUCTION,
    HNSW_INDEX_M,
    HNSW_INDEX_OPTIONS_SQL,
    ORACLE_HAS_VECTOR_INDEX_DDL,
    hnsw_ef_search_for,
    pgvector_version_at_least,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _CapturingConn:
    """asyncpg-shaped connection that records every statement."""

    def __init__(self, *, pgvector_version: str | None = "0.8.0") -> None:
        self.pgvector_version = pgvector_version
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((" ".join(sql.split()), args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list:
        self.calls.append((" ".join(sql.split()), args))
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((" ".join(query.split()), args))
        if "pg_extension" in query and "extname = 'vector'" in query:
            return self.pgvector_version
        return None


def _tx(conn: _CapturingConn) -> PostgresTransaction:
    return PostgresTransaction(conn, MagicMock())


def _root() -> UserContext:
    return UserContext(user_id="root", group_ids=[], role="root", namespace="root-ns", authenticated=True)


def _visibility() -> VisibilityFilter:
    """A root read filter — the shape ``semantic_search`` expects."""
    return VisibilityFilter.for_read(_root(), namespace=None)


def _set_config_calls(conn: _CapturingConn) -> list[tuple[str, tuple]]:
    return [c for c in conn.calls if "set_config" in c[0]]


# ── The tuning arithmetic ──────────────────────────────────────────────────


def test_ef_search_is_greatest_of_limit_times_four_and_one_hundred() -> None:
    """``GREATEST(limit*4, 100)`` — the exact formula the report names."""
    assert hnsw_ef_search_for(1) == 100
    assert hnsw_ef_search_for(10) == 100
    assert hnsw_ef_search_for(25) == 100
    # limit*4 crosses the floor at limit=25.
    assert hnsw_ef_search_for(26) == 104
    assert hnsw_ef_search_for(50) == 200
    assert hnsw_ef_search_for(100) == 400


def test_ef_search_never_falls_below_the_floor_for_degenerate_limits() -> None:
    """A zero/negative/garbage limit must not produce a zero ``ef_search``.

    ``hnsw.ef_search = 0`` would make the ANN scan return nothing, so
    a degenerate limit falls back to the floor (the safe direction:
    a wider candidate window never returns fewer rows than requested).
    """
    assert hnsw_ef_search_for(0) == HNSW_EF_SEARCH_FLOOR
    assert hnsw_ef_search_for(-5) == HNSW_EF_SEARCH_FLOOR
    assert hnsw_ef_search_for(None) == HNSW_EF_SEARCH_FLOOR
    assert hnsw_ef_search_for("not a number") == HNSW_EF_SEARCH_FLOOR


def test_ef_search_multiplier_is_four() -> None:
    """Pin the multiplier so a future change is deliberate."""
    assert HNSW_EF_SEARCH_MULTIPLIER == 4


# ── The pgvector version probe ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.8.0", True),
        ("0.8", True),
        ("0.9.1", True),
        ("1.0.0", True),
        ("0.8.0-pg13", True),
        ("0.7.4", False),
        ("0.7", False),
        ("0.6.1", False),
        ("0.5.0", False),
        (None, False),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_iterative_scan_is_gated_on_pgvector_080_or_newer(version, expected) -> None:
    """``hnsw.iterative_scan`` only exists in pgvector >= 0.8.

    Setting the GUC on an older extension raises
    ``unrecognized configuration parameter``, which would turn every
    semantic search into a 500. The version probe is what makes the
    knob safe to set.
    """
    assert pgvector_version_at_least(version, (0, 8)) is expected


# ── The query path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_search_sets_ef_search_per_query() -> None:
    """Every semantic query sets ``hnsw.ef_search`` for that query.

    The value is transaction-local (``set_config(..., true)``), so it
    does not leak to the next query that reuses a pooled connection.
    """
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=50,
        visibility=_visibility(),
    )

    ef_calls = [c for c in _set_config_calls(conn) if "hnsw.ef_search" in c[0]]
    assert len(ef_calls) == 1, conn.calls
    sql, args = ef_calls[0]
    assert "hnsw.ef_search" in sql
    # GREATEST(50*4, 100) = 200
    assert args == ("200",)


@pytest.mark.asyncio
async def test_ef_search_scales_with_the_requested_limit() -> None:
    """A bigger LIMIT gets a bigger candidate window."""
    for limit, expected in ((1, "100"), (10, "100"), (50, "200"), (100, "400")):
        conn = _CapturingConn()
        repo = PostgresMemoryRepository()
        await repo.semantic_search(
            _tx(conn),
            embedding=[0.1, 0.2, 0.3],
            limit=limit,
            visibility=_visibility(),
        )
        ef_calls = [c for c in _set_config_calls(conn) if "hnsw.ef_search" in c[0]]
        assert len(ef_calls) == 1
        assert ef_calls[0][1] == (expected,), f"limit={limit}"


@pytest.mark.asyncio
async def test_ef_search_is_transaction_local() -> None:
    """The knob must be transaction-local.

    Postgres' ``SET`` syntax does not accept bind parameters, so the
    codebase uses ``SELECT set_config(name, $1, true)`` — the third
    argument ``true`` makes it transaction-scoped, equivalent to
    ``SET LOCAL``. A session-level ``SET`` would leak the value to
    whatever query runs next on that pooled connection.
    """
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    ef_calls = [c for c in _set_config_calls(conn) if "hnsw.ef_search" in c[0]]
    assert ef_calls
    sql = ef_calls[0][0]
    assert "set_config('hnsw.ef_search', $1, true)" in sql


@pytest.mark.asyncio
async def test_iterative_scan_is_enabled_on_pgvector_080_or_newer() -> None:
    """On pgvector >= 0.8 the iterative scan is turned on."""
    conn = _CapturingConn(pgvector_version="0.8.0")
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    iterative = [c for c in _set_config_calls(conn) if "hnsw.iterative_scan" in c[0]]
    assert len(iterative) == 1, conn.calls
    assert iterative[0][1] == ("strict_order",)


@pytest.mark.asyncio
async def test_iterative_scan_is_not_set_on_older_pgvector() -> None:
    """On pgvector < 0.8 the GUC is NOT set.

    Older extensions do not know ``hnsw.iterative_scan`` and raise
    ``unrecognized configuration parameter`` — which would turn every
    semantic search into a 500 on those hosts.
    """
    conn = _CapturingConn(pgvector_version="0.7.4")
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    iterative = [c for c in _set_config_calls(conn) if "hnsw.iterative_scan" in c[0]]
    assert iterative == []
    # ... but ef_search IS still set: that GUC exists in every pgvector
    # release that has HNSW.
    ef_calls = [c for c in _set_config_calls(conn) if "hnsw.ef_search" in c[0]]
    assert len(ef_calls) == 1


@pytest.mark.asyncio
async def test_iterative_scan_is_not_set_when_the_probe_finds_no_extension() -> None:
    """A missing pgvector extension must not blow up the search."""
    conn = _CapturingConn(pgvector_version=None)
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    iterative = [c for c in _set_config_calls(conn) if "hnsw.iterative_scan" in c[0]]
    assert iterative == []


@pytest.mark.asyncio
async def test_the_pgvector_version_is_probed_once_and_cached() -> None:
    """The version probe is a per-install fact — probe it once."""
    conn = _CapturingConn(pgvector_version="0.8.0")
    repo = PostgresMemoryRepository()

    for _ in range(3):
        await repo.semantic_search(
            _tx(conn),
            embedding=[0.1, 0.2, 0.3],
            limit=10,
            visibility=_visibility(),
        )

    probes = [c for c in conn.calls if "pg_extension" in c[0]]
    assert len(probes) == 1, conn.calls


@pytest.mark.asyncio
async def test_a_failing_version_probe_does_not_fail_the_search() -> None:
    """A broken probe degrades to "no iterative scan", never to a 500."""

    class _BrokenProbeConn(_CapturingConn):
        async def fetchval(self, query: str, *args: Any) -> Any:
            if "pg_extension" in query:
                raise RuntimeError("probe exploded")
            return await super().fetchval(query, *args)

    conn = _BrokenProbeConn()
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    # ef_search was still applied; the search itself did not raise.
    ef_calls = [c for c in _set_config_calls(conn) if "hnsw.ef_search" in c[0]]
    assert len(ef_calls) == 1
    iterative = [c for c in _set_config_calls(conn) if "hnsw.iterative_scan" in c[0]]
    assert iterative == []


@pytest.mark.asyncio
async def test_a_failing_set_config_does_not_fail_the_search() -> None:
    """Tuning is an optimisation, never a correctness requirement."""

    class _BrokenSetConfigConn(_CapturingConn):
        async def execute(self, sql: str, *args: Any) -> str:
            if "set_config" in sql:
                raise RuntimeError("set_config exploded")
            return await super().execute(sql, *args)

    conn = _BrokenSetConfigConn()
    repo = PostgresMemoryRepository()

    await repo.semantic_search(
        _tx(conn),
        embedding=[0.1, 0.2, 0.3],
        limit=10,
        visibility=_visibility(),
    )

    # The SELECT still ran.
    selects = [c for c in conn.calls if "ORDER BY embedding" in c[0]]
    assert len(selects) == 1


# ── The index DDL ──────────────────────────────────────────────────────────


def test_hnsw_index_ddl_declares_m_and_ef_construction() -> None:
    """The shipped PG bootstrap migration declares the build parameters.

    pgvector's defaults are ``m=16`` and ``ef_construction=64``; naming
    them makes the index reproducible across standups instead of
    recording whatever the extension defaults were on build day.
    """
    sql = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations.sql").read_text()
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert f"m = {HNSW_INDEX_M}" in sql
    assert f"ef_construction = {HNSW_INDEX_EF_CONSTRUCTION}" in sql


def test_hnsw_index_options_clause_is_shared_by_every_ddl_site() -> None:
    """All three HNSW DDL sites declare the SAME options.

    Three separate sites create the index: the legacy PG bootstrap
    migration, the runtime shape repair in ``mnemos/persistence/schema.py``
    and the installer's rebuild. They must not drift.
    """
    expected = f"WITH (m = {HNSW_INDEX_M}, ef_construction = {HNSW_INDEX_EF_CONSTRUCTION})"

    bootstrap = (REPO_ROOT / "mnemos" / "db_migrations" / "migrations.sql").read_text()
    assert expected in bootstrap

    schema_src = (REPO_ROOT / "mnemos" / "persistence" / "schema.py").read_text()
    assert "hnsw_index_options_fragment" in schema_src

    installer_src = (REPO_ROOT / "mnemos" / "installer" / "db.py").read_text()
    assert "hnsw_index_options_fragment" in installer_src

    # The shared clause itself is the canonical string.
    assert HNSW_INDEX_OPTIONS_SQL == expected


def test_the_ivfflat_to_hnsw_rewrite_keeps_the_index_options() -> None:
    """The legacy ivfflat→hnsw rewrite must carry the options too.

    Otherwise a rewritten index would silently fall back to the
    extension defaults — exactly what F-8 sets out to avoid.
    """
    from mnemos.persistence.schema import render_migration_sql

    path = REPO_ROOT / "mnemos" / "db_migrations" / "migrations.sql"
    rendered = render_migration_sql(path, backend="postgres", embedding_dim=1024)
    assert "ivfflat" not in rendered.lower()
    assert HNSW_INDEX_OPTIONS_SQL in rendered


def test_the_hnsw_build_parameters_are_the_documented_values() -> None:
    """Pin m / ef_construction so a bump is a deliberate change."""
    assert HNSW_INDEX_M == 16
    assert HNSW_INDEX_EF_CONSTRUCTION == 64


# ── DDL spacing ────────────────────────────────────────────────────────────
#
# ``HNSW_INDEX_OPTIONS_SQL`` is the BARE clause ``WITH (m = 16,
# ef_construction = 64)``. A DDL site that concatenates it straight
# after ``vector_cosine_ops)`` produces ``vector_cosine_ops)WITH (...)``
# -- invalid Postgres syntax that fails the index build at install
# time with a confusing error, long after the change was merged.
#
# ``hnsw_index_options_fragment()`` returns the clause with the
# separating space already applied, so a call site cannot get it
# wrong. These tests render every DDL site and assert the emitted SQL
# is syntactically well-formed.


def test_the_options_fragment_carries_its_separating_space() -> None:
    """The fragment must start with exactly one space."""
    from mnemos.persistence.vector_tuning import hnsw_index_options_fragment

    fragment = hnsw_index_options_fragment()
    assert fragment.startswith(" "), repr(fragment)
    assert fragment == " " + HNSW_INDEX_OPTIONS_SQL
    assert not fragment.startswith("  "), repr(fragment)


def test_every_hnsw_ddl_site_emits_valid_spacing() -> None:
    """Render each DDL site and assert the spacing is correct.

    Covers the three dynamic sites (the installer's index rebuild, the
    runtime shape repair and the legacy ivfflat -> hnsw rewrite) plus
    the hand-written bootstrap migration.
    """
    import asyncio
    from pathlib import Path

    from mnemos.persistence.schema import _ensure_postgres_embedding_shape, render_migration_sql
    from mnemos.persistence.vector_tuning import hnsw_index_options_fragment

    repo_root = Path(__file__).resolve().parents[1]
    fragment = hnsw_index_options_fragment()

    def assert_valid(label: str, sql: str) -> None:
        assert ") WITH" in sql, f"{label}: missing space before WITH -> {sql}"
        assert ")WITH" not in sql, f"{label}: ')' directly followed by WITH -> {sql}"

    # Site 1: the installer's rebuild statement.
    installer_sql = (
        "DROP INDEX IF EXISTS idx_memories_embedding; "
        "CREATE INDEX IF NOT EXISTS idx_memories_embedding "
        f"ON memories USING hnsw (embedding vector_cosine_ops){fragment};"
    )
    assert_valid("installer/db.py", installer_sql)

    # Site 2: the runtime shape repair.
    class _Conn:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def fetchval(self, query: str, *args: object) -> object:
            if "format_type" in query:
                return "vector(1024)"
            if "am.amname" in query:
                return "ivfflat"  # force the repair branch
            if "COUNT(*)" in query:
                return 0
            return None

        async def execute(self, statement: str, *args: object) -> str:
            self.statements.append(statement)
            return "OK"

    conn = _Conn()
    asyncio.run(_ensure_postgres_embedding_shape(conn, 1024))
    creates = [s for s in conn.statements if "CREATE INDEX" in s]
    assert creates, conn.statements
    for statement in creates:
        assert_valid("persistence/schema.py", statement)

    # Site 3: the legacy ivfflat -> hnsw rewrite.
    rendered = render_migration_sql(
        repo_root / "mnemos" / "db_migrations" / "migrations.sql",
        backend="postgres",
        embedding_dim=1024,
    )
    hnsw_lines = [line for line in rendered.splitlines() if "USING hnsw" in line]
    assert hnsw_lines, "the rewritten migration must contain an HNSW index"
    for line in hnsw_lines:
        assert_valid("db_migrations/migrations.sql", line)

    # Site 4: the hand-written bootstrap migration.
    bootstrap = (repo_root / "mnemos" / "db_migrations" / "migrations.sql").read_text(encoding="utf-8")
    for line in bootstrap.splitlines():
        if "USING hnsw" in line:
            assert_valid("db_migrations/migrations.sql", line)


def test_no_hnsw_ddl_site_concatenates_the_bare_clause_without_a_space() -> None:
    """No source file may build HNSW DDL by hand-concatenating the
    bare ``HNSW_INDEX_OPTIONS_SQL`` constant.

    The constant has no leading space by design, so a site that
    interpolates it directly is one edit away from emitting
    ``vector_cosine_ops)WITH (...)``. Sites must use
    ``hnsw_index_options_fragment()``, which owns the separator.
    """
    import re

    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in sorted((repo_root / "mnemos").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"vector_cosine_ops\)\s*\{?([A-Za-z_][A-Za-z0-9_]*)", text):
            if match.group(1) == "HNSW_INDEX_OPTIONS_SQL":
                offenders.append(f"{path.relative_to(repo_root)}: {match.group(0)}")
        # Also catch the f-string form with a literal space before the
        # constant -- same hazard, just already spaced by hand.
        for match in re.finditer(r"vector_cosine_ops\)\s+\{HNSW_INDEX_OPTIONS_SQL\}", text):
            offenders.append(f"{path.relative_to(repo_root)}: {match.group(0)}")

    assert not offenders, (
        "HNSW DDL must use hnsw_index_options_fragment(); the bare constant "
        "has no leading space:\n" + "\n".join(offenders)
    )


# ── Oracle ─────────────────────────────────────────────────────────────────


def test_oracle_has_no_vector_index_ddl() -> None:
    """Oracle has no vector index DDL — every search is an exact scan.

    Recorded as a fact (not just the absence of code) so the gap is
    visible to anyone reading the tuning module.
    """
    assert ORACLE_HAS_VECTOR_INDEX_DDL is False


def test_the_oracle_backend_does_not_use_the_pgvector_hnsw_knobs() -> None:
    """The Oracle semantic search must not try to set pgvector GUCs."""
    from mnemos.persistence import oracle

    src = inspect.getsource(oracle.OracleMemoryRepository.semantic_search)
    assert "hnsw.ef_search" not in src
    assert "hnsw.iterative_scan" not in src
    assert "set_config" not in src


def test_the_oracle_semantic_search_documents_the_exact_scan_gap() -> None:
    """The Oracle search path must say why the tuning does not apply."""
    from mnemos.persistence import oracle

    src = inspect.getsource(oracle.OracleMemoryRepository.semantic_search)
    assert "F-8" in src
    assert "no vector index DDL" in src


# ── Other backends must be untouched ───────────────────────────────────────


def test_the_other_backends_do_not_emit_pgvector_knobs() -> None:
    """SQLite / Oracle / MySQL / MariaDB / Db2 must not set pgvector GUCs.

    The F-8 change is Postgres-only: ``hnsw.*`` is a pgvector GUC and
    the other backends have no pgvector (``supports_pgvector = False``).
    """
    for module_name in ("sqlite", "oracle", "mysql", "mariadb", "db2"):
        src = (REPO_ROOT / "mnemos" / "persistence" / f"{module_name}.py").read_text()
        assert "hnsw.ef_search" not in src, module_name
        assert "hnsw.iterative_scan" not in src, module_name


# ── The DDL sites must have the constant in scope ──────────────────────────
#
# Every HNSW DDL site interpolates ``HNSW_INDEX_OPTIONS_SQL``. A module
# that references the constant without importing it raises ``NameError``
# at the moment the DDL is built -- which for the installer means the
# Postgres index rebuild fails, and for the schema module means the
# migration render or the runtime index repair fails. These assertions
# pin the import so the failure surfaces in the test suite rather than
# during database initialisation.


def test_the_schema_module_has_the_hnsw_options_constant_in_scope() -> None:
    """``mnemos/persistence/schema.py`` must import the options helper.

    It is referenced by ``render_migration_sql`` (the ivfflat -> hnsw
    rewrite) and by ``_ensure_postgres_embedding_shape`` (the runtime
    index repair). Both are exercised at database-initialisation time,
    so a missing import is a hard startup failure.
    """
    from mnemos.persistence import schema

    assert hasattr(schema, "hnsw_index_options_fragment"), (
        "mnemos/persistence/schema.py must import hnsw_index_options_fragment from mnemos.persistence.vector_tuning."
    )
    assert "m = 16" in schema.hnsw_index_options_fragment()
    assert "ef_construction = 64" in schema.hnsw_index_options_fragment()


def test_the_installer_module_has_the_hnsw_options_constant_in_scope() -> None:
    """``mnemos/installer/db.py`` must import the options helper.

    The installer's HNSW index rebuild interpolates it into the psql
    command; a missing import makes the rebuild fail at install time.
    """
    from mnemos.installer import db as installer_db

    assert hasattr(installer_db, "hnsw_index_options_fragment"), (
        "mnemos/installer/db.py must import hnsw_index_options_fragment from mnemos.persistence.vector_tuning."
    )


def test_the_postgres_module_has_the_vector_tuning_helpers_in_scope() -> None:
    """``mnemos/persistence/postgres.py`` must import the helpers.

    ``semantic_search`` calls ``hnsw_ef_search_for`` and
    ``pgvector_version_at_least`` on every query; a missing import is a
    ``NameError`` that turns every semantic search into a 500.
    """
    from mnemos.persistence import postgres

    for name in ("hnsw_ef_search_for", "pgvector_version_at_least", "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION"):
        assert hasattr(postgres, name), (
            f"mnemos/persistence/postgres.py must import {name} from mnemos.persistence.vector_tuning."
        )


def test_the_hnsw_tuning_helper_resolves_its_names_at_runtime() -> None:
    """``_apply_hnsw_query_tuning`` must resolve all three names.

    Checking ``hasattr(postgres, name)`` proves the names are bound in
    the module namespace, but not that the function that USES them can
    reach them. This calls the helper for real against a fake
    connection and asserts the two transaction-local ``set_config``
    statements are emitted -- which can only happen if every name in
    the body resolved.

    A missing import here is not a static-analysis nit: the helper runs
    on EVERY semantic search, so an unresolved name turns every search
    into a 500.
    """
    import asyncio

    from mnemos.persistence import postgres

    class _Conn:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, sql: str, *args: object) -> str:
            self.calls.append(" ".join(sql.split()))
            return "OK"

        async def fetchval(self, query: str, *args: object) -> object:
            return "0.8.0" if "pg_extension" in query else None

    conn = _Conn()
    asyncio.run(postgres._apply_hnsw_query_tuning(conn, limit=10, pgvector_version="0.8.0"))

    assert any("hnsw.ef_search" in call for call in conn.calls), conn.calls
    assert any("hnsw.iterative_scan" in call for call in conn.calls), conn.calls


def test_the_iterative_scan_constant_is_defined_and_exported() -> None:
    """``PGVECTOR_ITERATIVE_SCAN_MIN_VERSION`` must exist and be usable.

    The constant is the gate for ``hnsw.iterative_scan``: the GUC only
    exists in pgvector >= 0.8, and setting it on an older extension
    raises ``unrecognized configuration parameter``. Both the Postgres
    module and the version comparison in the tuning module read it, so
    it must be defined once and exported.
    """
    from mnemos.persistence import postgres, vector_tuning

    assert vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION == (0, 8)
    assert "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION" in vector_tuning.__all__
    assert postgres.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION is vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION

    # The comparison helper must accept the constant's own value.
    assert vector_tuning.pgvector_version_at_least("0.8.0", vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION)
    assert not vector_tuning.pgvector_version_at_least("0.7.4", vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION)

    # The constant must be usable as a MINIMUM argument, i.e. it has to be
    # comparable against the parsed integer version components. A string
    # tuple ('0', '8') would satisfy a bare equality check but raise
    # TypeError inside the comparison on every real version string.
    assert all(isinstance(part, int) for part in vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION), (
        "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION must be an int tuple: "
        "pgvector_version_at_least compares it against parsed integer "
        "version components, and a string tuple raises TypeError."
    )

    # Both import paths must work -- a test may reach for either module.
    from mnemos.persistence.postgres import PGVECTOR_ITERATIVE_SCAN_MIN_VERSION as from_postgres
    from mnemos.persistence.vector_tuning import PGVECTOR_ITERATIVE_SCAN_MIN_VERSION as from_tuning

    assert from_postgres is from_tuning is vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION


def test_the_vector_tuning_all_lists_every_public_name() -> None:
    """``__all__`` must cover every public name the module defines.

    A name that exists but is missing from ``__all__`` still imports
    fine, so the gap is invisible until something checks the export
    surface. Pinning it here means "the constant is not exported"
    cannot become true without a test failing.
    """
    from mnemos.persistence import vector_tuning

    public = {name for name in dir(vector_tuning) if not name.startswith("_")}
    # ``annotations`` is the ``from __future__ import annotations``
    # artefact, not a name this module defines.
    public.discard("annotations")

    assert not (public - set(vector_tuning.__all__)), (
        f"these public names are missing from vector_tuning.__all__: {sorted(public - set(vector_tuning.__all__))}"
    )
    # And nothing in __all__ may be absent from the module.
    assert not (set(vector_tuning.__all__) - public), (
        f"these names are in __all__ but not defined: {sorted(set(vector_tuning.__all__) - public)}"
    )


def test_the_postgres_module_reexports_the_tuning_constants() -> None:
    """``postgres.py`` must expose the tuning constants it imports.

    ``semantic_search`` and ``_apply_hnsw_query_tuning`` read these
    from the module namespace, and the tests reach for them through
    ``postgres`` as well. If the import is dropped the names vanish
    from the module and the search path breaks with a NameError.
    """
    from mnemos.persistence import postgres, vector_tuning

    for name in (
        "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION",
        "hnsw_ef_search_for",
        "pgvector_version_at_least",
    ):
        assert hasattr(postgres, name), (
            f"mnemos/persistence/postgres.py must import {name} from mnemos.persistence.vector_tuning."
        )
        assert getattr(postgres, name) is getattr(vector_tuning, name), (
            f"postgres.{name} must be the SAME object as vector_tuning.{name}, not a copy."
        )


def test_the_test_modules_have_no_forward_references() -> None:
    """No test may reference a module-level name defined further down.

    A name defined after its use is only a problem if something
    evaluates it earlier -- and in a pytest module that is always the
    test body, never the import. Even so, source order that reads
    backwards is a trap for the next editor and produces phantom
    "NameError at import time" review findings. This pins the ordering
    so the file cannot drift back into it.
    """
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for name in (
        "test_f8_hnsw_vector_tuning.py",
        "test_f9_create_memory_embed_outside_transaction.py",
        "test_f10_index_hygiene_and_limit_caps.py",
    ):
        path = repo_root / "tests" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))

        defined: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.setdefault(node.name, node.lineno)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.setdefault(target.id, node.lineno)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Name)
                    and isinstance(sub.ctx, ast.Load)
                    and sub.id in defined
                    and defined[sub.id] > node.lineno
                ):
                    offenders.append(
                        f"{name}: {node.name}() at line {node.lineno} uses '{sub.id}' defined at line {defined[sub.id]}"
                    )

    assert not offenders, "forward references found:\n" + "\n".join(offenders)


def test_the_postgres_semantic_search_path_resolves_its_tuning_helpers() -> None:
    """Execute the tuning path end to end against a fake connection.

    This proves the helpers are not merely imported but actually
    reachable from the query path -- the same code the fleet runs.
    """
    from unittest.mock import MagicMock

    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
    from mnemos.persistence.visibility import VisibilityFilter

    class _Conn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def execute(self, sql: str, *args: object) -> str:
            self.calls.append((" ".join(sql.split()), args))
            return "OK"

        async def fetch(self, sql: str, *args: object) -> list:
            self.calls.append((" ".join(sql.split()), args))
            return []

        async def fetchval(self, query: str, *args: object) -> object:
            self.calls.append((" ".join(query.split()), args))
            if "pg_extension" in query:
                return "0.8.0"
            return None

    import asyncio

    conn = _Conn()
    repo = PostgresMemoryRepository()
    root = UserContext(user_id="root", group_ids=[], role="root", namespace="root-ns", authenticated=True)
    asyncio.run(
        repo.semantic_search(
            PostgresTransaction(conn, MagicMock()),
            embedding=[0.1, 0.2, 0.3],
            limit=10,
            visibility=VisibilityFilter.for_read(root, namespace=None),
        )
    )
    ef = [c for c in conn.calls if "hnsw.ef_search" in c[0]]
    iterative = [c for c in conn.calls if "hnsw.iterative_scan" in c[0]]
    assert len(ef) == 1, conn.calls
    assert ef[0][1] == ("100",)
    assert len(iterative) == 1, conn.calls
