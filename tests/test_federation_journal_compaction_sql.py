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
    ("migrations", "0067_federation_journal_narrow_trigger.sql"),
    ("migrations_oracle", "0067_federation_journal_narrow_trigger.sql"),
    ("migrations_db2", "0067_federation_journal_narrow_trigger.sql"),
    ("migrations_mysql", "0067_federation_journal_narrow_trigger.sql"),
    ("migrations_mariadb", "0067_federation_journal_narrow_trigger.sql"),
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
    assert "0067_federation_journal_narrow_trigger.sql" in source


def test_sqlite_migration_is_registered():
    source = (Path(__file__).resolve().parents[1] / "mnemos" / "persistence" / "sqlite.py").read_text()
    assert "migrations_v7_2_federation_journal_narrow_trigger_sqlite.sql" in source
