#!/usr/bin/env python3
"""
Resolve the canonical PostgreSQL migration list from
``mnemos/installer/db.py`` (the single source of truth, also pinned by
``tests/test_migration_lists_sync.py``) and apply each file against the
configured DSN via psql.

F18 fix: the previous GitLab CI integration job hardcoded
``db/migrations.sql`` and friends, but the repo layout has not had a
top-level ``db/`` directory for several versions — the real paths live
under ``mnemos/db_migrations/``. The old job's
``if [ -f "$f" ]; then ... else echo "skip $f (not present)"; fi``
silently skipped every file, so the test always passed against an
empty database. The parity check job (which used
``tests.test_migration_lists_sync.EXPECTED_MIGRATIONS`` with a ``db/``
prefix) hit the same stale paths and FAILED on the first missing file
— inconsistent failure modes for the same underlying bug.

This script:

  1. Imports ``mnemos.installer.db`` and pulls its ``migration_files``
     list (the same one ``tests/test_migration_lists_sync.py`` pins).
  2. Verifies every path exists on disk. ANY missing path is a hard
     failure — silently skipping a real check is worse than a job
     that's a little too strict.
  3. Streams the basenames through psql with ``ON_ERROR_STOP=1`` so the
     first SQL error also aborts the whole apply sequence.

Usage from CI:

    python3 scripts/ci_apply_postgres_migrations.py \\
        --dsn "postgresql://postgres:test@postgres:5432/mnemos_test"

Exit code 0 on success, 1 if any path is missing or any apply fails.

This script deliberately does NOT import any mnemos Python runtime code
that needs a live pool — it is plain AST + subprocess so it can run in
the GitLab ``before_script`` before the suite starts.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _extract_migration_list(source_path: Path, func_name: str) -> list[str]:
    """Walk the AST for ``migration_files = [...]`` inside ``func_name``
    and return the list of .sql basenames.

    This is the same AST-walk shape
    ``tests.test_migration_lists_sync._extract_migration_list`` uses, so
    the CI's view of "what migrations exist" cannot drift from the
    pin-test's view.
    """
    tree = ast.parse(source_path.read_text())

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "migration_files"
                    and isinstance(stmt.value, ast.List)
                ):
                    names: list[str] = []
                    for elt in stmt.value.elts:
                        last_str: str | None = None
                        for sub in ast.walk(elt):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                if sub.value.endswith(".sql"):
                                    last_str = sub.value
                        if last_str is None:
                            raise AssertionError(
                                f"could not find .sql filename in list element: {ast.dump(elt)}"
                            )
                        names.append(last_str)
                    return names
    raise AssertionError(f"no migration_files list found in {source_path}::{func_name}")


def _resolve_under_db_migrations(basenames: list[str]) -> list[Path]:
    """Translate each basename into its on-disk path.

    The installer builds paths like
    ``repo_path / "mnemos" / "db_migrations" / "<file>.sql"`` for the
    pre-numbered flat files and
    ``repo_path / "mnemos" / "db_migrations" / "migrations" / "<file>.sql"``
    for the numbered-series files. We mirror that resolution so the CI
    path list matches the installer's path list exactly.
    """
    db_migrations_root = REPO / "mnemos" / "db_migrations"
    resolved: list[Path] = []
    for name in basenames:
        if name[0].isdigit():
            # numbered-series files live under mnemos/db_migrations/migrations/
            p = db_migrations_root / "migrations" / name
        else:
            p = db_migrations_root / name
        resolved.append(p)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--dsn",
        required=True,
        help="Postgres DSN, e.g. postgresql://postgres:test@postgres:5432/mnemos_test",
    )
    parser.add_argument(
        "--installer",
        default=str(REPO / "mnemos" / "installer" / "db.py"),
        help="Path to the installer module (default: mnemos/installer/db.py).",
    )
    args = parser.parse_args()

    installer = Path(args.installer).resolve()
    if not installer.exists():
        print(f"[migrate] installer not found at {installer}", file=sys.stderr)
        return 1

    basenames = _extract_migration_list(installer, "run_migrations")
    paths = _resolve_under_db_migrations(basenames)

    # F18: hard-fail on missing paths. The pre-fix CI silently skipped
    # every file because it referenced a non-existent ``db/`` directory;
    # this script makes that a CI error instead of a green light.
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"[migrate] FAIL — {len(missing)} migration path(s) missing on disk:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print(
            "[migrate] The canonical migration list is computed from "
            "mnemos/installer/db.py and resolved against "
            "mnemos/db_migrations/. Add the missing files or remove the "
            "offending entry from the installer's migration_files list.",
            file=sys.stderr,
        )
        return 1

    # Apply. ON_ERROR_STOP=1 + non-zero exit on the first failure so a
    # bad migration aborts the whole apply sequence rather than
    # silently skipping the rest.
    psql_env = {**os.environ, "PGOPTIONS": "-c ON_ERROR_STOP=1"}
    print(f"[migrate] applying {len(paths)} migration(s) from {installer}")
    for p in paths:
        rel = p.relative_to(REPO)
        print(f"[migrate] applying {rel}")
        proc = subprocess.run(
            ["psql", args.dsn, "-v", "ON_ERROR_STOP=1", "-f", str(p)],
            env=psql_env,
        )
        if proc.returncode != 0:
            print(
                f"[migrate] FAIL — psql returned {proc.returncode} applying {rel}",
                file=sys.stderr,
            )
            return 1

    print(f"[migrate] OK — applied {len(paths)} migration(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
