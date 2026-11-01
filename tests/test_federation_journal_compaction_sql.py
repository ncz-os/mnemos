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
