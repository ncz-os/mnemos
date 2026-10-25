"""Unit tests for the ABC-first guard (backlog F-14).

The guard is a ratchet: ``tests/abc_first_baseline.json`` records, per file,
how many raw ``_lc._pool`` references it already carries OUTSIDE
``mnemos/persistence/``. The script fails when an in-baseline file's count
goes UP or a new file appears; it passes when counts go DOWN or drop to
zero. A stale baseline (e.g. left behind by a parallel job that removed
references first) must therefore never fail a merge.

The four cases the acceptance criterion demands:

1. clean tree (tree == baseline) passes
2. one added reference in an existing file fails
3. one added reference in a NEW file fails
4. a reduced count passes

Each case runs against a fixture repo (a temporary tree with a copy of the
guard script, resolved so a symlinked workspace root does not confuse the
script's own default repo-root detection).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Copy (not symlink) so the fixture tree is self-contained; resolve() first
# because the workspace root may itself sit behind a symlink, and the guard
# resolves its own path for the default repo root.
GUARD = Path(__file__).resolve().parents[1] / "scripts" / "check_abc_first_guard.py"


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.guard = root / "scripts" / "check_abc_first_guard.py"
        self.baseline = root / "tests" / "abc_first_baseline.json"
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        self.guard.write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "mnemos" / "persistence").mkdir(parents=True)

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def set_baseline(self, files: dict[str, int]) -> None:
        self.baseline.write_text(
            json.dumps({"_comment": "fixture", "files": files}, indent=2), encoding="utf-8"
        )

    def run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.guard), *extra],
            capture_output=True,
            text=True,
            cwd=self.root,
        )


def _one_ref() -> str:
    return "async with _lc._pool.acquire() as conn:\n    pass\n"


def _two_refs() -> str:
    return "async with _lc._pool.acquire() as conn:\n    pass\nasync with _lc._pool.acquire() as conn2:\n    pass\n"


def test_clean_tree_passes(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    fx.set_baseline({"mnemos/db/mod_a.py": 1})
    r = fx.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "passed" in r.stdout


def test_added_reference_in_existing_file_fails(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    fx.set_baseline({"mnemos/db/mod_a.py": 1})
    # Sibling job or a careless edit adds one more raw reference.
    fx.write("mnemos/db/mod_a.py", _two_refs())
    r = fx.run()
    assert r.returncode == 1, "guard must fail when an in-baseline file gains a reference"
    assert "mnemos/db/mod_a.py: 2 raw _lc._pool references, baseline allows 1" in r.stdout
    assert "FAILED" in r.stdout


def test_added_reference_in_new_file_fails(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    fx.set_baseline({"mnemos/db/mod_a.py": 1})
    fx.write("mnemos/db/mod_b.py", _one_ref())  # brand-new file, not in baseline
    r = fx.run()
    assert r.returncode == 1, "guard must fail when a NEW file gains a reference"
    assert "NEW: mnemos/db/mod_b.py has 1 raw _lc._pool reference(s) but is not in the baseline" in r.stdout
    assert "FAILED" in r.stdout


def test_reduced_count_passes(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _two_refs())
    fx.set_baseline({"mnemos/db/mod_a.py": 2})
    # A parallel job removed one reference but did not update the baseline
    # yet (stale baseline — must not break the merge).
    fx.write("mnemos/db/mod_a.py", _one_ref())
    r = fx.run()
    assert r.returncode == 0, "reduced count must pass: " + r.stdout + r.stderr
    assert "below baseline" in r.stdout


def test_file_reduced_to_zero_passes(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    fx.set_baseline({"mnemos/db/mod_a.py": 1})
    fx.write("mnemos/db/mod_a.py", "no pool access here\n")
    r = fx.run()
    assert r.returncode == 0, r.stdout + r.stderr


def test_references_inside_persistence_are_ignored(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.set_baseline({})
    fx.write("mnemos/persistence/postgres.py", _two_refs())
    fx.write("mnemos/persistence/__init__.py", _one_ref())
    r = fx.run()
    assert r.returncode == 0, "mnemos/persistence/ is the home of pool access: " + r.stdout
    assert "passed" in r.stdout


def test_regen_writes_current_counts(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    fx.write("mnemos/api/routes/new_route.py", _two_refs())
    r = fx.run("--regen")
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(fx.baseline.read_text())
    assert data["files"] == {"mnemos/api/routes/new_route.py": 2, "mnemos/db/mod_a.py": 1}
    # After regen the tree is its own baseline and passes.
    assert fx.run().returncode == 0


def test_missing_baseline_fails(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.write("mnemos/db/mod_a.py", _one_ref())
    r = fx.run()
    assert r.returncode == 1, "missing baseline must fail (fail closed), not pass"
    assert "baseline" in r.stdout and "missing" in r.stdout


def test_comment_and_docstring_mentions_do_not_count(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.set_baseline({})
    fx.write(
        "mnemos/db/prose_only.py",
        "# raw _lc._pool access is forbidden outside persistence\n"
        "def f():\n"
        '    """No raw _lc._pool access per the 0b rule."""\n'
        "    return None\n"
        "x = 1  # note: _lc._pool lives in mnemos/persistence\n",
    )
    r = fx.run()
    assert r.returncode == 0, "prose mentions must not be flagged: " + r.stdout
    assert "passed" in r.stdout


def test_real_repo_baseline_is_consistent() -> None:
    """The committed baseline must describe the current tree: the guard run
    on the real repo passes (the baseline is a ratchet, so it also passes
    if parallel jobs have already reduced counts)."""
    repo = GUARD.resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(GUARD)], capture_output=True, text=True, cwd=repo
    )
    assert r.returncode == 0, f"committed baseline must pass on HEAD: {r.stdout}{r.stderr}"
