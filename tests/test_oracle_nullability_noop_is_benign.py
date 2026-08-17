"""A fresh Oracle install must be able to finish its migrations.

Running the 6.1 migrations against Oracle 23.26.1-ee (the version the fleet
runs) on a FRESH schema aborted at 0050_lifecycle_workers.sql:

    ALTER TABLE deletion_requests MODIFY (memory_id NULL)
    ORA-01451: column to be modified to NULL cannot be modified to NULL

Oracle rejects a nullability change that is already satisfied, where Postgres
accepts `DROP NOT NULL` idempotently. A forward-only migration that relaxes a
column therefore aborts precisely when the column is already relaxed -- the
desired end state -- and provisioning never completes.

0050 says in its own header that re-running is safe "because Oracle schema
provisioning treats ORA-01430 duplicate-column errors as benign". ORA-01451 is
the same class and was simply missed.
"""

from __future__ import annotations

import pytest

from mnemos.persistence.schema import _is_benign_oracle_error


class _OraError(Exception):
    pass


ORA_01451 = "ORA-01451: column to be modified to NULL cannot be modified to NULL"
ORA_01442 = "ORA-01442: column to be modified to NOT NULL is already NOT NULL"


def test_the_exact_statement_that_blocked_a_fresh_install():
    assert _is_benign_oracle_error(
        "ALTER TABLE deletion_requests MODIFY (memory_id NULL)", _OraError(ORA_01451)
    ), "a fresh Oracle 6.1 install cannot complete provisioning without this"


@pytest.mark.parametrize("err", [ORA_01451, ORA_01442], ids=["to-null", "to-not-null"])
def test_both_nullability_no_ops_are_benign_on_alter(err):
    assert _is_benign_oracle_error("ALTER TABLE t MODIFY (c NULL)", _OraError(err))


@pytest.mark.parametrize("stmt", ["SELECT 1 FROM dual", "INSERT INTO t VALUES (1)", "DROP TABLE t"])
def test_not_forgiven_outside_alter(stmt):
    """Scoped like ORA-00001-on-INSERT: blanket forgiveness would hide real errors."""
    assert not _is_benign_oracle_error(stmt, _OraError(ORA_01451))


def test_unrelated_alter_errors_still_fail():
    assert not _is_benign_oracle_error(
        "ALTER TABLE t ADD c NUMBER", _OraError("ORA-00904: invalid identifier")
    )


# The guard passed its unit tests and still did not fire in production, because
# the executor hands the leading `--` banner comments to the guard along with
# the statement. Every test above uses a BARE statement; these use the real
# shape, which is what 0050 actually looks like on disk.

_REAL_0050 = (
    "--- Forward-only lifecycle worker migration. Re-running is safe because Oracle\n"
    "--- schema provisioning treats ORA-01430 duplicate-column errors as benign.\n"
    "ALTER TABLE deletion_requests MODIFY (memory_id NULL)"
)


def test_leading_sql_comments_do_not_defeat_the_guard():
    """The literal on-disk shape of the statement that blocked the install."""
    assert _is_benign_oracle_error(_REAL_0050, _OraError(ORA_01451)), (
        "a comment-prefixed ALTER must still be recognised, or the guard is "
        "dead code against every real migration file"
    )


def test_comment_prefix_does_not_forgive_unrelated_errors():
    assert not _is_benign_oracle_error(_REAL_0050, _OraError("ORA-00904: invalid identifier"))


def test_comment_prefixed_select_is_still_not_forgiven():
    assert not _is_benign_oracle_error(
        "-- banner\nSELECT 1 FROM dual", _OraError(ORA_01451)
    )
