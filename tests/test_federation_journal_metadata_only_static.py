"""Static cross-dialect assertions for 0072 (recall bookkeeping stops journaling).

The SQLite behaviour is exercised for real in
``test_federation_journal_metadata_only.py``. The other five dialect files
cannot run in CI without a live server, so this pins the invariants that a
hand edit could silently break:

* the metadata-only split exists in every dialect file;
* the metadata-only branch never inserts;
* the update branch never ceases to insert;
* the two flags the reviewer found missing (``old_exportable`` /
  ``new_exportable``) are populated on every insert path;
* no 0072 file references an undefined identifier in the predicate;
* the SQLite file is registered in ``SQLITE_MIGRATION_FILES`` so it actually
  runs.

What this does NOT cover: Oracle/DB2/MySQL/MariaDB/Postgres parse and execute
correctly against a live server. Only the shipped SQLite migration is proven
to execute.
"""

import re
from pathlib import Path

import pytest

from mnemos.persistence.sqlite import SQLITE_MIGRATION_FILES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "mnemos" / "db_migrations"

FILES = {
    "postgres": MIGRATIONS / "migrations" / "0072_federation_journal_metadata_only.sql",
    "sqlite": MIGRATIONS / "migrations_sqlite" / "migrations_v7_5_federation_journal_metadata_only_sqlite.sql",
    "mysql": MIGRATIONS / "migrations_mysql" / "0072_federation_journal_metadata_only.sql",
    "mariadb": MIGRATIONS / "migrations_mariadb" / "0072_federation_journal_metadata_only.sql",
    "oracle": MIGRATIONS / "migrations_oracle" / "0072_federation_journal_metadata_only.sql",
    "db2": MIGRATIONS / "migrations_db2" / "0072_federation_journal_metadata_only.sql",
}

# Columns the trigger predicate may compare. Every one of these exists on
# ``memories``; anything else in the predicate is the class of defect the
# review caught (a reference to a column that is not defined).
KNOWN_MEMORY_COLUMNS = {
    "federation_source",
    "content",
    "category",
    "namespace",
    "owner_id",
    "permission_mode",
    "deleted_at",
    "archived_at",
    "consolidated_into",
    "embedding",
    "metadata",
    "verbatim_content",
    "recall_count",
    "last_recalled_at",
}

METADATA_ONLY_COLUMNS = {"recall_count", "last_recalled_at"}


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_file_exists(dialect):
    assert FILES[dialect].is_file(), f"{dialect} 0072 mirror is missing"


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_adds_event_kind_column(dialect):
    assert "event_kind" in FILES[dialect].read_text()


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_predicate_references_only_known_columns(dialect):
    text = FILES[dialect].read_text()
    referenced = set(
        re.findall(
            r"(?:OLD|o|:OLD)\.(\w+)\s*(?:::text)?\s*"
            r"(?:IS DISTINCT FROM|IS NOT|<=>|<>|IS NULL)",
            text,
        )
    )
    unknown = referenced - KNOWN_MEMORY_COLUMNS
    assert not unknown, f"{dialect} 0072 compares undefined column(s): {sorted(unknown)}"


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_metadata_only_columns_are_split_out(dialect):
    text = FILES[dialect].read_text()
    for column in METADATA_ONLY_COLUMNS:
        assert column in text, f"{dialect} 0072 does not mention {column}"


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "mariadb", "oracle", "db2"])
def test_metadata_branch_never_inserts(dialect):
    """The metadata-only branch must reach a DELETE, never an INSERT."""
    text = FILES[dialect].read_text()
    lower = text.lower()
    meta_at = lower.find("metadata")
    assert meta_at != -1
    # Every DELETE of pending metadata rows must be guarded by event_kind, so
    # a content event queued for the publisher is never dropped.
    for match in re.finditer(r"DELETE FROM federation_changes[^;]*;", text, re.S):
        assert "'metadata'" in match.group(0), (
            f"{dialect}: pending-row DELETE is not restricted to metadata-only rows: {match.group(0)!r}"
        )


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_every_insert_populates_exportable_flags(dialect):
    text = FILES[dialect].read_text()
    for match in re.finditer(r"INSERT INTO federation_changes\(([^)]*)\)", text):
        columns = {column.strip() for column in match.group(1).split(",")}
        assert "old_exportable" in columns, f"{dialect}: INSERT omits old_exportable"
        assert "new_exportable" in columns, f"{dialect}: INSERT omits new_exportable"


