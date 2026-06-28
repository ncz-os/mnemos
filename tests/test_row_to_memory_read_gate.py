"""F6 guard (adversarial review 2026-06-28).

``row_to_memory`` defaults ``redact_secrets=False, frame_data=False`` — a
*deliberate, documented* choice (see its docstring): the default is full
content so a forgotten caller fails toward content-correctness, not silent
masking, and EVERY default-scope read handler passes ``redact_secrets=True``
explicitly via the route gate.

The risk the security review raised (F6) is that a *new* read handler forgets
the flag and silently leaks raw/unframed content. Rather than flip the
documented default (which would change the failure mode the maintainer chose),
this test makes "forgetting" impossible to do silently: every
``row_to_memory`` / ``_row_to_memory`` call in the API route layer must pass
``redact_secrets`` explicitly, UNLESS its enclosing function is an
allowlisted write-then-echo path (a trusted writer seeing its own just-written
content — redacting that echo would be wrong).

A new call site that omits the flag and is not a conscious echo fails here,
forcing the author to classify it. That is the fail-closed guarantee F6 wanted,
without overriding the documented default.
"""

from __future__ import annotations

import ast
import pathlib

# (filename, enclosing function) pairs allowed to call row_to_memory without an
# explicit redact_secrets flag: write-then-echo paths returning the caller's own
# just-written/just-reverted row. Adding to this list is a conscious decision.
ECHO_ALLOWLIST: set[tuple[str, str]] = {
    ("memories.py", "create_memory"),
    ("memories.py", "update_memory"),
    ("versions.py", "revert_memory"),
}

_ROUTES_DIR = pathlib.Path(__file__).resolve().parents[1] / "mnemos" / "api" / "routes"


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return getattr(best, "name", "<module>")


def _row_to_memory_calls():
    """Yield (filename, enclosing_fn, lineno, passes_redact_flag) for every
    row_to_memory / _row_to_memory call in the API route layer."""
    for path in sorted(_ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in ("row_to_memory", "_row_to_memory"):
                continue
            kwargs = {kw.arg for kw in node.keywords}
            yield (path.name, _enclosing_function(tree, node.lineno), node.lineno, "redact_secrets" in kwargs)


def test_route_layer_has_row_to_memory_calls():
    """Sanity: the scanner actually finds call sites (guards against a silent
    no-op if the helper is renamed or the routes move)."""
    calls = list(_row_to_memory_calls())
    assert calls, "no row_to_memory calls found in mnemos/api/routes — scanner stale?"


def test_default_scope_reads_pass_redact_secrets_explicitly():
    """Every route-layer row_to_memory call passes redact_secrets explicitly,
    unless it is an allowlisted write-then-echo path."""
    offenders = [
        f"{fname}::{fn} (line {lineno})"
        for fname, fn, lineno, has_flag in _row_to_memory_calls()
        if not has_flag and (fname, fn) not in ECHO_ALLOWLIST
    ]
    assert not offenders, (
        "row_to_memory called without an explicit redact_secrets flag outside the "
        "echo allowlist — a default-scope read must pass redact_secrets=True (and "
        "frame_data=True), or be added to ECHO_ALLOWLIST if it is a trusted "
        "write-then-echo path:\n  " + "\n  ".join(offenders)
    )


def test_echo_allowlist_entries_still_exist():
    """Allowlisted echo sites must still be real no-flag calls — keeps the
    allowlist from going stale (and silently permitting a future omission)."""
    seen = {(fname, fn) for fname, fn, _, has_flag in _row_to_memory_calls() if not has_flag}
    stale = ECHO_ALLOWLIST - seen
    assert not stale, f"ECHO_ALLOWLIST has entries no longer present as no-flag calls: {stale}"
