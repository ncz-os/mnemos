"""Every advertised OAuth backend implements the entire repository contract."""

import inspect
from types import SimpleNamespace

import pytest

from mnemos.persistence.base import OAUTH_CAPABILITY, OAuthPersistence, OAuthRepository
from mnemos.persistence.db2 import Db2Backend, Db2BackendNative
from mnemos.persistence.mariadb import MariadbBackend
from mnemos.persistence.mysql import MysqlBackend
from mnemos.persistence.oracle import OracleBackend
from mnemos.persistence.postgres import PostgresBackend
from mnemos.persistence.sqlite import SqliteBackend


@pytest.mark.parametrize(
    "backend_class",
    [SqliteBackend, PostgresBackend, OracleBackend, MysqlBackend, MariadbBackend, Db2Backend, Db2BackendNative],
)
def test_oauth_backend_implements_entire_abstract_contract(backend_class, tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="compat"))
    if backend_class is SqliteBackend:
        backend = backend_class(tmp_path / "contract.db", settings)
    else:
        backend = backend_class(pool=object(), settings=settings)
    assert isinstance(backend, OAuthPersistence)
    assert OAUTH_CAPABILITY in backend.capabilities
    repository = backend.oauth
    assert isinstance(repository, OAuthRepository)
    assert not type(repository).__abstractmethods__
    for method in OAuthRepository.__abstractmethods__:
        assert inspect.iscoroutinefunction(getattr(repository, method)), method