def test_sqlite_mirror_is_registered():
    assert "migrations_v7_5_federation_journal_metadata_only_sqlite.sql" in SQLITE_MIGRATION_FILES


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_no_reference_to_undefined_seq_predicate(dialect):
    """``seq`` exists on every dialect's ``federation_changes`` (0062) and is
    the publisher's watermark; the predicate must not require it to be NULL on
    a row whose primary key is ``event_id`` instead."""
    text = FILES[dialect].read_text()
    for match in re.finditer(r"WHERE[^;\n]*seq IS NULL[^;\n]*", text, re.S):
        clause = match.group(0)
        assert "event_kind" in clause and "memory_id" in clause, (
            f"{dialect}: a seq IS NULL test is not scoped to pending metadata rows: {clause!r}"
        )


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_metadata_branch_targets_a_delete_of_unpublished_rows(dialect):
    """Absorption removes rows queued earlier -- never rows it inserts.

    The trigger intentionally inserts no ``event_kind='metadata'`` row: the
    whole point of 0072 is that a metadata-only write appends nothing. The
    DELETE exists to reclaim rows left pending by a pre-0072 node, a restored
    backup, or a replica that has not yet applied this migration. Pin that
    shape so "deletes rows it never creates" cannot quietly become true (a
    DELETE with no ``seq IS NULL`` would start destroying published events,
    and one with no ``event_kind`` filter would destroy content events).
    """
    text = FILES[dialect].read_text()
    deletes = re.findall(r"DELETE FROM federation_changes[^;]*;", text, re.S)
    assert deletes, f"{dialect}: metadata-only branch has no DELETE"
    for statement in deletes:
        assert "event_kind" in statement and "'metadata'" in statement, (
            f"{dialect}: absorption DELETE is not restricted to metadata rows: {statement!r}"
        )
        assert "seq IS NULL" in statement, (
            f"{dialect}: absorption DELETE is not restricted to unpublished rows: {statement!r}"
        )
        assert "memory_id" in statement, f"{dialect}: absorption DELETE is not scoped to one memory: {statement!r}"


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_trigger_never_inserts_a_metadata_only_event(dialect):
    """The split must not silently degrade into "journal the counter anyway"."""
    text = FILES[dialect].read_text()
    for statement in re.findall(r"INSERT INTO federation_changes[^;]*;", text, re.S):
        assert "'metadata'" not in statement, (
            f"{dialect}: a trigger INSERT writes an event_kind='metadata' row; metadata-only writes must append nothing"
        )


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "mariadb", "postgres", "oracle", "db2"])
def test_seq_is_the_pending_watermark_not_the_primary_key(dialect):
    """``seq IS NULL`` must keep meaning "not yet published".

    The absorption predicate filters on ``seq IS NULL``. That is only correct
    because ``event_id`` is the identity column and ``seq`` is a nullable
    watermark the publisher assigns later. Pin the 0062 shape so a future
    "cleanup" cannot make ``seq`` the primary key, which would turn every
    ``seq IS NULL`` check into a filter that never matches.
    """
    # MariaDB has no 0062 of its own: it runs the MySQL schema verbatim
    # (wire-compatible), so the MySQL definition is the one that governs it.
    directory = {"postgres": "migrations", "mariadb": "migrations_mysql"}.get(dialect, f"migrations_{dialect}")
    definition = (MIGRATIONS / directory / "0062_federation_journal.sql").read_text()

    table = re.search(r"CREATE TABLE[^(]*federation_changes \((.*?)\)\s*[;@']", definition, re.S)
    assert table, f"{dialect}: cannot find the federation_changes definition"
    body = table.group(1)
    event_id = re.search(r"event_id ([^,]*?PRIMARY KEY)", body)
    assert event_id, f"{dialect}: event_id is not the primary key"
    seq = re.search(r"\bseq (\w+)", body)
    assert seq, f"{dialect}: no seq column"
    assert "PRIMARY KEY" not in seq.group(1) and "NOT NULL" not in seq.group(1), (
        f"{dialect}: seq must stay a nullable watermark, never the identity: {seq.group(0)!r}"
    )


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_event_kind_has_a_content_default_so_legacy_rows_backfill(dialect):
    """ADD COLUMN ... DEFAULT 'content' backfills existing rows.

    Without the default, rows inserted before the migration would carry NULL
    and escape both the content and metadata branches. With it, every
    pre-existing row is content (which is what the old trigger meant).
    """
    text = FILES[dialect].read_text()
    add = re.search(r"ADD (?:COLUMN )?(?:IF NOT EXISTS )?event_kind[^;@\n]*", text)
    assert add, f"{dialect}: no event_kind ADD COLUMN"
    # Postgres splits the default onto a following ALTER COLUMN ... SET DEFAULT
    # (ADD COLUMN IF NOT EXISTS has no inline DEFAULT arm); every other dialect
    # carries it inline.
    same_statement = text[add.start() :].split(";", 1)[0]
    asserts_default = "DEFAULT 'content'" in same_statement or re.search(
        r"ALTER COLUMN event_kind SET DEFAULT 'content'", text
    )
    assert asserts_default, f"{dialect}: event_kind must default to 'content' so legacy rows backfill: {add.group(0)!r}"


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "mariadb", "postgres", "oracle", "db2"])
def test_content_branch_also_absorbs_pending_metadata_rows(dialect):
    """A content event supersedes any pending metadata row for that memory.

    On the single-trigger dialects (postgres/mysql/mariadb/db2/oracle) one
    statement can change a content column and ``recall_count`` together. The
    content branch then appends one event; without deleting the pending
    metadata row the feed delivers that memory twice for identical state.
    SQLite splits content and metadata into disjoint ``AFTER UPDATE OF``
    triggers, so a single statement cannot hit both and it needs no DELETE
    in its content trigger.
    """
    text = FILES[dialect].read_text()
    if dialect == "sqlite":
        # Disjoint column lists: content trigger must NOT touch recall columns.
        content_trigger = re.search(r"CREATE TRIGGER fed_journal_update_content.*?END;", text, re.S)
        assert content_trigger, "sqlite: no content trigger"
        of_clause = re.search(r"AFTER UPDATE OF ([^O]+?) ON memories", content_trigger.group(0))
        assert of_clause, "sqlite: content trigger has no column list"
        columns = {c.strip() for c in of_clause.group(1).split(",")}
        assert "recall_count" not in columns and "last_recalled_at" not in columns, (
            f"sqlite: content and metadata triggers must stay disjoint, got {sorted(columns)}"
        )
        return

    # Single-trigger dialects: the content branch must begin with the DELETE.
    if dialect in {"postgres"}:
        branch = re.search(r"IF content_changed THEN(.*?)ELSE", text, re.S)
    elif dialect in {"mysql", "mariadb"}:
        # the inner content IF opens with the long NOT(...) predicate
        branch = re.search(r"THEN\n(.*?)ELSE", text, re.S)
    elif dialect == "db2":
        branch = re.search(r"IF v_is_content = 1 THEN(.*?)ELSEIF", text, re.S)
    else:  # oracle
        branch = re.search(r"IF v_content = 1 THEN(.*?)ELSIF", text, re.S)
    assert branch, f"{dialect}: cannot locate the content branch"
    body = branch.group(1)
    delete = re.search(r"DELETE FROM federation_changes[^;]*;", body)
    assert delete, f"{dialect}: content branch does not absorb pending metadata rows"
    assert "'metadata'" in delete.group(0) and "seq IS NULL" in delete.group(0), (
        f"{dialect}: content-branch DELETE is not scoped to unpublished metadata rows: {delete.group(0)!r}"
    )
    # It must run before the INSERT so the new event is the only pending row.
    assert body.index(delete.group(0)) < body.index("INSERT INTO federation_changes"), (
        f"{dialect}: absorb must precede the content INSERT"
    )


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_every_absorption_delete_is_scoped_to_one_memory(dialect):
    """No absorption DELETE may be global.

    A DELETE keyed only on ``seq IS NULL AND event_kind='metadata'`` would wipe
    every other memory's pending metadata row whenever one memory is recalled.
    The statement may wrap across lines, so match the whole statement rather
    than a single line.
    """
    text = FILES[dialect].read_text()
    deletes = re.findall(r"DELETE FROM federation_changes(.*?);", text, re.S)
    assert deletes, f"{dialect}: expected at least one absorption DELETE"
    for body in deletes:
        assert re.search(r"memory_id\s*=\s*(NEW\.id|:NEW\.id|n\.id)", body), (
            f"{dialect}: absorption DELETE is not scoped to one memory: {' '.join(body.split())!r}"
        )
        assert "seq IS NULL" in body, f"{dialect}: DELETE must only target unpublished rows"
        assert "'metadata'" in body, f"{dialect}: DELETE must only target metadata rows"


@pytest.mark.parametrize("dialect", sorted(FILES))
def test_no_absorption_delete_targets_published_rows(dialect):
    """Published (sequenced) rows are journal history and must never be deleted.

    Deleting them would move a peer's cursor semantics. Only pending rows
    coalesce.
    """
    text = FILES[dialect].read_text()
    for body in re.findall(r"DELETE FROM federation_changes(.*?);", text, re.S):
        assert "seq IS NOT NULL" not in body and "seq >" not in body, (
            f"{dialect}: a DELETE targets published rows: {' '.join(body.split())!r}"
        )
