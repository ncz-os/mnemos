"""Tests for the write-behind cache-aside CooldownStore."""

from __future__ import annotations

from mnemos.domain.pantheon.cooldown import CooldownManager, InMemoryCooldownStore
from mnemos.domain.pantheon.cooldown_cache import WriteBehindCooldownStore
from mnemos.domain.pantheon.cooldown_sqlite import SqliteCooldownStore
from mnemos.domain.pantheon.errors import normalize_error


class _CountingDurable(InMemoryCooldownStore):
    """In-memory durable that counts how many writes reached it."""

    def __init__(self):
        super().__init__()
        self.writes = 0

    def set_cooldown(self, tenant, deployment, cooled_until):
        self.writes += 1
        super().set_cooldown(tenant, deployment, cooled_until)

    def incr(self, tenant, deployment, minute, *, success):
        self.writes += 1
        super().incr(tenant, deployment, minute, success=success)


def test_writes_hit_l1_immediately_durable_only_on_flush():
    durable = _CountingDurable()
    s = WriteBehindCooldownStore(durable, flush_threshold=100)
    s.set_cooldown("t", "d", 500.0)
    s.incr("t", "d", 10, success=False)
    # L1 reflects immediately
    assert s.get_cooled_until("t", "d") == 500.0
    assert s.get_counts("t", "d", 10) == (0, 1)
    # durable untouched until flush
    assert durable.writes == 0
    assert s.pending == 2
    n = s.flush()
    assert n == 2
    assert durable.writes == 2
    assert s.pending == 0


def test_auto_flush_on_threshold():
    durable = _CountingDurable()
    s = WriteBehindCooldownStore(durable, flush_threshold=3)
    s.incr("t", "d", 1, success=True)
    s.incr("t", "d", 1, success=True)
    assert durable.writes == 0  # below threshold
    s.incr("t", "d", 1, success=True)  # 3rd -> auto-flush
    assert durable.writes == 3
    assert s.pending == 0


def test_cooldown_read_through_from_durable():
    durable = InMemoryCooldownStore()
    durable.set_cooldown("t", "d", 999.0)  # set by a "prior run"
    s = WriteBehindCooldownStore(durable)  # fresh L1
    assert s.get_cooled_until("t", "d") == 999.0  # read-through
    # now cached in L1
    durable.set_cooldown("t", "d", 111.0)  # change durable underneath
    assert s.get_cooled_until("t", "d") == 999.0  # served from L1 cache


def test_counts_are_l1_only_no_double_count():
    durable = _CountingDurable()
    s = WriteBehindCooldownStore(durable, flush_threshold=1)  # flush every op
    s.incr("t", "d", 5, success=False)  # L1=1 fail, flushed to durable
    s.incr("t", "d", 5, success=False)  # L1=2 fail, flushed
    # get_counts is L1-authoritative (not L1+durable) -> no double count
    assert s.get_counts("t", "d", 5) == (0, 2)


def test_survives_restart_via_durable(tmp_path):
    path = str(tmp_path / "cd.sqlite")
    durable1 = SqliteCooldownStore(path)
    s1 = WriteBehindCooldownStore(durable1, flush_threshold=1)
    s1.set_cooldown("t", "d", 4242.0)
    durable1.close()
    # "restart": new durable over same file + fresh write-behind
    durable2 = SqliteCooldownStore(path)
    s2 = WriteBehindCooldownStore(durable2)
    assert s2.get_cooled_until("t", "d") == 4242.0  # recovered from durable
    durable2.close()


def test_drop_in_for_cooldown_manager():
    durable = _CountingDurable()
    mgr = CooldownManager(WriteBehindCooldownStore(durable, flush_threshold=1))
    d = mgr.record_failure("dep", normalize_error(status_code=429), 1000.0, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert mgr.is_cooled("dep", 1000.0) is True
    assert durable.writes > 0  # write-behind reached durable
