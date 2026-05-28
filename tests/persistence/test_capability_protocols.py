from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mnemos.persistence.base import (
    ALL_CAPABILITIES,
    AuditPersistence,
    ConsultationsPersistence,
    CorePersistence,
    FederationPersistence,
    OAuthPersistence,
    SessionsPersistence,
    StatePersistence,
)
from mnemos.persistence.db2 import Db2Backend
from mnemos.persistence.oracle import OracleBackend
from mnemos.persistence.postgres import PostgresBackend
from mnemos.persistence.sqlite import SqliteBackend


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="compat"))


def _protocol_results(backend: object) -> dict[str, bool]:
    return {
        "core": isinstance(backend, CorePersistence),
        "oauth": isinstance(backend, OAuthPersistence),
        "sessions": isinstance(backend, SessionsPersistence),
        "consultations": isinstance(backend, ConsultationsPersistence),
        "federation": isinstance(backend, FederationPersistence),
        "audit": isinstance(backend, AuditPersistence),
        "state": isinstance(backend, StatePersistence),
    }


def test_sqlite_backend_advertises_all_capabilities(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "mnemos.db", _settings())

    assert backend.capabilities == set(ALL_CAPABILITIES)
    assert _protocol_results(backend) == {capability: True for capability in ALL_CAPABILITIES}


def test_postgres_backend_advertises_all_capabilities() -> None:
    backend = PostgresBackend(pool=object(), settings=_settings())

    assert backend.capabilities == set(ALL_CAPABILITIES)
    assert _protocol_results(backend) == {capability: True for capability in ALL_CAPABILITIES}


def test_oracle_backend_advertises_all_capabilities() -> None:
    backend = OracleBackend(pool=object(), settings=_settings())

    assert backend.capabilities == set(ALL_CAPABILITIES)
    assert _protocol_results(backend) == {capability: True for capability in ALL_CAPABILITIES}


def test_db2_backend_advertises_all_capabilities() -> None:
    backend = Db2Backend(pool=object(), settings=_settings())

    assert backend.capabilities == set(ALL_CAPABILITIES)
    assert _protocol_results(backend) == {capability: True for capability in ALL_CAPABILITIES}
