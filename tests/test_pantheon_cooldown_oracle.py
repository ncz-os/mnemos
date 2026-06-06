"""Tests for OracleCooldownStore via a fake oracledb pool.

The fake emulates the MERGE/SELECT/DELETE statements against in-memory dicts, so
the store's logic round-trips without a live Oracle (live schema is applied by
fleet-ops migration, per repo convention). Also asserts Oracle SQL shape.
"""

from __future__ import annotations

from mnemos.domain.pantheon.cooldown import CooldownManager, CooldownStore
from mnemos.domain.pantheon.cooldown_oracle import OracleCooldownStore, schema_ddl
from mnemos.domain.pantheon.errors import normalize_error


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._result = None

    def execute(self, sql, params=None):
        p = params or {}
        self._db.calls.append(sql)
        if sql.startswith("MERGE INTO pantheon_cooldown"):
            self._db.cooldowns[(p["tenant"], p["deployment"])] = p["cooled_until"]
        elif sql.startswith("MERGE INTO pantheon_counter"):
            key = (p["tenant"], p["deployment"], p["minute"])
            b = self._db.counters.setdefault(key, [0, 0])
            b[0] += p["inc_s"]
            b[1] += p["inc_f"]
        elif sql.startswith("SELECT cooled_until"):
            v = self._db.cooldowns.get((p["tenant"], p["deployment"]))
            self._result = (v,) if v is not None else None
        elif sql.startswith("SELECT successes"):
            b = self._db.counters.get((p["tenant"], p["deployment"], p["minute"]))
            self._result = (b[0], b[1]) if b else None
        elif sql.startswith("DELETE FROM pantheon_counter"):
            cutoff = p["cutoff"]
            for k in [k for k in self._db.counters if k[2] < cutoff]:
                del self._db.counters[k]

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, db):
        self._db = db

    def cursor(self):
        return _FakeCursor(self._db)

    def commit(self):
        self._db.commits += 1


class _FakePool:
    def __init__(self):
        self.cooldowns = {}
        self.counters = {}
        self.calls = []
        self.commits = 0
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1
        return _FakeConn(self)

    def release(self, _conn):
        self.released += 1


def _store(**kw):
    return OracleCooldownStore(_FakePool(), **kw)


def test_satisfies_abc_and_ddl():
    s = _store()
    assert isinstance(s, CooldownStore)
    ddl = schema_ddl()
    assert len(ddl) == 2 and all(d.startswith("CREATE TABLE") for d in ddl)


def test_cooldown_roundtrip_uses_merge_and_select():
    s = _store()
    assert s.get_cooled_until("t", "d") is None
    s.set_cooldown("t", "d", 1234.5)
    assert s.get_cooled_until("t", "d") == 1234.5
    s.set_cooldown("t", "d", 2000.0)  # upsert
    assert s.get_cooled_until("t", "d") == 2000.0
    assert any(c.startswith("MERGE INTO pantheon_cooldown") for c in s._pool.calls)  # noqa: SLF001
    assert any(c.startswith("SELECT cooled_until") for c in s._pool.calls)  # noqa: SLF001


def test_counter_increment_and_prune():
    s = _store(prune_minutes=2)
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 100, success=False)
    assert s.get_counts("t", "d", 100) == (1, 1)
    s.incr("t", "d", 103, success=False)  # cutoff 101 -> minute 100 pruned
    assert s.get_counts("t", "d", 100) == (0, 0)
    assert s.get_counts("t", "d", 103) == (0, 1)
    assert any(c.startswith("MERGE INTO pantheon_counter") for c in s._pool.calls)  # noqa: SLF001
    assert any(c.startswith("DELETE FROM pantheon_counter") for c in s._pool.calls)  # noqa: SLF001


def test_tenant_isolation_and_pool_lifecycle():
    s = _store()
    s.set_cooldown("A", "d", 5.0)
    assert s.get_cooled_until("A", "d") == 5.0
    assert s.get_cooled_until("B", "d") is None
    # every op acquires + releases a pooled connection
    assert s._pool.acquired == s._pool.released  # noqa: SLF001


def test_drop_in_for_cooldown_manager():
    mgr = CooldownManager(_store())
    d = mgr.record_failure("dep", normalize_error(status_code=429), 1000.0, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert mgr.is_cooled("dep", 1000.0) is True
    assert mgr.is_cooled("dep", 1010.0) is False
