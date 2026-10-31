"""Routes must not import each other's private names.

Decision M11 step 14: the shared helpers that six route modules were
importing out of ``api/routes/memories.py`` now live in library modules
(``api/read_policy.py``, ``api/memory_mutations.py``, ``api/errors.py``).
This test is the executable form of that rule, so a future helper cannot
quietly walk back into a sibling route.

Two layers:

* a static AST scan that names the offending ``route -> module.symbol``
  pair, which is far more useful in a failure message than Grimp's
  contract output; and
* an assertion that the import-linter contract covering the same ground
  is present and KEPT, so the contract cannot be deleted or silently
  loosened while this test keeps passing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = ROOT / "mnemos" / "api" / "routes"

CONTRACT_NAME = "routes do not import each other's private names"

# Route-local shims that are shared on purpose. Each entry must carry a
# reason: these are helpers with no request or auth state, not handlers.
# Keep this list identical to ``ignore_imports`` in pyproject.toml —
# test_ignore_list_matches_contract fails otherwise.
ALLOWED = {
    ("dag", "_postgres_only"),
    ("entities", "_postgres_only"),
    ("morpheus", "_postgres_only"),
    ("memories", "narrate"),
    ("versions", "dag"),
}


def _route_module_name(path: Path) -> str:
    return path.stem


def _imported_private_names(tree: ast.AST) -> list[tuple[str, int, str]]:
    """Yield (module, lineno, symbol) for ``from ..routes.X import _sym``."""
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        parts = node.module.split(".")
        if len(parts) < 4 or parts[0:3] != ["mnemos", "api", "routes"]:
            continue
        source = parts[3]
        for alias in node.names:
            out.append((source, node.lineno, alias.name))
    return out


def test_routes_do_not_import_sibling_route_names() -> None:
    violations: list[str] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        importer = _route_module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for source, lineno, symbol in _imported_private_names(tree):
            if source == importer:
                continue
            if (importer, source) in ALLOWED:
                continue
            violations.append(
                f"{path.relative_to(ROOT)}:{lineno} {importer} imports {symbol!r} from sibling route {source!r}"
            )
    assert not violations, (
        "route modules must import shared helpers from mnemos.api.read_policy, "
        "mnemos.api.memory_mutations, mnemos.api.errors (or a domain/persistence "
        "module), not from each other:\n  " + "\n  ".join(violations)
    )


def test_ignore_list_matches_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = project["tool"]["importlinter"]["contracts"]
    matching = [c for c in contracts if c.get("name") == CONTRACT_NAME]
    assert matching, f"import-linter contract {CONTRACT_NAME!r} is missing from pyproject.toml"
    contract = matching[0]

    listed = {tuple(line.split(" -> mnemos.api.routes.")) for line in contract.get("ignore_imports", [])}
    normalised = {(src.split(".")[-1], dst) for src, dst in listed}
    assert normalised == ALLOWED, (
        "the import-linter ignore_imports and this test's ALLOWED set disagree: "
        f"only in pyproject={sorted(normalised - ALLOWED)}, "
        f"only in test={sorted(ALLOWED - normalised)}"
    )


def test_shared_helpers_live_outside_routes() -> None:
    """The relocated helpers are importable from the library modules."""
    from mnemos.api import errors, memory_mutations, read_policy

    for module, names in (
        (
            read_policy,
            ("_redacted_for_webhook", "_should_frame_data", "_should_redact_secrets", "_validate_permission_mode"),
        ),
        (
            memory_mutations,
            (
                "_insert_memory_with_created_webhook",
                "_rls_context",
                "_schedule_outbox_deliveries",
                "_write_memory_mutation_audit_entry",
            ),
        ),
        (errors, ("is_db_disconnect", "database_disconnected_detail", "translate_persistence_error")),
    ):
        for name in names:
            assert hasattr(module, name), f"{module.__name__} is missing {name}"
