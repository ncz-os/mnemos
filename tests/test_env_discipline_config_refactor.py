"""Prove the env-discipline refactor kept behaviour identical.

``lint:env-discipline`` (``.gitlab-ci.yml``) requires every runtime env read
to live in ``mnemos/core/config.py`` (or ``mnemos/installer/``). This test
pins the contract for the settings moved there in this change:

* MCP streamable-HTTP session limits — read by ``mnemos/mcp/http.py`` via
  the ``mnemos.core.config`` accessors.
* ``MNEMOS_REPLICA_MODE`` / ``MNEMOS_PRIMARY_URL`` — read by
  ``mnemos/core/replica_mode.py`` via the ``mnemos.core.config`` accessors.

Each case sets the real process environment and asserts the config value is
exactly what the pre-refactor inline ``os.getenv``/``os.environ`` code
would have produced (same names, same defaults, same clamping /
strip-and-fallback semantics).
"""

from __future__ import annotations


from mnemos.core import config
from mnemos.core import replica_mode


# ────────────────────────────────────────────────────────────────────────────
# MCP streamable-HTTP session settings (mnemos/mcp/http.py)
# ────────────────────────────────────────────────────────────────────────────


def _clear_mcp_session_env(monkeypatch) -> None:
    for name in (
        "MNEMOS_MCP_MAX_SESSIONS",
        "MNEMOS_MCP_MAX_PRINCIPAL_SESSIONS",
        "MNEMOS_MCP_MAX_INITIAL_BODY_BYTES",
        "MNEMOS_MCP_SESSION_IDLE_SECONDS",
        "MNEMOS_MCP_SESSION_STARTUP_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_mcp_session_settings_defaults_match_pre_refactor(monkeypatch):
    """Unset vars produce the documented defaults (256 / 16 / 1MiB / 900 / 10)."""
    _clear_mcp_session_env(monkeypatch)
    assert config.mcp_max_sessions_env() == 256
    assert config.mcp_max_principal_sessions_env() == 16
    assert config.mcp_max_initial_body_bytes_env() == 1048576
    assert config.mcp_session_idle_seconds_env() == 900.0
    assert config.mcp_session_startup_seconds_env() == 10.0


def test_mcp_session_settings_env_overrides(monkeypatch):
    _clear_mcp_session_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MCP_MAX_SESSIONS", "64")
    monkeypatch.setenv("MNEMOS_MCP_MAX_PRINCIPAL_SESSIONS", "8")
    monkeypatch.setenv("MNEMOS_MCP_MAX_INITIAL_BODY_BYTES", "2048")
    monkeypatch.setenv("MNEMOS_MCP_SESSION_IDLE_SECONDS", "300")
    monkeypatch.setenv("MNEMOS_MCP_SESSION_STARTUP_SECONDS", "5")

    assert config.mcp_max_sessions_env() == 64
    assert config.mcp_max_principal_sessions_env() == 8
    assert config.mcp_max_initial_body_bytes_env() == 2048
    assert config.mcp_session_idle_seconds_env() == 300.0
    assert config.mcp_session_startup_seconds_env() == 5.0


def test_mcp_session_settings_clamps_match_pre_refactor(monkeypatch):
    """The inline code clamped ints to >= 1 and floats to >= 0.01; keep that.

    Pre-refactor, ``int("")`` / ``float("")`` raised ``ValueError`` — those
    crashes are preserved exactly (not "improved").
    """
    _clear_mcp_session_env(monkeypatch)

    # Lower-bound clamping.
    monkeypatch.setenv("MNEMOS_MCP_MAX_SESSIONS", "0")
    assert config.mcp_max_sessions_env() == 1
    monkeypatch.setenv("MNEMOS_MCP_MAX_PRINCIPAL_SESSIONS", "-3")
    assert config.mcp_max_principal_sessions_env() == 1
    monkeypatch.setenv("MNEMOS_MCP_MAX_INITIAL_BODY_BYTES", "0")
    assert config.mcp_max_initial_body_bytes_env() == 1
    monkeypatch.setenv("MNEMOS_MCP_SESSION_IDLE_SECONDS", "0")
    assert config.mcp_session_idle_seconds_env() == 0.01
    monkeypatch.setenv("MNEMOS_MCP_SESSION_STARTUP_SECONDS", "-1")
    assert config.mcp_session_startup_seconds_env() == 0.01

    # Garbage values raised ValueError before the refactor; they must still.
    monkeypatch.setenv("MNEMOS_MCP_MAX_SESSIONS", "not-a-number")
    try:
        config.mcp_max_sessions_env()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-integer session count")

    monkeypatch.setenv("MNEMOS_MCP_SESSION_IDLE_SECONDS", "not-a-number")
    try:
        config.mcp_session_idle_seconds_env()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-float idle seconds")


def test_mcp_session_settings_empty_string_still_raises(monkeypatch):
    """``MNEMOS_MCP_MAX_SESSIONS=`` meant ``int("")`` -> ValueError pre-refactor."""
    _clear_mcp_session_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MCP_MAX_SESSIONS", "")
    try:
        config.mcp_max_sessions_env()
    except ValueError:
        pass
    else:
        raise AssertionError("empty value must still raise ValueError")


def test_mcp_http_manager_reads_config(monkeypatch):
    """The HTTP session manager resolves its limits through config.py, not
    direct ``os.getenv`` calls."""
    _clear_mcp_session_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_MCP_MAX_SESSIONS", "7")
    monkeypatch.setenv("MNEMOS_MCP_MAX_PRINCIPAL_SESSIONS", "3")
    monkeypatch.setenv("MNEMOS_MCP_MAX_INITIAL_BODY_BYTES", "128")
    monkeypatch.setenv("MNEMOS_MCP_SESSION_IDLE_SECONDS", "42")
    monkeypatch.setenv("MNEMOS_MCP_SESSION_STARTUP_SECONDS", "2")

    from tests.test_runtime_validation import _fresh_http

    http = _fresh_http(monkeypatch)
    manager = http._StreamableHTTPMCP()
    assert manager.max_sessions == 7
    assert manager.max_principal_sessions == 3
    assert manager.max_body_bytes == 128
    assert manager.idle_seconds == 42.0
    assert manager.startup_seconds == 2.0
    manager._closing = True  # nothing to shut down; construction only


# ────────────────────────────────────────────────────────────────────────────
# Replica mode switch + primary URL (mnemos/core/replica_mode.py)
# ────────────────────────────────────────────────────────────────────────────


def _clear_replica_env(monkeypatch) -> None:
    monkeypatch.delenv("MNEMOS_REPLICA_MODE", raising=False)
    monkeypatch.delenv("MNEMOS_PRIMARY_URL", raising=False)


def test_replica_mode_config_defaults_auto(monkeypatch):
    _clear_replica_env(monkeypatch)
    assert config.replica_mode_env() == "auto"


def test_replica_mode_config_valid_values(monkeypatch):
    _clear_replica_env(monkeypatch)
    for value, expected in (("auto", "auto"), ("on", "on"), ("off", "off")):
        monkeypatch.setenv("MNEMOS_REPLICA_MODE", value)
        assert config.replica_mode_env() == expected


def test_replica_mode_config_case_insensitive(monkeypatch):
    _clear_replica_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "  On  ")
    assert config.replica_mode_env() == "on"


