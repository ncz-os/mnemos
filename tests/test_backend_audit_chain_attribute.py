"""Every backend must answer ``audit_chain``, even if the answer is None.

``mnemos/domain/federation.py`` guards its audit write with

    if mutation_applied and backend is not None and backend.audit_chain is not None:

which only works if the attribute exists. The persistence backends are bare
classes rather than subclasses of the ABC that declares the property, so each
one has to define it. SqliteBackend, PostgresBackend and OracleBackend do;
Db2Backend inherits OracleBackend's. MysqlBackend defined neither, so
MariadbBackend raised

    AttributeError: 'MariadbBackend' object has no attribute 'audit_chain'

and every federation sync that applied a mutation returned HTTP 503. Measured
on a live MariaDB host: it could not pull a single memory.

The base class documents ``None`` as the correct answer for a backend that has
not shipped the audit-chain rows, so returning None is the fix -- not raising.
"""

from __future__ import annotations

import pytest

BACKENDS = [
    ("sqlite", "mnemos.persistence.sqlite", "SqliteBackend"),
    ("postgres", "mnemos.persistence.postgres", "PostgresBackend"),
    ("oracle", "mnemos.persistence.oracle", "OracleBackend"),
    ("db2", "mnemos.persistence.db2", "Db2Backend"),
    ("mysql", "mnemos.persistence.mysql", "MysqlBackend"),
    ("mariadb", "mnemos.persistence.mariadb", "MariadbBackend"),
]


def _load(module: str, name: str):
    mod = pytest.importorskip(module)
    return getattr(mod, name)


@pytest.mark.parametrize(("label", "module", "name"), BACKENDS, ids=[b[0] for b in BACKENDS])
def test_backend_class_exposes_audit_chain(label, module, name):
    """The attribute must resolve on the class, without instantiating."""
    cls = _load(module, name)
    assert hasattr(cls, "audit_chain"), (
        f"{name} has no audit_chain; federation's "
        "`backend.audit_chain is not None` guard will raise AttributeError"
    )


@pytest.mark.parametrize(("label", "module", "name"), BACKENDS, ids=[b[0] for b in BACKENDS])
def test_audit_chain_is_a_property_not_a_method(label, module, name):
    """Federation reads it as an attribute; a plain method would be truthy.

    A bound method is never None, so `is not None` would pass and the code
    would go on to call repository methods on the method object.
    """
    cls = _load(module, name)
    assert isinstance(getattr(cls, "audit_chain", None), property), (
        f"{name}.audit_chain must be a property, or the federation guard "
        "silently takes the wrong branch"
    )


def test_mysql_family_reports_none_rather_than_raising():
    """The MySQL family ships no audit chain; None is the documented answer."""
    mysql = _load("mnemos.persistence.mysql", "MysqlBackend")
    mariadb = _load("mnemos.persistence.mariadb", "MariadbBackend")
    for cls in (mysql, mariadb):
        inst = cls.__new__(cls)  # no DSN/pool needed to read the property
        assert cls.audit_chain.fget(inst) is None
