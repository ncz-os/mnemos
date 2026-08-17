"""The deletion-request claim must build SQL each backend will actually run.

Measured on the production Oracle 23ai primary during the 6.1 upgrade: the
Oracle branch built

    SELECT * FROM (SELECT * FROM deletion_requests WHERE ... ORDER BY ...)
    WHERE ROWNUM <= 1 FOR UPDATE SKIP LOCKED

and every tick raised

    ORA-02014: cannot select FOR UPDATE from view with DISTINCT, GROUP BY, etc.

The branch already knew Oracle rejects FETCH FIRST with FOR UPDATE, but the
replacement had the same defect: the lock still targets an inline view, and
ROWNUM inside it is enough to trigger ORA-02014. Both deletion workers sat in
a permanent error state and /health reported degraded.

There is no Oracle in unit CI, so these tests pin the generated SQL shape
rather than executing it. The shape is the thing that was wrong.
"""

from __future__ import annotations

import pytest

from mnemos.persistence import worker_lifecycle


class _RecordingOps:
    """Captures the statement `_claim` builds without touching a database."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.statements: list[str] = []

    async def fetchone(self, sql: str, *params):
        self.statements.append(sql)
        return None  # empty queue -> _claim returns None before any UPDATE

    async def execute(self, sql: str, *params):  # pragma: no cover - not reached
        self.statements.append(sql)
        return 0


async def _claim_sql(dialect: str, *, hard: bool) -> str:
    ops = _RecordingOps(dialect)
    assert await worker_lifecycle._claim(ops, hard=hard) is None
    assert len(ops.statements) == 1
    return " ".join(ops.statements[0].split())


@pytest.mark.asyncio
@pytest.mark.parametrize("hard", [False, True], ids=["soft", "hard"])
async def test_oracle_locks_the_table_not_a_view(hard):
    """FOR UPDATE must apply to the base table, never to a ROWNUM view."""
    sql = await _claim_sql("oracle", hard=hard)
    lock_at = sql.index("FOR UPDATE")
    prefix = sql[:lock_at]
    assert prefix.count("(") == prefix.count(")"), (
        "FOR UPDATE must sit outside every subquery, or Oracle locks the view: " + sql
    )
    assert sql.startswith("SELECT * FROM deletion_requests WHERE id ="), (
        "the outer query must select straight from the table: " + sql
    )
    assert "SKIP LOCKED" in sql, "concurrent workers must not block on each other"


@pytest.mark.asyncio
async def test_oracle_never_emits_the_ora_02014_shapes():
    """The two constructs Oracle refuses to lock through."""
    sql = await _claim_sql("oracle", hard=False)
    assert "FETCH FIRST" not in sql.upper(), "FETCH FIRST + FOR UPDATE is ORA-02014"
    tail = sql[sql.index("FOR UPDATE") :]
    assert "ROWNUM" not in tail.upper()
    # The offending form: ROWNUM filter applied to the locked relation itself.
    assert ") WHERE ROWNUM <= 1 FOR UPDATE" not in sql.upper(), (
        "this is the exact statement that raised ORA-02014 in production: " + sql
    )


@pytest.mark.asyncio
async def test_claim_still_orders_by_queue_priority():
    """Locking the table must not cost the ordering that makes it a queue."""
    soft = await _claim_sql("oracle", hard=False)
    assert "ORDER BY confirmed_at ASC, requested_at ASC" in soft
    hard = await _claim_sql("oracle", hard=True)
    assert "ORDER BY restore_by ASC" in hard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "expected"),
    [("mysql", "LIMIT 1 FOR UPDATE SKIP LOCKED"), ("postgres", "LIMIT 1")],
)
async def test_other_dialects_are_unchanged(dialect, expected):
    """The Oracle fix must not disturb the backends that already worked."""
    assert (await _claim_sql(dialect, hard=False)).endswith(expected)


@pytest.mark.asyncio
async def test_placeholder_count_survives_the_rewrite():
    """The hard-delete branch binds restore_by; it must still bind exactly once.

    The predicate moved into a nested subquery, which is precisely the kind of
    edit that silently drops or duplicates a bind parameter.
    """
    assert (await _claim_sql("oracle", hard=True)).count("?") == 1
    assert (await _claim_sql("oracle", hard=False)).count("?") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("hard", [False, True], ids=["soft", "hard"])
async def test_db2_uses_native_sql_not_oracle_compatibility(hard):
    """Db2 must not depend on DB2_COMPATIBILITY_VECTOR=ORA.

    ROWNUM exists on Db2 only under the Oracle-compatibility vector, which is
    an instance-wide setting a Db2 deployment is not obliged to enable. The
    fleet's Db2 12.1.5 happens to have it on, so the shared Oracle branch
    appeared to work there -- it would have failed on a stock instance.

    Verified against Db2 Community Edition 12.1.5: this statement is accepted,
    and it uses no Oracle-compatibility construct.
    """
    sql = await _claim_sql("db2", hard=hard)
    assert "ROWNUM" not in sql.upper(), (
        "ROWNUM is Oracle-compatibility-only on Db2: " + sql
    )
    assert "FETCH FIRST 1 ROWS ONLY" in sql, "Db2 limits rows with FETCH FIRST"
    assert "SKIP LOCKED DATA" in sql, "workers must step over each other's rows"
    assert "KEEP UPDATE LOCKS" in sql, (
        "the row must be update-locked at read time, or two workers race to the UPDATE"
    )


@pytest.mark.asyncio
async def test_db2_and_oracle_no_longer_share_a_statement():
    """They diverged deliberately; a future edit must not re-merge them."""
    assert await _claim_sql("db2", hard=False) != await _claim_sql("oracle", hard=False)
