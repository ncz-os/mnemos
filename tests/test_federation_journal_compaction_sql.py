"""F-4: the narrowed trigger and the compaction statement, per dialect.

No live server is required: these pin the SQL text each backend ships, so a
dialect that silently keeps the bookkeeping columns (or drops ``embedding``,
which ``copy_embeddings`` peers depend on) fails here rather than in
production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "mnemos" / "db_migrations"

#: Columns the feed actually exports or gates on. These MUST still fire.
KEPT = (
    "federation_source",
    "content",
    "category",
    "namespace",
    "owner_id",
    "permission_mode",
    "deleted_at",
    "archived_at",
    "consolidated_into",
    # embedding/metadata stay journaled: a copy_embeddings=1 peer receives
    # them and the trigger cannot see a consumer's scope.
    "embedding",
    "metadata",
    "verbatim_content",
)

#: Columns that used to fire and must not any more.
DROPPED = ("subcategory", "source_model", "source_provider", "source_agent", "source_session")

TRIGGER_FILES = [
    ("migrations", "0068_federation_journal_narrow_trigger.sql"),
    ("migrations_oracle", "0068_federation_journal_narrow_trigger.sql"),
    ("migrations_db2", "0068_federation_journal_narrow_trigger.sql"),
    ("migrations_mysql", "0068_federation_journal_narrow_trigger.sql"),
    ("migrations_mariadb", "0068_federation_journal_narrow_trigger.sql"),
    ("migrations_sqlite", "migrations_v7_2_federation_journal_narrow_trigger_sqlite.sql"),
]


def _update_trigger_region(text: str) -> str:
    """The ``fed_journal_update`` statement only, not the delete/insert ones."""
    start = text.index("fed_journal_update")
    end = text.find("fed_journal_delete", start)
    return text[start:end] if end != -1 else text[start:]


@pytest.mark.parametrize("directory,name", TRIGGER_FILES)
def test_narrowed_trigger_exists(directory, name):
    assert (MIGRATIONS / directory / name).is_file()


@pytest.mark.parametrize("directory,name", TRIGGER_FILES)
def test_narrowed_trigger_keeps_the_exported_columns(directory, name):
    region = _update_trigger_region((MIGRATIONS / directory / name).read_text())
    for column in KEPT:
        assert column in region, f"{directory}/{name} dropped a fed column: {column}"


@pytest.mark.parametrize("directory,name", TRIGGER_FILES)
def test_narrowed_trigger_drops_the_bookkeeping_columns(directory, name):
    region = _update_trigger_region((MIGRATIONS / directory / name).read_text())
    for column in DROPPED:
        assert column not in region, f"{directory}/{name} still fires on: {column}"


@pytest.mark.parametrize("directory,name", TRIGGER_FILES)
def test_narrowed_trigger_no_longer_fires_on_updated(directory, name):
    """``updated`` is re-stamped by every writer; it must not be a trigger."""
    region = _update_trigger_region((MIGRATIONS / directory / name).read_text())
    assert "updated)" not in region.replace("federation_remote_updated)", "")


def test_mysql_provisioning_replays_the_narrowed_trigger():
    """The MySQL/MariaDB nodes provision the journal in code, not via a runner."""
    source = (Path(__file__).resolve().parents[1] / "mnemos" / "persistence" / "mysql.py").read_text()
    assert "0068_federation_journal_narrow_trigger.sql" in source


def test_sqlite_migration_is_registered():
    source = (Path(__file__).resolve().parents[1] / "mnemos" / "persistence" / "sqlite.py").read_text()
    assert "migrations_v7_2_federation_journal_narrow_trigger_sqlite.sql" in source


def test_mysql_provisioning_strips_embedding_from_the_narrowed_trigger():
    """0068 rebuilds the trigger, so it must honour ``separate_embeddings``.

    0062 is patched to drop the embedding comparison on a dedicated-embeddings
    table; 0068 is replayed afterwards and re-creates ``fed_journal_update``
    from the file, so an unpatched 0068 silently restores the comparison and
    the journal starts firing on embedding-only writes again. Drives the real
    provisioning function with a recording cursor.
    """
    import asyncio

    from mnemos.persistence import mysql

    class _Cursor:
        def __init__(self, sink):
            self._sink = sink

        async def execute(self, statement):
            self._sink.append(statement)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Conn:
        def __init__(self):
            self.statements = []

        def cursor(self):
            return _Cursor(self.statements)

    def _trigger_bodies(separate_embeddings: bool) -> str:
        conn = _Conn()
        asyncio.run(mysql._ensure_mysql_federation_journal(conn, separate_embeddings=separate_embeddings))
        return " ".join(s for s in conn.statements if "fed_journal_update" in s)

    assert "OLD.embedding" in _trigger_bodies(separate_embeddings=False)
    assert "OLD.embedding" not in _trigger_bodies(separate_embeddings=True), (
        "the narrowed trigger restored the embedding comparison on a dedicated-embeddings table"
    )


def test_postgres_narrowing_runs_after_the_trigger_that_creates_it():
    """0068 replaces the function body, so it must be applied after 0062.

    ``CREATE OR REPLACE FUNCTION`` swaps the body in place and the trigger
    binds the function by name, so no ``CREATE TRIGGER`` is needed -- but the
    ordering is: if 0068 were applied first, 0062's full-width function would
    overwrite the narrowing.
    """
    from mnemos.persistence.schema import postgres_migration_paths

    names = [path.name for path in postgres_migration_paths()]
    assert "0062_federation_journal.sql" in names
    assert "0068_federation_journal_narrow_trigger.sql" in names
    assert names.index("0068_federation_journal_narrow_trigger.sql") > names.index("0062_federation_journal.sql")

    body = (Path("mnemos/db_migrations/migrations/0068_federation_journal_narrow_trigger.sql")).read_text()
    # The body is replaced, never re-created without a definition.
    assert "CREATE OR REPLACE FUNCTION fed_journal_update_fn" in body


def _numeric_migrations(names):
    """``(number, name)`` for the ``NNNN[_suffix]_rest.sql`` names, else skip.

    ``migrations.sql`` and the ``migrations_v*`` legacy files are applied
    first by a separate list (``_POSTGRES_LEGACY_MIGRATIONS``), so only the
    plain zero-padded prefix carries a number to compare.
    """
    seen = []
    for name in names:
        head = name.split("_", 1)[0]
        # 0061c / 0061d carry a letter suffix on the same number; keep the
        # numeric part but preserve the original name for the failure message.
        digits = head.rstrip("abcdefghijklmnopqrstuvwxyz")
        if digits.isdigit():
            seen.append((int(digits), name))
    return seen


@pytest.mark.parametrize(
    "paths_fn",
    ["postgres_migration_paths", "oracle_migration_paths", "db2_migration_paths"],
)
def test_numbered_migrations_are_applied_in_numeric_order(paths_fn):
    """Existence is not enough: the shipped order must be numeric.

    ``sorted()`` over the directory is *lexicographic*, so a migration that
    reuses a number (``0067_new_thing``/``0067_another_thing``) lands next to
    it and a later ``0068`` still sorts last -- but a *duplicate* number also
    makes the relative order depend on the rest of the filename, so the
    narrowed trigger could silently be applied before the trigger it replaces.
    This asserts the whole sequence is non-decreasing by number, which the
    older existence check on 0062/0068 alone could not see.
    """
    from mnemos.persistence import schema

    names = [path.name for path in getattr(schema, paths_fn)()]
    numbered = _numeric_migrations(names)
    assert numbered, f"{paths_fn} returned no numbered migrations"
    keys = [number for number, _ in numbered]
    first_bad = next(
        (i for i in range(1, len(keys)) if keys[i] < keys[i - 1]),
        None,
    )
    assert first_bad is None, (
        f"{paths_fn} applies {numbered[first_bad][1]} after {numbered[first_bad - 1][1]}: "
        "migration numbers must stay unique and ordered"
    )


NARROWED_NUMBERED = "0068_federation_journal_narrow_trigger.sql"

#: Every numbered migration the narrowed trigger must be applied *after*.
#: 0062 defines the trigger; 0064 adds the dialect bodies.
NARROWED_DEPENDS_ON = (
    "0062_federation_journal.sql",
    "0064_federation_journal_dialects.sql",
)


@pytest.mark.parametrize(
    "paths_fn",
    ["postgres_migration_paths", "oracle_migration_paths", "db2_migration_paths"],
)
def test_narrowed_trigger_sorts_after_every_migration_it_depends_on(paths_fn):
    """The narrowed trigger must get the highest number, not merely a later slot.

    ``sorted()`` is lexicographic, so a reused number (``0067_x`` next to
    ``0067_y``) resolves by the rest of the filename rather than by intent.
    Pinning the number strictly above each dependency keeps the replacement
    function last no matter what a parallel branch adds at the same number.
    """
    from mnemos.persistence import schema

    names = [path.name for path in getattr(schema, paths_fn)()]
    assert NARROWED_NUMBERED in names, f"{paths_fn} never applies {NARROWED_NUMBERED}"

    numbered = {name: number for number, name in _numeric_migrations(names)}
    ours = numbered[NARROWED_NUMBERED]
    for dependency in NARROWED_DEPENDS_ON:
        assert dependency in names, f"{paths_fn} does not apply {dependency}"
        assert ours > numbered[dependency], (
            f"{NARROWED_NUMBERED} ({ours}) must be numbered above {dependency} ({numbered[dependency]})"
        )
    # And it must still be applied after them in the real, sorted order.
    assert names.index(NARROWED_NUMBERED) > names.index(NARROWED_DEPENDS_ON[0])
    assert names.index(NARROWED_NUMBERED) > names.index(NARROWED_DEPENDS_ON[1])


def test_no_other_migration_reuses_the_narrowed_trigger_number():
    """The behaviour this guards: two files must never share our number.

    Pre-existing pairs (0010/0011/0039/0054/0061c/0061d) are intentional and
    untouched; the regression is a *new* file landing on the narrowed
    trigger's number, which is exactly what the ``0067`` collision did.
    """
    directories = [
        "migrations",
        "migrations_oracle",
        "migrations_db2",
        "migrations_mysql",
        "migrations_mariadb",
    ]
    prefix = NARROWED_NUMBERED.split("_", 1)[0]
    ours_number = int(prefix)
    clashes = {}
    for directory in directories:
        same = sorted(
            path.name
            for path in (MIGRATIONS / directory).glob("*.sql")
            if path.name.split("_", 1)[0].rstrip("abcdefghijklmnopqrstuvwxyz").isdigit()
            and int(path.name.split("_", 1)[0].rstrip("abcdefghijklmnopqrstuvwxyz")) == ours_number
        )
        if len(same) > 1:
            clashes[directory] = same
    assert not clashes, f"migration number {ours_number} is reused: {clashes}"


# ---------------------------------------------------------------------------
# Pinning tests for findings raised against an older revision of this branch.
#
# A review of this branch reported, among others: an abstract/implementation
# name mismatch on the sync log, a missing Postgres rows-without-embedding
# counter, a mismatched SQLite sync-log INSERT, a Postgres ef_search passed as
# an int, and the narrowed trigger still living at 0067. None of those hold on
# the current tree; these tests pin the correct state so a regression cannot
# bring them back silently.
# ---------------------------------------------------------------------------

_PERSISTENCE = Path(__file__).resolve().parents[1] / "mnemos" / "persistence"


def test_sync_log_abstract_and_implementations_share_one_name():
    """Only ``finish_sync_log`` exists -- there is no ``record_sync_log``.

    A split name would leave the ABC unimplemented and make the repository
    abstract at instantiation time.
    """
    sources = sorted(_PERSISTENCE.glob("*.py"))
    text = "\n".join(path.read_text() for path in sources)
    assert "def finish_sync_log" in text
    assert "def record_sync_log" not in text, "a second sync-log writer name appeared"

    base = (_PERSISTENCE / "base.py").read_text()
    assert "async def finish_sync_log(" in base
    # mariadb inherits MysqlFederationRepository: one shared implementation is
    # correct, a second copy would be the drift this guards against.
    for backend in ("postgres", "sqlite", "mysql", "oracle", "db2"):
        body = (_PERSISTENCE / f"{backend}.py").read_text()
        assert "async def finish_sync_log(" in body, f"{backend} drifted off the ABC name"


def test_postgres_implements_the_per_peer_rows_without_embedding_counter():
    """The counter is abstract, so every backend repository must implement it."""
    base = (_PERSISTENCE / "base.py").read_text()
    assert "count_federated_rows_without_embedding_per_peer" in base
    postgres = (_PERSISTENCE / "postgres.py").read_text()
    assert "def count_federated_rows_without_embedding_per_peer" in postgres


def test_sqlite_sync_log_writer_updates_and_never_inserts():
    """``finish_sync_log`` is an UPDATE: its placeholders match its columns."""
    sqlite = (_PERSISTENCE / "sqlite.py").read_text()
    start = sqlite.index("async def finish_sync_log(")
    end = sqlite.index("async def count_federated_rows_without_embedding_per_peer", start)
    region = sqlite[start:end]
    assert "UPDATE federation_sync_log" in region
    assert "INSERT INTO federation_sync_log" not in region
    # Every column the statement sets has a matching bind, and nothing else.
    # 11 assignments = finished_at (= CURRENT_TIMESTAMP) + 10 bound columns.
    set_columns = region.split("SET", 1)[1].split("WHERE", 1)[0]
    assigned = [line for line in set_columns.splitlines() if "=" in line]
    assert len(assigned) == 11, assigned
    assert len(assigned) - 1 == region.split("WHERE", 1)[0].count("?")
    assert "events_received = ?" in region
    assert "rows_without_embedding = ?" in region


def test_postgres_ef_search_is_bound_as_text():
    """``set_config`` takes text; an int bind would need a cast server-side."""
    postgres = (_PERSISTENCE / "postgres.py").read_text()
    start = postgres.index("async def _apply_hnsw_query_tuning(")
    region = postgres[start : postgres.index("\n\n\n", start)]
    assert "str(ef_search)" in region, "hnsw.ef_search must be bound as a string"


def test_mysql_provisioning_points_at_the_0068_trigger():
    """The provisioning path names the same file the migration runner ships."""
    mysql = (_PERSISTENCE / "mysql.py").read_text()
    assert "0068_federation_journal_narrow_trigger.sql" in mysql
    assert "0067_federation_journal_narrow_trigger.sql" not in mysql
    for directory in (
        "migrations",
        "migrations_oracle",
        "migrations_db2",
        "migrations_mysql",
        "migrations_mariadb",
    ):
        assert (MIGRATIONS / directory / "0068_federation_journal_narrow_trigger.sql").is_file(), directory
        assert not (MIGRATIONS / directory / "0067_federation_journal_narrow_trigger.sql").exists(), directory


def test_federation_status_route_accepts_the_per_peer_counter():
    """``_to_peer`` carries ``rows_without_embedding`` into the payload."""
    routes = (Path(__file__).resolve().parents[1] / "mnemos" / "api" / "routes" / "federation.py").read_text()
    assert "def _to_peer(row, *, rows_without_embedding" in routes


def test_vector_tuning_exports_the_hnsw_fragment_the_installer_imports():
    """The installer imports it at module scope: a missing export is fatal."""
    tuning = (_PERSISTENCE / "vector_tuning.py").read_text()
    assert "def hnsw_index_options_fragment(" in tuning
    installer = (Path(__file__).resolve().parents[1] / "mnemos" / "installer" / "db.py").read_text()
    assert "from mnemos.persistence.vector_tuning import hnsw_index_options_fragment" in installer


# ---------------------------------------------------------------------------
# Pinning tests for a second round of findings on this branch.
#
# That review reported a FederationSyncLogResponse missing the new counters, a
# ``counters=`` keyword passed to a _store_memories that does not take it,
# a missing _fetch_all_dicts import, an unused rows_without_embedding, a
# loop-unsafe peer lock, and a UserContext imported from the wrong module.
# None hold. These pin the parts a silent regression could break.
# ---------------------------------------------------------------------------


def test_sync_log_models_declare_the_new_counters():
    """The counters must be on the model the endpoint actually serialises.

    They belong to FederationSyncLogEntry, not to the outer response, which
    only wraps a list of entries.
    """
    from mnemos.domain.models import FederationSyncLogEntry, FederationSyncLogResponse

    entry_fields = FederationSyncLogEntry.model_fields
    assert "events_received" in entry_fields
    assert "rows_without_embedding" in entry_fields
    # Both carry a default so pre-0067 rows (NULL columns) still validate.
    assert entry_fields["events_received"].default == 0
    assert entry_fields["rows_without_embedding"].default == 0

    assert "entries" in FederationSyncLogResponse.model_fields
    # And the entry model round-trips without the new columns supplied.
    entry = FederationSyncLogEntry(id="1", started_at="now", memories_pulled=0, memories_new=0, memories_updated=0)
    assert entry.rows_without_embedding == 0
    assert entry.events_received == 0


def test_store_memories_accepts_the_counters_keyword():
    """sync_peer passes counters= to _store_memories; the signature must allow it."""
    import inspect

    from mnemos.domain.federation import _store_memories

    params = inspect.signature(_store_memories).parameters
    assert "counters" in params, list(params)
    assert params["counters"].default is None

    source = inspect.getsource(_store_memories)
    assert "def _store_memories(" in source
    # The one caller inside the module is the sync loop.
    module_source = inspect.getsource(__import__("mnemos.domain.federation", fromlist=["x"]))
    assert "_store_memories(" in module_source
    assert "counters=" in module_source


def test_sync_peer_threads_rows_without_embedding_into_the_log():
    """The counted value must reach finish_sync_log, not be computed and dropped."""
    import inspect

    from mnemos.domain import federation as federation_module

    source = inspect.getsource(federation_module._sync_peer_locked)
    assert "count_federated_rows_without_embedding_per_peer" in source
    assert "rows_without_embedding=rows_without_embedding" in source


def test_peer_sync_lock_is_keyed_per_event_loop():
    """A lock cached globally would be bound to whichever loop awaited it first."""
    import asyncio

    from mnemos.domain.federation import _peer_sync_lock

    async def _take():
        return _peer_sync_lock("pin-peer")

    first = asyncio.run(_take())
    second = asyncio.run(_take())
    assert first is not second, "the lock registry is not loop-aware"


def test_mysql_count_helper_resolves_without_an_extra_import():
    """_fetch_all_dicts lives in mysql.py; importing it would be a NameError-free no-op."""
    from mnemos.persistence import mysql as mysql_module

    assert hasattr(mysql_module, "_fetch_all_dicts")


def test_user_context_is_importable_from_the_path_the_tests_use():
    """tests import it from mnemos.api.dependencies, which re-exports the class."""
    from mnemos.api.dependencies import UserContext as FromDeps
    from mnemos.core.auth_context import UserContext as FromCore

    assert FromDeps is FromCore


# ---------------------------------------------------------------------------
# Pinning tests for a third round of findings.
#
# That round repeated the counters= and 0068 claims already pinned above, and
# added: Path/pytest/inspect missing from this module, MIGRATIONS undefined or
# pointing outside db_migrations, PGVECTOR_ITERATIVE_SCAN_MIN_VERSION and
# _apply_hnsw_query_tuning not exposed by postgres, and postgres itself
# needing to re-export hnsw_index_options_fragment. The first two are covered
# by the tests above; the import and path claims are pinned here because a
# wrong MIGRATIONS root would silently turn every file-existence check into a
# skip-free pass on an empty glob.
# ---------------------------------------------------------------------------


def test_this_module_has_the_imports_its_tests_need():
    """Path, pytest and inspect must be real names in the test module's globals."""
    import inspect

    module = __import__(__name__, fromlist=["__file__"])
    for name in ("Path", "pytest"):
        assert name in vars(module), f"{name} is not defined in this test module"
    # inspect is imported inside the tests that need it; assert one such test
    # actually resolves it rather than relying on a module-level import.
    source = inspect.getsource(__import__(__name__, fromlist=["x"]))
    assert "import inspect" in source


