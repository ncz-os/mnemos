"""MariaDB must not emit ``CAST(x AS JSON)``.

MySQL has a real JSON type and accepts the cast. MariaDB does not implement it
at all -- its JSON is an alias for LONGTEXT with a ``json_valid()`` CHECK -- and
raises:

    (1064, "You have an error in your SQL syntax; check the manual that
     corresponds to your MariaDB server version for the right syntax to use
     near 'JSON),\\n CAST(NULL AS JSON), 1, 300, 'strict', ...' at line 6")

The MariaDB federation repository inherits its SQL from the MySQL one, so it
inherited the cast. Every ``POST /v1/federation/peers`` returned HTTP 500,
which meant a MariaDB node could not be given a peer and therefore could never
federate at all. Found on a live MariaDB 11 host.

There is no MariaDB in unit CI, so these tests pin the emitted SQL fragments
rather than executing them.
"""

from __future__ import annotations

import inspect

from mnemos.persistence.mariadb import MariadbFederationRepository
from mnemos.persistence.mysql import MysqlFederationRepository


def test_mariadb_binds_json_without_a_cast():
    """The cast MariaDB rejects must not appear in its bind fragment."""
    assert "CAST" not in MariadbFederationRepository._JSON_BIND.upper(), (
        "MariaDB cannot CAST(... AS JSON): " + MariadbFederationRepository._JSON_BIND
    )
    assert MariadbFederationRepository._JSON_BIND == "%s"


def test_mariadb_reads_json_columns_without_a_cast():
    expr = MariadbFederationRepository._JSON_METADATA_EXPR
    assert "AS JSON" not in expr.upper(), "MariaDB cannot cast to JSON: " + expr
    assert "NULLIF(metadata" in expr, "the empty-string guard must survive: " + expr


def test_mysql_keeps_the_explicit_cast():
    """MySQL has a real JSON type; the fix must not regress it."""
    assert MysqlFederationRepository._JSON_BIND == "CAST(%s AS JSON)"
    assert "AS JSON" in MysqlFederationRepository._JSON_METADATA_EXPR.upper()


def test_the_two_dialects_actually_differ():
    """A future edit must not collapse them back to one shared fragment."""
    assert MariadbFederationRepository._JSON_BIND != MysqlFederationRepository._JSON_BIND
    assert (
        MariadbFederationRepository._JSON_METADATA_EXPR
        != MysqlFederationRepository._JSON_METADATA_EXPR
    )


def test_no_hardcoded_json_cast_remains_in_the_peer_sql():
    """create_peer / update_peer must go through the overridable fragment.

    Asserting on the source is deliberate: a literal ``CAST(%s AS JSON)`` left
    inline would be invisible to the subclass override and would fail only on
    a live MariaDB, which CI does not have.
    """
    for method in (
        MysqlFederationRepository.create_peer,
        MysqlFederationRepository.update_peer,
    ):
        src = inspect.getsource(method)
        assert "CAST(%s AS JSON)" not in src, (
            f"{method.__qualname__} hardcodes the cast instead of using _JSON_BIND"
        )
        assert "_JSON_BIND" in src, f"{method.__qualname__} must use the dialect fragment"
