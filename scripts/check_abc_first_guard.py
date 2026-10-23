#!/usr/bin/env python3
"""ABC-first persistence guard (backlog F-14).

Enforces the governing rule from ``docs/FEATURES_TO_BE_DONE.md`` section 0b:

    A new or repaired storage capability is delivered as:
    ABC method -> implementation on all six backends -> facade exposure
    -> route calls the ABC.

    Raw ``_lc._pool`` access outside ``mnemos/persistence/`` is the defect,
    not the shortcut.

This guard is a **ratchet**. A committed baseline
(``tests/abc_first_baseline.json``) records, per file, how many raw
``_lc._pool`` / ``_lc._raw_pool`` references it already carried. The check
FAILS when:

  * an in-baseline file's count goes UP, or
  * a file not in the baseline gains a reference,

and it PASSES when counts go DOWN or files drop to zero. Counts never
increase, so a stale baseline (e.g. left by a parallel job that removed
references first) can never fail a merge — it is silently regenerated in
``--regen`` runs and pruned on the fly by this script's report.

Usage:
  python3 scripts/check_abc_first_guard.py          # CI: compare vs baseline
  python3 scripts/check_abc_first_guard.py --regen  # rewrite the baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO / "tests" / "abc_first_baseline.json"

# Raw pool access on the lifecycle object: ``_lc._pool`` and
# ``_lc._raw_pool``. A preceding identifier character (``x_lc._pool``) is a
# different attribute chain and is excluded, as is ``_lcs._pool``-style
# pluralization.
RAW_POOL_RE = re.compile(r"(?<![\w])_lc\._(?:raw_)?pool")

TQD = '"""'
TQS = "'''"

# A bare docstring-delimiter line: whitespace, then one (or two) triple
# quotes, optionally followed by more prose on the same line.
_DOCSTRING_LINE_RE = re.compile(r"^\s*(?:\"\"\"|''')")

# Never scanned: the persistence layer itself (where pool access belongs),
# tests (fixtures), docs, venvs, build output, and the retired services
# scaffold (also excluded from ruff).
_SKIP_DIRS = frozenset(
    {
        "persistence",  # under mnemos/
        "tests",
        "docs",
        "archive",
        "venv",
        ".venv",
        "dist",
        "build",
        "services",
        "node_modules",
        ".pytest_cache",
        "__pycache__",
    }
)


def _repo_root(script_path: Path) -> Path:
    """Repo root for this script; overridable so unit tests can run the
    scan against a fixture tree without touching the real repo."""
    return script_path.resolve().parents[1]


def _count_raw_pool(text: str) -> int:
    """Count raw pool references (see ``_raw_pool_lines``)."""
    return len(_raw_pool_lines(text))


def scan_repo(root: Path) -> dict[str, list[int]]:
    """Raw ``_lc._pool`` reference line numbers per .py file under
    ``mnemos/`` (prose mentions skipped — see ``_count_raw_pool``)."""
    counts: dict[str, list[int]] = {}
    mnemos_dir = root / "mnemos"
    if not mnemos_dir.is_dir():
        return counts
    for path in sorted(mnemos_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = _raw_pool_lines(text)
        if lines:
            counts[path.relative_to(root).as_posix()] = lines
    return counts


def _raw_pool_lines(text: str) -> list[int]:
    """Line numbers (1-based) of every counted reference, with the same
    prose-skipping rules as ``_count_raw_pool``."""
    out: list[int] = []
    in_docstring = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if in_docstring:
            in_docstring -= line.count(TQD) + line.count(TQS)
            continue
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith(">>>"):
            continue
        code = line.split("#", 1)[0]
        opens = code.count(TQD) + code.count(TQS)
        if _DOCSTRING_LINE_RE.match(code):
            in_docstring = opens
            continue
        out.extend(lineno for _ in RAW_POOL_RE.findall(code))
        in_docstring = opens % 2
    return out


def check(repo: Path, baseline_path: Path) -> int:
    """Compare the live tree against the baseline; return 0 = pass."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["files"]
    live = scan_repo(repo)

    problems: list[str] = []
    for f, lines in sorted(live.items()):
        count = len(lines)
        allowed = baseline.get(f)
        if allowed is None:
            problems.append(
                f"NEW: {f} has {count} raw _lc._pool reference(s) but is not in the baseline "
                + "(line(s): " + ", ".join(map(str, lines)) + ")"
            )
        elif count > allowed:
            problems.append(
                f"{f}: {count} raw _lc._pool references, baseline allows {allowed} "
                + "(line(s): " + ", ".join(map(str, lines)) + ")"
            )
    return _report(live, baseline, problems)


def _report(live: dict[str, list[int]], baseline: dict[str, int], problems: list[str]) -> int:
    for f, lines in sorted(live.items()):
        count = len(lines)
        allowed = baseline.get(f)
        if allowed is None:
            tag = "NEW"
        elif count > allowed:
            tag = "OVER"
        elif count < allowed:
            tag = "below baseline (good — re-run with --regen to record the lower count)"
        else:
            tag = "at baseline"
        print(f"  {f}: {count} (baseline {allowed if allowed is not None else 'absent'}) — {tag}")
    removed = sorted(set(baseline) - set(live))
    if removed:
        print(
            "  files reduced to zero (re-run with --regen to prune the baseline): "
            + ", ".join(removed)
        )
    if problems:
        print("\nABC-first guard FAILED — raw _lc._pool references outside mnemos/persistence/:")
        for p in problems:
            print(f"  {p}")
        print(
            "\nThe rule (docs/FEATURES_TO_BE_DONE.md §0b): deliver storage work as "
            "ABC method -> all six backends -> facade exposure -> route calls the ABC. "
            "Do not add raw pool references; route through the persistence ABC instead."
        )
        return 1
    print("ABC-first guard passed: no increase in raw _lc._pool references outside mnemos/persistence/.")
    return 0


def regen(repo: Path, baseline_path: Path) -> int:
    """Rewrite the baseline to the current tree state."""
    live = scan_repo(repo)
    data = {
        "_comment": (
            "ABC-first guard (F-14) baseline: per-file raw _lc._pool / "
            "_lc._raw_pool reference counts OUTSIDE mnemos/persistence/. "
            "Ratchet — lower a count when a reference is removed; never raise one. "
            "Regenerate with: python3 scripts/check_abc_first_guard.py --regen"
        ),
        "files": {k: len(live[k]) for k in sorted(live)},
    }
    baseline_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in live.values())
    print(f"baseline regenerated: {len(live)} files, {total} references total")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ABC-first persistence guard (F-14).")
    parser.add_argument(
        "--regen",
        action="store_true",
        help="rewrite the baseline to the current tree state instead of checking",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="repository root to scan (default: this script's repo root)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="baseline file (default: tests/abc_first_baseline.json under the repo)",
    )
    args = parser.parse_args(argv)

    repo = (args.repo or _repo_root(Path(__file__))).resolve()
    baseline_path = (args.baseline or (repo / "tests" / "abc_first_baseline.json")).resolve()
    if args.regen:
        return regen(repo, baseline_path)
    if not baseline_path.is_file():
        print(
            f"ABC-first guard FAILED — baseline {baseline_path} is missing. "
            f"Create it with: python3 scripts/check_abc_first_guard.py --regen"
        )
        return 1
    return check(repo, baseline_path)


if __name__ == "__main__":
    sys.exit(main())
