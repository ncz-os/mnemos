from types import SimpleNamespace

import pytest

from mnemos.core import config, network_guard


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("  127.42.1.2  ", True),
        ("127.0.0.0/8", True),
        ("localhost", True),
        ("[::1]", True),
        ("0:0:0:0:0:0:0:1", True),
        ("", False),
        (" [] ", False),
        ("0.0.0.0", False),
        ("::", False),
        ("192.0.2.1", False),
        ("LOCALHOST", False),
        ("example.invalid", False),
        ("127.0.0.1:5002", False),
        ("127.0.0.0/16", False),
    ],
)
def test_is_loopback_bind(host, expected):
    assert network_guard.is_loopback_bind(host) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        (" 1 ", True),
        ("TrUe", True),
        (" YES ", True),
        ("on", False),
        ("0", False),
        ("true-ish", False),
    ],
)
def test_unsafe_bind_allowed(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    else:
        monkeypatch.setenv(network_guard.UNSAFE_NETWORK_BIND_ENV, value)
    assert network_guard.unsafe_bind_allowed() is expected


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (SimpleNamespace(), False),
        (SimpleNamespace(auth=None), False),
        (SimpleNamespace(auth=SimpleNamespace()), False),
        (SimpleNamespace(auth=SimpleNamespace(enabled=False)), False),
        (SimpleNamespace(auth=SimpleNamespace(enabled=True)), True),
    ],
)
def test_auth_is_enabled(monkeypatch, settings, expected):
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert network_guard.auth_is_enabled() is expected


@pytest.mark.parametrize("host", [None, "0.0.0.0", "example.invalid"])
def test_refusal_reason_fails_closed(monkeypatch, host):
    monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    monkeypatch.setattr(network_guard, "auth_is_enabled", lambda: False)
    reason = network_guard.refusal_reason(host, "test service")
    action = "start" if host is None else f"bind {host}"
    assert reason.startswith(f"test service refuses to {action} with authentication disabled:")
    assert "unauthenticated root principal" in reason
    assert "--host 127.0.0.1" in reason
    assert f"{network_guard.UNSAFE_NETWORK_BIND_ENV}=1" in reason


def test_refusal_reason_short_circuits_safe_cases(monkeypatch):
    def unexpected():
        pytest.fail("unnecessary configuration lookup")

    monkeypatch.setattr(network_guard, "auth_is_enabled", unexpected)
    monkeypatch.setattr(network_guard, "unsafe_bind_allowed", unexpected)
    assert network_guard.refusal_reason("[::1]", "service") is None
    assert network_guard.refusal_reason(None, "service", auth_enabled=True) is None


def test_refusal_reason_respects_explicit_auth_and_escape_hatch(monkeypatch):
    monkeypatch.setattr(network_guard, "auth_is_enabled", lambda: True)
    monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    assert network_guard.refusal_reason(None, "service") is None
    assert network_guard.refusal_reason(None, "service", auth_enabled=False) is not None
    monkeypatch.setenv(network_guard.UNSAFE_NETWORK_BIND_ENV, " yes ")
    assert network_guard.refusal_reason(None, "service", auth_enabled=False) is None


def test_validated_bind_host_round_trip(monkeypatch):
    monkeypatch.setenv(network_guard.BIND_CHECKED_ENV, "")
    monkeypatch.delenv(network_guard.BIND_CHECKED_ENV)
    monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    assert network_guard.validated_bind_host() is None
    network_guard.record_validated_bind("0.0.0.0")
    assert network_guard.validated_bind_host() == "0.0.0.0"
    assert network_guard.refusal_reason(network_guard.validated_bind_host(), "service", auth_enabled=False) is not None
    network_guard.record_validated_bind("127.0.0.1")
    assert network_guard.validated_bind_host() == "127.0.0.1"
    network_guard.record_validated_bind("")
    assert network_guard.validated_bind_host() is None


def test_record_validated_bind_propagates_write_error(monkeypatch):
    def fail_write(name, value):
        raise OSError("write failed")

    monkeypatch.setattr(config, "set_runtime_env_value", fail_write)
    with pytest.raises(OSError, match="write failed"):
        network_guard.record_validated_bind("127.0.0.1")
