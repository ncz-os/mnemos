"""Migration parsing must preserve executable trigger boundaries."""

from pathlib import Path
import pytest
from mnemos.persistence.mysql import _split_mysql_statements, _strip_leading_comments


@pytest.mark.parametrize(
    "body",
    [
        "SET x = CASE WHEN 1 THEN 2 ELSE 3 END; SET y = 4;",
        "SET x = CASE WHEN 1 THEN CASE WHEN 2 THEN 3 END ELSE 4 END; SET y = 4;",
        "CASE x WHEN 1 THEN SET x=2; ELSE SET x=3; END CASE; SET y=4;",
        "BEGIN SET x=CASE WHEN 1 THEN 2 END; END; SET y=4;",
        "SET x='CASE END BEGIN'; /* CASE END */ SET y=4;",
    ],
)
def test_case_end_does_not_split_trigger(body):
    trigger = "CREATE TRIGGER t AFTER INSERT ON memories FOR EACH ROW BEGIN " + body + " END;"
    parts = _split_mysql_statements(trigger + " SELECT 1;")
    assert len(parts) == 2
    assert parts[0].rstrip(";") == trigger.rstrip(";")
    assert parts[1] == "SELECT 1"


def test_real_federation_migration_has_three_complete_triggers():
    path = Path(__file__).parents[1] / "mnemos/db_migrations/migrations_mysql/0062_federation_journal.sql"
    statements = _split_mysql_statements(path.read_text())
    triggers = [s for s in statements if s.startswith("CREATE TRIGGER")]
    assert len(triggers) == 3
    assert all(s.endswith("END IF;\nEND;") for s in triggers)
    assert not any(s.startswith(("END", "ELSE")) for s in statements)


def test_header_comment_does_not_wrap_the_first_statement():
    """A file header must not become part of the first executable statement.

    aiomysql executes one statement per ``execute`` and rejects multi-statement
    text, so "-- header\\n\\nALTER ...;" never ran: the FIRST ALTER of every
    migration shaped like that (0065's ``key_prefix``, 0071's
    ``last_cursor_ts``) was silently a no-op on upgraded databases.
    """
    sql = (
        "-- migration: 9001_example\n"
        "-- purpose:   header block\n"
        "\n"
        "ALTER TABLE t ADD COLUMN a INT;\n"
        "ALTER TABLE t ADD COLUMN b INT;\n"
    )
    statements = _split_mysql_statements(sql)
    assert len(statements) == 2
    executable = [_strip_leading_comments(s) for s in statements]
    assert executable == ["ALTER TABLE t ADD COLUMN a INT", "ALTER TABLE t ADD COLUMN b INT"]
    assert not any(s.lstrip().startswith("--") for s in executable)


def test_leading_comments_are_left_alone_inside_a_statement():
    trigger = "CREATE TRIGGER t AFTER INSERT ON memories FOR EACH ROW\nBEGIN\n  -- body note\n  SET @x = 1;\nEND;"
    statements = _split_mysql_statements(trigger)
    assert len(statements) == 1
    # No comment block before the SQL: the statement is untouched.
    assert _strip_leading_comments(statements[0]) == statements[0]
    assert "-- body note" in statements[0]


@pytest.mark.parametrize("name", ["0065_api_keys_key_prefix.sql", "0071_federation_replica_health.sql"])
def test_real_alter_only_migrations_execute_every_alter(name):
    path = Path(__file__).parents[1] / "mnemos/db_migrations/migrations_mysql" / name
    statements = [_strip_leading_comments(s) for s in _split_mysql_statements(path.read_text())]
    assert statements, name
    for statement in statements:
        assert statement.lstrip().upper().startswith("ALTER TABLE"), statement
        assert "--" not in statement.splitlines()[0], f"{name}: comment still heads a statement"
