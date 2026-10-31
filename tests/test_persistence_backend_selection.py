"""Persistence backend selection (M7a).

Covers the contract that the backend a deployment runs on is decided by
what the operator actually configured:

* an explicitly configured DSN decides, and an UNKNOWN DSN scheme is a
  configuration error rather than a silent fall back to Postgres;
* an explicit backend name that conflicts with the DSN scheme logs a
  warning (the DSN still wins — it names the server to connect to);
* ``ORACLE_DSN`` / ``DB2_DSN`` select the backend AND supply the DSN, so
  startup does not fail with "no oracle:// DSN configured".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from mnemos.core import config
from mnemos.core import lifecycle


_ENV_KEYS = (
    "MNEMOS_CONFIG_PATH",
    "MNEMOS_PROFILE",
    "MNEMOS_PROFILE_OVERRIDE",
    "MNEMOS_PERSISTENCE_BACKEND",
    "PERSISTENCE_BACKEND",
    "PG_BACKEND",
    "MNEMOS_DATABASE_DSN",
    "DATABASE_DSN",
    "PG_DSN",
    "MNEMOS_DATABASE_URL",
    "DATABASE_URL",
    "PG_URL",
    "MNEMOS_SQLITE_PATH",
    "SQLITE_DB_PATH",
    "PG_SQLITE_PATH",
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
    "ORACLE_DSN",
    "DB2_DSN",
)


@contextmanager
def _isolated_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    env: dict[str, str] | None = None,
) -> Iterator[config.Settings]:
    with monkeypatch.context() as scoped:
        for key in _ENV_KEYS:
            scoped.delenv(key, raising=False)
        scoped.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        for key, value in (env or {}).items():
            scoped.setenv(key, value)
        settings = config.reload_settings()
        try:
            yield settings
        finally:
            config.reload_settings()


def test_explicit_dsn_scheme_selects_that_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_DATABASE_DSN": "oracle://u:p@host:1521/ORCL"},
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "oracle"


def test_explicit_dsn_scheme_wins_over_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={
            "MNEMOS_DATABASE_DSN": "postgres://u:p@host:5432/db",
            "MNEMOS_PERSISTENCE_BACKEND": "oracle",
        },
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "postgres"


def test_backend_name_conflicting_with_dsn_scheme_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={
            "MNEMOS_DATABASE_DSN": "postgres://u:p@host:5432/db",
            "MNEMOS_PERSISTENCE_BACKEND": "sqlite",
        },
    ) as settings:
        with caplog.at_level(logging.WARNING, logger=lifecycle.__name__):
            assert lifecycle._select_persistence_backend(settings) == "postgres"

    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("conflicts with" in message for message in warnings), warnings


def test_backend_name_conflicting_with_dsn_scheme_does_not_log_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={
            "MNEMOS_DATABASE_DSN": "postgres://user:sup3rs3cret@host:5432/db",
            "MNEMOS_PERSISTENCE_BACKEND": "sqlite",
        },
    ) as settings:
        with caplog.at_level(logging.WARNING, logger=lifecycle.__name__):
            lifecycle._select_persistence_backend(settings)

    assert "sup3rs3cret" not in caplog.text


def test_unknown_dsn_scheme_raises_instead_of_falling_back_to_postgres(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_DATABASE_DSN": "mssql://u:p@host:1433/db"},
    ) as settings:
        with pytest.raises(ValueError, match="Unsupported database DSN scheme"):
            lifecycle._select_persistence_backend(settings)


def test_unknown_dsn_scheme_in_url_field_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_DATABASE_URL": "redis://localhost:6379/0"},
    ) as settings:
        with pytest.raises(ValueError, match="Unsupported database DSN scheme"):
            lifecycle._select_persistence_backend(settings)


def test_libpq_keyword_value_dsn_still_selects_postgres(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"MNEMOS_DATABASE_DSN": "host=localhost port=5432 dbname=mnemos"},
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "postgres"


def test_oracle_dsn_env_selects_oracle_and_supplies_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    oracle_dsn = "oracle://u:p@host:1521/ORCLPDB1"
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"ORACLE_DSN": oracle_dsn},
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "oracle"
        assert lifecycle._oracle_dsn_from_settings(settings) == oracle_dsn


def test_db2_dsn_env_selects_db2_and_supplies_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db2_dsn = "db2://u:p@host:50000/MNEMOS"
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={"DB2_DSN": db2_dsn},
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "db2"
        assert lifecycle._db2_dsn_from_settings(settings) == db2_dsn


def test_configured_oracle_dsn_wins_over_oracle_dsn_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    configured = "oracle://u:p@host:1521/CONFIGURED"
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={
            "ORACLE_DSN": "oracle://u:p@host:1521/ENV",
            "MNEMOS_DATABASE_DSN": configured,
        },
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "oracle"
        assert lifecycle._oracle_dsn_from_settings(settings) == configured


def test_configured_db2_dsn_wins_over_db2_dsn_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    configured = "db2://u:p@host:50000/CONFIGURED"
    with _isolated_settings(
        monkeypatch,
        tmp_path,
        env={
            "DB2_DSN": "db2://u:p@host:50000/ENV",
            "MNEMOS_DATABASE_DSN": configured,
        },
    ) as settings:
        assert lifecycle._select_persistence_backend(settings) == "db2"
        assert lifecycle._db2_dsn_from_settings(settings) == configured


def test_no_dsn_and_no_backend_falls_back_to_profile_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "server"}) as settings:
        assert lifecycle._select_persistence_backend(settings) == "postgres"
    with _isolated_settings(monkeypatch, tmp_path, env={"MNEMOS_PROFILE": "edge"}) as settings:
        assert lifecycle._select_persistence_backend(settings) == "sqlite"
