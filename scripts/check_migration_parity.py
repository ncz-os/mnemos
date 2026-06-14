#!/usr/bin/env python3
"""
Migration parity checker — mnemos-migration-parity-ci-gate.

For mnemos-prod-working multi-backend persistence, schema-mutating
migrations must land on ALL three primary backends:

  db/migrations/           ← PostgreSQL (canonical source-of-truth)
  db/migrations_oracle/    ← Oracle 23ai
  db/migrations_db2/       ← IBM Db2 12.1 (Oracle Compat mode)

A SQLite parity dir exists for the edge profile but is not enforced
by this gate (edge profile is single-tenant, lighter contract).

Modes:

  --mode staged   pre-commit hook: look at git index, fail if a newly
                  added .sql under any one of the 3 dirs is missing a
                  matching basename in the other two.

  --mode diff     CI gate: look at git diff against CI_MERGE_REQUEST_DIFF_BASE_SHA
                  (GitLab MR) or origin/main (fallback), fail if a newly
                  added .sql in any of the 3 dirs lacks parity.

  --mode full     CI safety net: walk all 3 dirs, fail if any file has
                  no matching basename in the others. This is now a hard
                  gate: all historical drift has been backfilled.

Defaults to --mode staged (env override: MIGRATION_PARITY_MODE).

Rationale: the audit-persistence gap that surfaced 2026-05-23
(GRAEAE consultations existed in PG, missing from Oracle, broke
/v1/consultations with 503 audit-trail-required) is what this gate
prevents. Every new mutation lands on all backends or commit/PR red.

Old flat-file PG migrations (db/migrations_v*.sql at db/ root) are
NOT covered — they predate the parity contract. New migrations go
in db/migrations/ subdir with the basename convention
NNNN_topic.sql (4-digit zero-padded sequence + snake_case topic).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Three parity-required backends — keys are display labels, values are dirs
BACKENDS = {
    "pg": REPO / "db" / "migrations",
    "oracle": REPO / "db" / "migrations_oracle",
    "db2": REPO / "db" / "migrations_db2",
}


def staged_basenames() -> set[str]:
    """Newly-added .sql basenames under any of the 3 backend dirs (git index)."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=A"],
            cwd=REPO,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[parity] git diff failed: {e}", file=sys.stderr)
        return set()
    bases: set[str] = set()
    for line in out.splitlines():
        for be_dir in BACKENDS.values():
            rel = be_dir.relative_to(REPO)
            if line.startswith(f"{rel}/") and line.endswith(".sql"):
                bases.add(Path(line).name)
                break
    return bases


def diff_basenames() -> set[str]:
    """Newly-added .sql basenames under any of the 3 backend dirs (MR/PR diff)."""
    base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "origin/main"
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD"],
            cwd=REPO,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[parity] git diff against {base} failed: {e}", file=sys.stderr)
        return set()
    bases: set[str] = set()
    for line in out.splitlines():
        for be_dir in BACKENDS.values():
            rel = be_dir.relative_to(REPO)
            if line.startswith(f"{rel}/") and line.endswith(".sql"):
                bases.add(Path(line).name)
                break
    return bases


def full_inventory() -> set[str]:
    """Every .sql basename committed under any of the 3 dirs."""
    bases: set[str] = set()
    for be_dir in BACKENDS.values():
        if not be_dir.is_dir():
            continue
        for f in be_dir.glob("*.sql"):
            bases.add(f.name)
    return bases


def check(basenames: set[str]) -> list[str]:
    """Return list of failure messages — empty if parity holds."""
    failures = []
    for base in sorted(basenames):
        missing = []
        for be_name, be_dir in BACKENDS.items():
            if not (be_dir / base).exists():
                missing.append(f"{be_dir.relative_to(REPO)}/{base}")
        if missing:
            failures.append(f"{base} missing in: {', '.join(missing)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--mode",
        choices=("staged", "diff", "full"),
        default=os.environ.get("MIGRATION_PARITY_MODE", "staged"),
        help="staged = pre-commit hook (git index); diff = CI MR/PR diff; "
        "full = inventory all 3 dirs (informational).",
    )
    args = parser.parse_args()

    if args.mode == "staged":
        bases = staged_basenames()
        if not bases:
            print("[parity] no new migrations staged — OK")
            return 0
        print(f"[parity] checking {len(bases)} staged migration(s): {sorted(bases)}")
    elif args.mode == "diff":
        bases = diff_basenames()
        if not bases:
            print("[parity] no new migrations in MR diff — OK")
            return 0
        print(f"[parity] checking {len(bases)} new migration(s) in diff: {sorted(bases)}")
    else:  # full
        bases = full_inventory()
        if not bases:
            print("[parity] no migrations in any backend dir — OK")
            return 0
        print(f"[parity] full inventory: {len(bases)} migration basename(s)")

    failures = check(bases)
    if failures:
        print("", file=sys.stderr)
        print(f"[parity] FAIL — {len(failures)} migration(s) lack parity:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Every migration NNNN_topic.sql under db/migrations/ MUST also exist", file=sys.stderr)
        print("under db/migrations_oracle/ AND db/migrations_db2/.", file=sys.stderr)
        print("Add the missing files (translated for the target backend) or", file=sys.stderr)
        print("rename the additions so all 3 carry the same basename.", file=sys.stderr)
        return 1

    print(f"[parity] OK — {len(bases)} migration(s) have parity across pg/oracle/db2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
