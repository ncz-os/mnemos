"""Named non-conformance for the model-registry contract (phase 1.5).

Phase 1.5 ships ``ModelRegistryRepository`` (``mnemos.persistence.base``) with
a full SQLite implementation and a partial Postgres one. Oracle, DB2, MySQL
and MariaDB do not have the physical tables the contract needs yet — the
Oracle ``model_registry`` write path is Oracle-specific SQL (``ROWNUM``,
``:named`` binds) and none of those four backends has price BANDS, per-field
observations, benchmarks or versioned curated routing.

That gap is made EXPLICIT here rather than hidden behind ``NotImplementedError``
defaults on the ABC: each class below subclasses
:class:`mnemos.persistence.base.UnsupportedModelRegistryRepository`, names
itself in ``backend_name``, and raises
:class:`mnemos.persistence.base.ModelRegistryNotSupported` from every
operation. The contract suite parametrises over these names and skips with
``unsupported backend: <name> — phase 2`` — a visible, greppable reason, not a
silent pass.

Phase 2 is expected to delete these classes one at a time as each backend's
DDL and implementation land.
"""

from __future__ import annotations

from mnemos.persistence.base import UnsupportedModelRegistryRepository


class OracleModelRegistryRepository(UnsupportedModelRegistryRepository):
    """Oracle registry — phase 2.

    Phase 2 lands the band-aware ``model_prices``, ``model_field_obs``,
    ``model_benchmarks`` and versioned ``curated_routing`` DDL alongside a
    translation of the existing ``OracleConsultationAuditRepository.upsert_model``
    / ``upsert_model_pricing`` write path onto the ABC. The phase-1.5 rule is
    strict: no Oracle DDL and no behaviour change to the existing path.
    """

    backend_name = "oracle"


class Db2ModelRegistryRepository(UnsupportedModelRegistryRepository):
    """IBM Db2 registry — phase 2 (rides the Oracle port)."""

    backend_name = "db2"


class MysqlModelRegistryRepository(UnsupportedModelRegistryRepository):
    """MySQL registry — phase 2."""

    backend_name = "mysql"


class MariadbModelRegistryRepository(MysqlModelRegistryRepository):
    """MariaDB registry — phase 2 (inherits the MySQL non-conformance)."""

    backend_name = "mariadb"


__all__ = [
    "Db2ModelRegistryRepository",
    "MariadbModelRegistryRepository",
    "MysqlModelRegistryRepository",
    "OracleModelRegistryRepository",
]
