"""SQLite-backed durable tier for the PANTHEON cooldown store.

Implements the :class:`~mnemos.domain.pantheon.cooldown.CooldownStore` contract
on a SQLite file. This is the durable backing tier the in-process L1
(:class:`~mnemos.domain.pantheon.cooldown.InMemoryCooldownStore`) writes behind:
cooldown state and minute-bucket counters survive a process restart.

Design notes (matching the cache-aside contract, GRAEAE mandate §A):
  * Cooldown TTL is *logical*: ``cooled_until`` is stored as an epoch-seconds
    column and compared to the caller's clock at read — no DB-native TTL, no
    per-request DELETE.
  * Counters are minute-bucketed and incremented atomically with
    ``INSERT ... ON CONFLICT DO UPDATE``. Stale minute buckets are pruned on
    write so the table stays bounded.
  * Keys are ``(tenant, deployment)`` so one tenant's failures never affect
    another's.

Stdlib ``sqlite3`` only. Methods are synchronous (matching the ABC); SQLite
writes are local and fast, and in the cache-aside design they run off the hot
path (write-behind), not on the LLM call path.
"""

from __future__ import annotations

import sqlite3
from threading import Lock

from mnemos.domain.pantheon.cooldown import CooldownStore

DEFAULT_PRUNE_MINUTES = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pantheon_cooldown (
    tenant       TEXT NOT NULL,
    deployment   TEXT NOT NULL,
    cooled_until REAL NOT NULL,
    PRIMARY KEY (tenant, deployment)
);
CREATE TABLE IF NOT EXISTS pantheon_counter (
    tenant     TEXT NOT NULL,
    deployment TEXT NOT NULL,
    minute     INTEGER NOT NULL,
    successes  INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, deployment, minute)
);
"""


class SqliteCooldownStore(CooldownStore):
    """Durable :class:`CooldownStore` backed by a SQLite file."""

    def __init__(self, path: str, *, prune_minutes: int = DEFAULT_PRUNE_MINUTES) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = Lock()
        self._prune_minutes = prune_minutes
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cooled_until FROM pantheon_cooldown WHERE tenant=? AND deployment=?",
                (tenant, deployment),
            ).fetchone()
            return row[0] if row else None

    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pantheon_cooldown (tenant, deployment, cooled_until) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (tenant, deployment) DO UPDATE SET cooled_until=excluded.cooled_until",
                (tenant, deployment, cooled_until),
            )

    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        # `column` is a controlled literal (never user input) — safe to inline.
        column = "successes" if success else "failures"
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO pantheon_counter (tenant, deployment, minute, {column}) "  # noqa: S608
                "VALUES (?, ?, ?, 1) "
                f"ON CONFLICT (tenant, deployment, minute) DO UPDATE SET {column}={column}+1",
                (tenant, deployment, minute),
            )
            self._conn.execute(
                "DELETE FROM pantheon_counter WHERE minute < ?",
                (minute - self._prune_minutes,),
            )

    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT successes, failures FROM pantheon_counter WHERE tenant=? AND deployment=? AND minute=?",
                (tenant, deployment, minute),
            ).fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
