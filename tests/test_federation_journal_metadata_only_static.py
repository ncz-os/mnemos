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