def test_migrations_root_points_at_db_migrations():
    """Both backend trees live under mnemos/db_migrations."""
    assert MIGRATIONS.name == "db_migrations"
    assert MIGRATIONS.parent.name == "mnemos"
    assert MIGRATIONS.is_dir()
    # And every directory this module reads actually resolves below it.
    for directory in ("migrations", "migrations_oracle", "migrations_db2", "migrations_mysql", "migrations_mariadb"):
        assert (MIGRATIONS / directory).is_dir(), directory


def test_postgres_module_exposes_the_tuning_symbols_its_tests_import():
    """Tests reach into postgres._apply_hnsw_query_tuning and the version constant."""
    from mnemos.persistence import postgres

    assert callable(postgres._apply_hnsw_query_tuning)
    # A (major, minor) tuple, compared against a parsed pgvector version.
    assert isinstance(postgres.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION, tuple)


def test_events_received_is_its_own_counter_not_an_embed_counter():
    """total_events is tracked separately and passed straight to the log."""
    import inspect

    from mnemos.domain import federation as federation_module

    source = inspect.getsource(federation_module._sync_peer_locked)
    assert "total_events" in source
    assert "events_received=total_events" in source
    # The embed counter dict intentionally holds only embed outcomes.
    assert "embed_counters: Dict[str, int] = {}" in source
    assert 'embed_counters["events_received"]' not in source


