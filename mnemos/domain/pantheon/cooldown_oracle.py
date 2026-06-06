"""Oracle-backed durable tier for the PANTHEON cooldown store.

Implements the :class:`~mnemos.domain.pantheon.cooldown.CooldownStore` contract
on Oracle 23ai — the production durable tier that rides the mnemos persistence
ABC (SQLite is the dev/edge equivalent). Mirrors
:class:`~mnemos.domain.pantheon.cooldown_sqlite.SqliteCooldownStore` semantics
(logical-TTL cooldowns, minute-bucket counters with on-write prune, tenant-
scoped) using Oracle ``MERGE`` for atomic upserts.

Connection model matches mnemos/hive_mind/oracle_repository.py: an injectable
``oracledb`` pool (``acquire``/``release``). Schema creation is deferred to
fleet-ops migrations (same convention as OracleHiveMindRepository.init) — the
DDL is exposed via :func:`schema_ddl` for that migration. Synchronous (matching
the ABC); calls are short and run off the hot path behind the write-behind L1.
"""

from __future__ import annotations

from threading import Lock

from mnemos.domain.pantheon.cooldown import CooldownStore

DEFAULT_PRUNE_MINUTES = 2


def _is_unique_violation(exc: BaseException) -> bool:
    """Oracle ORA-00001 unique/PK violation (from a concurrent first-insert race)."""
    return "ORA-00001" in str(exc)


_DDL_COOLDOWN = (
    "CREATE TABLE pantheon_cooldown ("
    " tenant VARCHAR2(128) NOT NULL,"
    " deployment VARCHAR2(256) NOT NULL,"
    " cooled_until BINARY_DOUBLE NOT NULL,"
    " CONSTRAINT pantheon_cooldown_pk PRIMARY KEY (tenant, deployment))"
)
_DDL_COUNTER = (
    "CREATE TABLE pantheon_counter ("
    " tenant VARCHAR2(128) NOT NULL,"
    " deployment VARCHAR2(256) NOT NULL,"
    " minute NUMBER(19) NOT NULL,"
    " successes NUMBER(19) DEFAULT 0 NOT NULL,"
    " failures NUMBER(19) DEFAULT 0 NOT NULL,"
    " CONSTRAINT pantheon_counter_pk PRIMARY KEY (tenant, deployment, minute))"
)

_MERGE_COOLDOWN = (
    "MERGE INTO pantheon_cooldown t "
    "USING (SELECT :tenant AS tenant, :deployment AS deployment FROM dual) s "
    "ON (t.tenant = s.tenant AND t.deployment = s.deployment) "
    "WHEN MATCHED THEN UPDATE SET cooled_until = :cooled_until "
    "WHEN NOT MATCHED THEN INSERT (tenant, deployment, cooled_until) "
    "VALUES (:tenant, :deployment, :cooled_until)"
)
_MERGE_COUNTER = (
    "MERGE INTO pantheon_counter t "
    "USING (SELECT :tenant AS tenant, :deployment AS deployment, :minute AS minute FROM dual) s "
    "ON (t.tenant = s.tenant AND t.deployment = s.deployment AND t.minute = s.minute) "
    "WHEN MATCHED THEN UPDATE SET successes = successes + :inc_s, failures = failures + :inc_f "
    "WHEN NOT MATCHED THEN INSERT (tenant, deployment, minute, successes, failures) "
    "VALUES (:tenant, :deployment, :minute, :inc_s, :inc_f)"
)
_SELECT_COOLDOWN = "SELECT cooled_until FROM pantheon_cooldown WHERE tenant = :tenant AND deployment = :deployment"
_SELECT_COUNTS = (
    "SELECT successes, failures FROM pantheon_counter "
    "WHERE tenant = :tenant AND deployment = :deployment AND minute = :minute"
)
_DELETE_STALE = "DELETE FROM pantheon_counter WHERE minute < :cutoff"


def schema_ddl() -> list[str]:
    """DDL statements for fleet-ops migration to apply (Oracle has no IF NOT EXISTS)."""
    return [_DDL_COOLDOWN, _DDL_COUNTER]


class OracleCooldownStore(CooldownStore):
    """Durable :class:`CooldownStore` backed by Oracle via an injectable pool."""

    def __init__(self, pool, *, prune_minutes: int = DEFAULT_PRUNE_MINUTES) -> None:
        self._pool = pool
        self._prune_minutes = prune_minutes
        self._lock = Lock()

    def _write(self, sql: str, params: dict) -> None:
        with self._lock:
            conn = self._pool.acquire()
            try:
                # retry once on a concurrent first-insert race: the MERGE
                # MATCHes (row now exists) and takes its UPDATE branch.
                for attempt in range(2):
                    cur = conn.cursor()
                    try:
                        cur.execute(sql, params)
                        conn.commit()
                        return
                    except Exception as exc:
                        if attempt == 0 and _is_unique_violation(exc):
                            continue
                        raise
                    finally:
                        cur.close()
            finally:
                self._pool.release(conn)

    def _read_one(self, sql: str, params: dict):
        with self._lock:
            conn = self._pool.acquire()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                    return cur.fetchone()
                finally:
                    cur.close()
            finally:
                self._pool.release(conn)

    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        row = self._read_one(_SELECT_COOLDOWN, {"tenant": tenant, "deployment": deployment})
        return float(row[0]) if row else None

    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        self._write(
            _MERGE_COOLDOWN,
            {"tenant": tenant, "deployment": deployment, "cooled_until": cooled_until},
        )

    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        self._write(
            _MERGE_COUNTER,
            {
                "tenant": tenant,
                "deployment": deployment,
                "minute": minute,
                "inc_s": 1 if success else 0,
                "inc_f": 0 if success else 1,
            },
        )
        self._write(_DELETE_STALE, {"cutoff": minute - self._prune_minutes})

    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        row = self._read_one(_SELECT_COUNTS, {"tenant": tenant, "deployment": deployment, "minute": minute})
        return (int(row[0]), int(row[1])) if row else (0, 0)