def test_replica_mode_config_empty_value_warns_and_falls_back(monkeypatch, caplog):
    """``MNEMOS_REPLICA_MODE=`` (whitespace) warns "empty value" -> ``auto``."""
    _clear_replica_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "   ")
    with caplog.at_level("WARNING", logger="mnemos.core.config"):
        assert config.replica_mode_env() == "auto"
    assert any("empty value" in r.message for r in caplog.records)


def test_replica_mode_config_unknown_value_warns_and_falls_back(monkeypatch, caplog):
    """Unknown values warn and fall back to ``auto``."""
    _clear_replica_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "banana")
    with caplog.at_level("WARNING", logger="mnemos.core.config"):
        assert config.replica_mode_env() == "auto"
    assert any("banana" in r.message for r in caplog.records)


def test_replica_mode_wrapper_delegates_to_config(monkeypatch, caplog):
    """``mnemos.core.replica_mode.replica_mode_env`` is a thin wrapper over
    ``mnemos.core.config.replica_mode_env`` — same values, same warnings.
    """
    _clear_replica_env(monkeypatch)

    assert replica_mode.replica_mode_env() == "auto"
    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "on")
    assert replica_mode.replica_mode_env() == "on"

    monkeypatch.setenv("MNEMOS_REPLICA_MODE", "")
    with caplog.at_level("WARNING", logger="mnemos.core.config"):
        assert replica_mode.replica_mode_env() == "auto"
    assert any("empty value" in r.message for r in caplog.records)


def test_primary_url_config_strip_and_none(monkeypatch):
    """``MNEMOS_PRIMARY_URL`` is stripped; blank/unset -> ``None``."""
    _clear_replica_env(monkeypatch)

    # Unset.
    assert config.replica_primary_url_env() is None

    # Whitespace-only -> None (same ``value or None`` semantics).
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "   ")
    assert config.replica_primary_url_env() is None

    # Real value is stripped.
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "  http://primary.local  ")
    assert config.replica_primary_url_env() == "http://primary.local"


def test_primary_url_wrapper_delegates_to_config(monkeypatch):
    """``replica_mode.primary_url()`` delegates to config with identical
    strip / ``None`` semantics."""
    _clear_replica_env(monkeypatch)

    assert replica_mode.primary_url() is None
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "https://primary.example.test")
    assert replica_mode.primary_url() == "https://primary.example.test"
    monkeypatch.setenv("MNEMOS_PRIMARY_URL", "   ")
    assert replica_mode.primary_url() is None


# ────────────────────────────────────────────────────────────────────────────
# Static pin: no direct ``os.environ`` / ``os.getenv`` in the refactored modules
# ────────────────────────────────────────────────────────────────────────────


def test_no_direct_env_reads_in_refactored_modules():
    """The env-discipline lint allows direct reads ONLY in config.py and
    installer/. Pin that here so the refactor cannot silently regress."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("mnemos/mcp/http.py", "mnemos/core/replica_mode.py"):
        source = (repo_root / rel).read_text(encoding="utf-8")
        assert "os.getenv" not in source, f"{rel}: direct os.getenv read"
        assert "os.environ" not in source, f"{rel}: direct os.environ read"
