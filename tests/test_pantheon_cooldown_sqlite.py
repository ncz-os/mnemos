"""Tests for the SQLite-backed durable CooldownStore."""

from __future__ import annotations

from mnemos.domain.pantheon.cooldown import CooldownManager, CooldownStore
from mnemos.domain.pantheon.cooldown_sqlite import SqliteCooldownStore
from mnemos.domain.pantheon.errors import normalize_error


def _store(tmp_path, **kw):
    return SqliteCooldownStore(str(tmp_path / "cd.sqlite"), **kw)


def test_satisfies_cooldownstore_abc(tmp_path):
    s = _store(tmp_path)
    assert isinstance(s, CooldownStore)
    s.close()


def test_cooldown_roundtrip_and_upsert(tmp_path):
    s = _store(tmp_path)
    assert s.get_cooled_until("t", "d") is None
    s.set_cooldown("t", "d", 1234.5)
    assert s.get_cooled_until("t", "d") == 1234.5
    s.set_cooldown("t", "d", 2000.0)  # ON CONFLICT upsert
    assert s.get_cooled_until("t", "d") == 2000.0
    s.close()


def test_counts_increment_atomically(tmp_path):
    s = _store(tmp_path)
    assert s.get_counts("t", "d", 100) == (0, 0)
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 100, success=False)
    assert s.get_counts("t", "d", 100) == (2, 1)
    s.close()


def test_prunes_old_minute_buckets(tmp_path):
    s = _store(tmp_path, prune_minutes=2)
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 103, success=False)  # cutoff = 101 -> minute 100 pruned
    assert s.get_counts("t", "d", 100) == (0, 0)
    assert s.get_counts("t", "d", 103) == (0, 1)
    s.close()


def test_tenant_and_deployment_isolation(tmp_path):
    s = _store(tmp_path)
    s.set_cooldown("A", "d", 500.0)
    assert s.get_cooled_until("A", "d") == 500.0
    assert s.get_cooled_until("B", "d") is None
    assert s.get_cooled_until("A", "d2") is None
    s.close()


def test_persists_across_reconnect(tmp_path):
    path = str(tmp_path / "cd.sqlite")
    s = SqliteCooldownStore(path)
    s.set_cooldown("t", "d", 999.0)
    s.incr("t", "d", 100, success=False)
    s.close()

    s2 = SqliteCooldownStore(path)  # reopen same file
    assert s2.get_cooled_until("t", "d") == 999.0
    assert s2.get_counts("t", "d", 100) == (0, 1)
    s2.close()


def test_drop_in_for_cooldown_manager(tmp_path):
    mgr = CooldownManager(_store(tmp_path))
    decision = mgr.record_failure("dep", normalize_error(status_code=429), 1000.0, is_single_deployment_group=False)
    assert decision.should_cooldown is True
    assert mgr.is_cooled("dep", 1000.0) is True
    assert mgr.is_cooled("dep", 1010.0) is False  # past the logical-TTL cooled_until