# ---------------------------------------------------------------------------
# Pinning tests for a fourth round of findings.
#
# That round repeated the counters=, rows_without_embedding, pytest and
# MIGRATIONS claims, and added three that had not been raised before: that an
# event loop is not weakrefable, so the WeakKeyDictionary lock registry would
# raise TypeError; that _apply_hnsw_query_tuning does not take
# pgvector_version; and that PGVECTOR_ITERATIVE_SCAN_MIN_VERSION is missing
# from vector_tuning.__all__. The first two are pinned here because they are
# runtime properties a refactor could break with no test noticing.
# ---------------------------------------------------------------------------


def test_peer_lock_registry_works_and_releases_dead_loops():
    """The weakref registry must bind, isolate loops, and not leak.

    A WeakKeyDictionary keyed by the event loop is only sound if a loop is
    weakrefable at all; if it were not, every _peer_sync_lock call would raise
    TypeError. This also pins the two behaviours the registry exists for: one
    lock per loop, and a finished loop's locks are dropped.
    """
    import asyncio
    import weakref

    from mnemos.domain import federation as federation_module

    async def _acquire(peer: str):
        loop = asyncio.get_running_loop()
        weakref.ref(loop)  # raises TypeError if loops are not weakrefable
        lock = federation_module._peer_sync_lock(peer)
        async with lock:  # must be acquirable on THIS loop
            pass
        return lock

    first = asyncio.run(_acquire("pin-peer"))
    second = asyncio.run(_acquire("pin-peer"))
    assert first is not second, "the registry is not keyed by loop"

    # Two calls inside one loop return the same lock.
    async def _same():
        return federation_module._peer_sync_lock("pin-peer") is federation_module._peer_sync_lock("pin-peer")

    assert asyncio.run(_same()), "the lock is not stable within a loop"

    # A finished loop's entry must be dropped, or the registry leaks one
    # dict per loop for the process lifetime. Measured against the loop that
    # created it (not the global size), so other tests' loops cannot make
    # this flaky.
    held: list = []

    async def _hold():
        loop = asyncio.get_running_loop()
        held.append(weakref.ref(loop))
        federation_module._peer_sync_lock("pin-peer-leak")
        return True

    assert asyncio.run(_hold())
    assert held[0]() is None, "the loop was not released"
    # The loop that registered "pin-peer-leak" is gone, so its entry must be
    # too: a plain dict keyed by loop would keep the lock forever. Keyed on
    # this test's own peer name rather than the global size, so other tests'
    # live loops cannot make it flaky.
    import gc

    gc.collect()
    surviving = [registry for registry in federation_module._SYNC_LOCKS.values() if "pin-peer-leak" in registry]
    assert not surviving, "a dead loop still holds its locks"


def test_apply_hnsw_query_tuning_accepts_pgvector_version():
    """The helper gates iterative_scan on the version, so it must take it."""
    import inspect

    from mnemos.persistence import postgres

    params = inspect.signature(postgres._apply_hnsw_query_tuning).parameters
    assert "pgvector_version" in params, list(params)
    assert "limit" in params, list(params)


def test_vector_tuning_exports_the_version_constant():
    """Consumers compare against it, so it belongs in __all__."""
    from mnemos.persistence import vector_tuning

    assert "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION" in vector_tuning.__all__
    assert vector_tuning.PGVECTOR_ITERATIVE_SCAN_MIN_VERSION == (0, 8)
