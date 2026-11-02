"""M12 federation-peer columns must use each backend's native DDL types.

A single schema change (the per-peer lag/health + embedder-fingerprint
columns on ``federation_peers``) lands in two places per backend: the
fresh-provision ``0001``/v3 file and the incremental ``0071`` migration.
Those two files are written by hand in five different SQL dialects, so it
is easy for a copy-paste to leave a type that the target engine rejects.

Oracle has no ``TEXT``/``VARCHAR``/``INT``; PostgreSQL has no ``VARCHAR2``
or ``NUMBER``. A stray type only shows up the first time someone provisions
that dialect from scratch -- exactly the path no unit test exercises unless
we parse the migration files directly, which is what this module does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "mnemos" / "db_migrations"

# The M12 columns, and the type each dialect is allowed to give them.
# Anything outside the allowed set means a foreign dialect's type leaked in.
_M12_COLUMNS = {
    "last_cursor_ts",
    "lag_seconds",
    "rows_last_sync",
    "consecutive_failures",
    "peer_embed_model",
    "peer_embed_dim",
    "embedding_status",
}

_ALLOWED = {
    "oracle": {
        "last_cursor_ts": {"TIMESTAMP WITH TIME ZONE", "TIMESTAMP"},
        "lag_seconds": {"BINARY_DOUBLE"},
        "rows_last_sync": {"NUMBER"},
        "consecutive_failures": {"NUMBER"},
        "peer_embed_model": {"VARCHAR2"},
        "peer_embed_dim": {"NUMBER"},
        "embedding_status": {"VARCHAR2", "CLOB"},
    },
    "postgres": {
        "last_cursor_ts": {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"},
        "lag_seconds": {"DOUBLE PRECISION"},
        "rows_last_sync": {"INTEGER", "BIGINT"},
        "consecutive_failures": {"INTEGER", "BIGINT"},
        "peer_embed_model": {"TEXT", "VARCHAR", "CHARACTER VARYING"},
        "peer_embed_dim": {"INTEGER", "BIGINT"},
        "embedding_status": {"TEXT", "VARCHAR", "CHARACTER VARYING"},
    },
}


def _column_types(sql: str) -> dict[str, str]:
    """Return {lowercased_column: UPPERCASED_TYPE} for the M12 columns found."""
    found: dict[str, str] = {}
    for line in sql.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith("--"):
            continue
        # Drop a trailing comment if present.
        stripped = stripped.split("--", 1)[0].strip().rstrip(",")
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$", stripped)
        if not match:
            continue
        name, rest = match.group(1).lower(), match.group(2)
        if name not in _M12_COLUMNS:
            continue
        # First token(s) that make up the type; keep it simple by taking the
        # type prefix up to the first constraint keyword.
        type_text = rest.upper()
        for stop in (" NOT NULL", " NULL", " DEFAULT", " WITH DEFAULT", " PRIMARY"):
            type_text = type_text.split(stop, 1)[0]
        found[name] = type_text.strip()
    return found


def _type_ok(declared: str, allowed: set[str]) -> bool:
    """True if ``declared`` is one of ``allowed`` ignoring length/precision.

    ``VARCHAR2(200)`` -> ``VARCHAR2``; ``DOUBLE PRECISION`` stays two words.
    """
    normalized = declared.upper()
    # Strip a trailing type modifier like (200) or (6,2).
    normalized = re.sub(r"\([0-9,\s]*\)", "", normalized).strip()
    for candidate in allowed:
        if normalized == candidate or normalized.startswith(candidate + " "):
            return True
    return False


_ORACLE_FILES = [
    MIGRATIONS / "migrations_oracle" / "0001_core_schema.sql",
    MIGRATIONS / "migrations_oracle" / "0071_federation_replica_health.sql",
]

_POSTGRES_FILES = [
    MIGRATIONS / "migrations_v3_federation.sql",
    MIGRATIONS / "migrations" / "0071_federation_replica_health.sql",
]


@pytest.mark.parametrize("path", _ORACLE_FILES)
def test_oracle_m12_columns_use_oracle_types(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    types = _column_types(path.read_text())
    if not types:
        pytest.skip(f"{path.name} does not define M12 columns")
    for column, declared in types.items():
        allowed = _ALLOWED["oracle"][column]
        assert _type_ok(declared, allowed), (
            f"{path.name}: {column} declared as {declared!r} -- Oracle has no "
            f"such type; expected one of {sorted(allowed)}"
        )
    # Guard against foreign-dialect leakage: Oracle never accepts these types.
    body = path.read_text().upper()
    foreign = ("TEXT", "VARCHAR(", "VARCHAR (")
    for column in types:
        for bad in foreign:
            assert f"{column.upper()} {bad}" not in body, (
                f"{path.name}: {column} uses non-Oracle type {bad.strip(' (')!r}"
            )
        # Bare INT / INTEGER is not Oracle; NUMBER is.
        assert not re.search(rf"{column.upper()}\s+(INT|INTEGER)\b", body), (
            f"{path.name}: {column} uses non-Oracle INT/INTEGER; use NUMBER"
        )


@pytest.mark.parametrize("path", _POSTGRES_FILES)
def test_postgres_m12_columns_use_postgres_types(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    types = _column_types(path.read_text())
    if not types:
        pytest.skip(f"{path.name} does not define M12 columns")
    for column, declared in types.items():
        allowed = _ALLOWED["postgres"][column]
        assert _type_ok(declared, allowed), (
            f"{path.name}: {column} declared as {declared!r} -- PostgreSQL has "
            f"no such type; expected one of {sorted(allowed)}"
        )
    body = path.read_text().upper()
    for column in types:
        assert f"{column.upper()} VARCHAR2" not in body, (
            f"{path.name}: {column} uses Oracle's VARCHAR2 in a PostgreSQL migration"
        )
        assert not re.search(rf"{column.upper()}\s+NUMBER", body), (
            f"{path.name}: {column} uses Oracle's NUMBER in a PostgreSQL migration"
        )


def test_m12_columns_consistent_between_provision_and_upgrade_oracle() -> None:
    """0001 (fresh provision) and 0071 (upgrade) must agree on Oracle types."""
    fresh = _column_types((MIGRATIONS / "migrations_oracle" / "0001_core_schema.sql").read_text())
    upgrade = _column_types((MIGRATIONS / "migrations_oracle" / "0071_federation_replica_health.sql").read_text())
    common = set(fresh) & set(upgrade)
    mismatched = {c: (fresh[c], upgrade[c]) for c in common if fresh[c] != upgrade[c]}
    assert not mismatched, f"Oracle 0001 vs 0071 type drift: {mismatched}"
