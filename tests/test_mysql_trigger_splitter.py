"""Migration parsing must preserve executable trigger boundaries."""

from pathlib import Path
import pytest
from mnemos.persistence.mysql import _split_mysql_statements


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
