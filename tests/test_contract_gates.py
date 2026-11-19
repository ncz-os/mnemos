"""Executable forms of the two CI contract gates this repo runs on every MR.

Both gates are "run a tool, then assert the tree is what the tool says":

* ``lint:import-linter`` — ``lint-imports --config pyproject.toml`` must keep
  every contract. The one that historically broke was the route graph:
  ``routes.ingest`` pulled shared helpers from ``api.memory_mutations``,
  which in turn reached back into ``routes.memories``. That is an import
  cycle through a lower layer, and it is exactly what the
  "routes do not import each other's private names" contract forbids.

* ``lint:backend-parity`` — ``scripts/generate_backend_parity_matrix.py``
  renders ``docs/BACKEND_PARITY.md`` purely from the live tree, so a
  committed matrix that no longer matches the code is drift. The CI job
  regenerates and runs ``git diff --exit-code``; the generator's own
  ``--check`` mode performs the same comparison in-process.

Both checks are kept cheap here: the route assertion is a static AST
scan (no import-linter/grimp needed to run the default suite) and the
parity assertion calls the generator's ``--check`` path directly.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = ROOT / "mnemos" / "api" / "routes"

#: Modules on the ingest write path that sit below the route layer. None of
#: them may import a sibling route module: a lower layer importing
#: ``mnemos.api.routes.*`` turns "route -> library" into a cycle.
INGEST_PATH_LIBRARIES = (
    "mnemos/api/memory_mutations.py",
    "mnemos/api/read_policy.py",
    "mnemos/api/errors.py",
    "mnemos/api/persistence_helpers.py",
    "mnemos/domain/deletion_fence.py",
)


def _route_imports(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, module) for every ``mnemos.api.routes.*`` import."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mnemos.api.routes"):
                    found.append((node.lineno, alias.name))
            continue
        if module and module.startswith("mnemos.api.routes"):
            found.append((node.lineno, module))
    return found


def test_ingest_write_path_does_not_import_route_modules() -> None:
    """The library modules ingest depends on must not reach back into routes.

    ``routes.ingest -> api.memory_mutations -> routes.memories`` broke the
    layered contract: the middle module is below the route layer, so the
    dependency must point one way only. The embedder call that caused it
    now goes through ``mnemos.core.lifecycle`` (bound as ``_lc``), which
    sits at the bottom of the layer stack.
    """
    violations: list[str] = []
    for rel in INGEST_PATH_LIBRARIES:
        for lineno, module in _route_imports(ROOT / rel):
            violations.append(f"{rel}:{lineno} imports {module}")
    assert not violations, (
        "lower-layer modules on the ingest write path must not import route "
        "modules (it creates a cycle through the API layer):\n  " + "\n  ".join(violations)
    )


def test_memory_mutations_binds_embedder_from_lifecycle() -> None:
    """The post-PATCH re-embed helper reads the embedder from ``_lc``.

    Patching ``_lc._get_embedding`` must be the way a test fakes the model;
    importing it out of a route module would re-introduce the cycle above.
    """
    source = (ROOT / "mnemos/api/memory_mutations.py").read_text(encoding="utf-8")
    assert "_lc._get_embedding(" in source
    assert "from mnemos.api.routes" not in source


def test_ingest_route_uses_shared_library_helpers() -> None:
    """``routes.ingest`` keeps taking its write helpers from the library."""
    tree = ast.parse((ROUTES_DIR / "ingest.py").read_text(encoding="utf-8"))
    imported_from: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_from.add(node.module)
    assert "mnemos.api.memory_mutations" in imported_from
    assert "mnemos.api.read_policy" in imported_from
    assert not {m for m in imported_from if m.startswith("mnemos.api.routes")}


def test_backend_parity_matrix_matches_code() -> None:
    """``docs/BACKEND_PARITY.md`` must be a pure function of the live tree.

    Same assertion the CI job makes with ``git diff --exit-code``, without
    needing a git checkout: the generator's ``--check`` mode re-renders the
    matrix and compares it byte-for-byte with the committed file.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_backend_parity_matrix.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    assert result.returncode == 0, (
        "docs/BACKEND_PARITY.md has drifted from the code; run "
        "`python3 scripts/generate_backend_parity_matrix.py` and commit the "
        f"result.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
