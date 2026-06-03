"""Tests for the PANTHEON cooldown circuit-breaker."""

from __future__ import annotations

from mnemos.domain.pantheon.cooldown import (
    DEFAULT_COOLDOWN_SECONDS,
    CooldownManager,
    InMemoryCooldownStore,
    evaluate_cooldown,
)
from mnemos.domain.pantheon.errors import normalize_error

NOW = 1000.0


def _err(status=None, body=None):
    return normalize_error(status_code=status, body=body)


# ── pure evaluate_cooldown ──────────────────────────────────────────────────


def test_plain_400_never_trips():
    d = evaluate_cooldown(_err(400), successes=0, failures=10, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_api_connection_never_trips():
    d = evaluate_cooldown(
        _err(body="APIConnectionError: refused"), successes=0, failures=10, is_single_deployment_group=False
    )
    assert d.should_cooldown is False


def test_single_deployment_group_never_trips():
    d = evaluate_cooldown(_err(429), successes=0, failures=9, is_single_deployment_group=True)
    assert d.should_cooldown is False


def test_429_trips_multi():
    d = evaluate_cooldown(_err(429), successes=0, failures=1, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "rate_limit"
    assert d.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS


def test_permanent_auth_and_not_found_trip_multi():
    assert evaluate_cooldown(_err(401), successes=0, failures=1, is_single_deployment_group=False).should_cooldown
    assert evaluate_cooldown(_err(404), successes=0, failures=1, is_single_deployment_group=False).should_cooldown


def test_failure_rate_trips_over_threshold():
    # 4 fail / 6 total = 66% > 50%, total >= 5, server errors (cooldownable, not permanent)
    d = evaluate_cooldown(_err(500), successes=2, failures=4, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "failure_rate"


def test_failure_rate_not_tripped_below_min_requests():
    # 3 fail / 3 total = 100% but total < 5
    d = evaluate_cooldown(_err(500), successes=0, failures=3, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_failure_rate_not_tripped_at_exactly_half():
    # 5 fail / 10 = 50%, needs > 50%
    d = evaluate_cooldown(_err(500), successes=5, failures=5, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_custom_cooldown_seconds_passthrough():
    d = evaluate_cooldown(_err(429), successes=0, failures=1, is_single_deployment_group=False, cooldown_seconds=30)
    assert d.cooldown_seconds == 30


# ── CooldownManager + store ─────────────────────────────────────────────────


def _mgr():
    return CooldownManager(InMemoryCooldownStore())


def test_manager_trips_and_marks_cooled():
    m = _mgr()
    d = m.record_failure("gpt", _err(429), NOW, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert m.is_cooled("gpt", NOW) is True
    assert m.is_cooled("gpt", NOW + DEFAULT_COOLDOWN_SECONDS + 0.1) is False  # logical-TTL expiry


def test_manager_single_group_does_not_cool():
    m = _mgr()
    d = m.record_failure("only", _err(429), NOW, is_single_deployment_group=True)
    assert d.should_cooldown is False
    assert m.is_cooled("only", NOW) is False


def test_manager_filter_available_removes_cooled():
    m = _mgr()
    m.record_failure("b", _err(429), NOW, is_single_deployment_group=False)
    assert m.filter_available(["a", "b", "c"], NOW) == ["a", "c"]


def test_manager_failure_rate_path():
    m = _mgr()
    # 2 successes, then server-error failures in the same minute. Trips when the
    # failure rate first exceeds 50% with total >= 5:
    #   fail1: 1/3=33%; fail2: 2/4=50% (total<5 anyway); fail3: 3/5=60% -> TRIP.
    m.record_success("x", NOW)
    m.record_success("x", NOW)
    for _ in range(2):
        d = m.record_failure("x", _err(503), NOW, is_single_deployment_group=False)
        assert d.should_cooldown is False
    d = m.record_failure("x", _err(503), NOW, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "failure_rate"


def test_tenant_isolation():
    m = _mgr()
    m.record_failure("shared", _err(429), NOW, is_single_deployment_group=False, tenant="A")
    assert m.is_cooled("shared", NOW, tenant="A") is True
    assert m.is_cooled("shared", NOW, tenant="B") is False  # tenant B's key unaffected


def test_store_counts_are_minute_bucketed():
    s = InMemoryCooldownStore()
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 100, success=False)
    s.incr("t", "d", 101, success=False)
    assert s.get_counts("t", "d", 100) == (1, 1)
    assert s.get_counts("t", "d", 101) == (0, 1)
    assert s.get_counts("t", "d", 999) == (0, 0)
