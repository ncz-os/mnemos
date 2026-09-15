#!/usr/bin/env python3
"""AST-level verifier for the v7 Feature 3/5 mechanical large-file split.

Confirms that a class or method moved from ``mnemos/persistence/oracle.py``
into ``mnemos/persistence/oracle_audit.py`` is byte-identical modulo
location. The comparison uses ``ast.dump`` on the function/class node, so
indentation differences from re-nesting are tolerated but any real
behavioral change (different statements, arguments, decorators, default
values, etc.) is caught.

Why AST and not raw bytes?
    The moved symbol changes its nesting level: a method defined inside
    ``OracleBackend`` ends up inside ``OracleAuditJournalMixin``, and the
    latter ends up at module top level inside ``oracle_audit.py``. That
    shifts the indentation of every line by 4 or 8 spaces. A raw byte
    comparison would flag the move itself as a diff. AST comparison
    ignores whitespace/indent by definition and only flags real changes.

Usage (single symbol)::

    python tools/verify_method_move.py \
        --before HEAD~1 \
        --before-file mnemos/persistence/oracle.py \
        --after-file mnemos/persistence/oracle_audit.py \
        --symbol OracleAuditChainRepository

Usage (summary across all 4 split symbols)::

    python tools/verify_method_move.py --report

Exit code is 0 if every check passes and nonzero if any real behavioral
diff is detected (or if a symbol cannot be located in either the BEFORE
or AFTER tree, which would itself be a bug).

The default ``--before`` is ``HEAD~1`` (the parent of the split commit).
For diffing against an older ref, pass ``--before <ref>`` explicitly. If
the split is uncommitted in your working tree, pass ``--before HEAD`` so
the BEFORE tree is the last committed snapshot rather than the split
commit itself.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Default symbol catalog for the v7 Feature 3/5 split.
# ---------------------------------------------------------------------------
# Each entry pins exactly where the symbol lives in the BEFORE and AFTER
# trees. ``before_class`` and ``after_class`` let us disambiguate methods
# that have the same simple name in more than one class. ``None`` means
# "top-level in the file". ``after_file`` is relative to the repo root.

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SYMBOLS: tuple[dict, ...] = (
    {
        "symbol": "OracleAuditChainRepository",
        "before_file": "mnemos/persistence/oracle.py",
        "after_file": "mnemos/persistence/oracle_audit.py",
        "before_class": None,
        "after_class": None,
    },
    {
        "symbol": "create_journal_entry",
        "before_file": "mnemos/persistence/oracle.py",
        "after_file": "mnemos/persistence/oracle_audit.py",
        "before_class": "OracleBackend",
        "after_class": "OracleAuditJournalMixin",
    },
    {
        "symbol": "list_journal_entries",
        "before_file": "mnemos/persistence/oracle.py",
        "after_file": "mnemos/persistence/oracle_audit.py",
        "before_class": "OracleBackend",
        "after_class": "OracleAuditJournalMixin",
    },
    {
        "symbol": "delete_journal_entry",
        "before_file": "mnemos/persistence/oracle.py",
        "after_file": "mnemos/persistence/oracle_audit.py",
        "before_class": "OracleBackend",
        "after_class": "OracleAuditJournalMixin",
    },
)


# Path of the *post-split* oracle.py — used to confirm that the moved symbol
# no longer exists at its old location. The split is mechanical, so the
# symbol must be ABSENT from the new oracle.py at the BEFORE location.
POST_SPLIT_ORACLE_FILE = "mnemos/persistence/oracle.py"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolLocation:
    """A resolved (file, class, name) pointer into an AST tree."""

    file: Path
    enclosing_class: str | None
    name: str

    def short(self) -> str:
        if self.enclosing_class is None:
            return f"{self.file.name}::{self.name}"
        return f"{self.file.name}::{self.enclosing_class}.{self.name}"


def _read_git_file(ref: str, repo_relative_path: str) -> str:
    """Return the contents of ``repo_relative_path`` at git ``ref``."""
    out = subprocess.run(
        ["git", "show", f"{ref}:{repo_relative_path}"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def _parse(path: Path, source: str | None = None) -> ast.Module:
    text = source if source is not None else path.read_text(encoding="utf-8")
    return ast.parse(text, filename=str(path))


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise LookupError(f"class {name!r} not found at module top level")


def _find_top_level(tree: ast.Module, name: str) -> ast.ClassDef | ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            return node
    raise LookupError(f"top-level {name!r} not found")


def _find_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        # Both ``def`` and ``async def`` show up in the AST. ``async def``
        # is *not* a subclass of ``FunctionDef`` (PEP 492 made it a sibling),
        # so check both explicitly.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(f"method {name!r} not found on class {cls.name!r}")


def _find_in_tree(
    tree: ast.Module,
    *,
    name: str,
    enclosing_class: str | None,
) -> ast.AST:
    """Locate ``name`` inside ``tree`` at the requested scope, or raise.

    Used by the absence-from-source check: it MUST raise ``LookupError``
    when the symbol is absent (so the caller treats that as PASS).
    """
    if enclosing_class is None:
        return _find_top_level(tree, name)
    cls = _find_class(tree, enclosing_class)
    return _find_method(cls, name)


def _resolve(
    *,
    label: str,
    tree: ast.Module,
    file: Path,
    name: str,
    enclosing_class: str | None,
) -> tuple[ast.AST, SymbolLocation]:
    if enclosing_class is None:
        node = _find_top_level(tree, name)
        loc = SymbolLocation(file=file, enclosing_class=None, name=name)
    else:
        cls = _find_class(tree, enclosing_class)
        node = _find_method(cls, name)
        loc = SymbolLocation(file=file, enclosing_class=enclosing_class, name=name)
    return node, loc


def _dump(node: ast.AST) -> str:
    """Stable, whitespace/indent-insensitive dump of an AST node.

    We deliberately use the default ``ast.dump`` (no
    ``include_attributes=True``) so that ``lineno``/``col_offset`` are not
    part of the comparison. The structural fingerprint is identical for
    nodes that are syntactically and semantically equivalent regardless
    of their position in the file or their indentation level.
    """
    return ast.dump(node, annotate_fields=True, indent=2)


# ---------------------------------------------------------------------------
# Per-symbol check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    symbol: str
    before_loc: SymbolLocation
    after_loc: SymbolLocation
    passed: bool
    detail: str = ""


def check_symbol(
    *,
    symbol: str,
    before_ref: str,
    before_file: str,
    after_file: str,
    before_class: str | None,
    after_class: str | None,
) -> CheckResult:
    before_path = REPO_ROOT / before_file
    after_path = REPO_ROOT / after_file

    # BEFORE: pull from git history.
    try:
        before_src = _read_git_file(before_ref, before_file)
    except subprocess.CalledProcessError as exc:
        return CheckResult(
            symbol=symbol,
            before_loc=SymbolLocation(file=before_path, enclosing_class=before_class, name=symbol),
            after_loc=SymbolLocation(file=after_path, enclosing_class=after_class, name=symbol),
            passed=False,
            detail=(f"could not read {before_ref}:{before_file} from git: {exc.stderr.strip() or exc}"),
        )

    before_tree = _parse(before_path, source=before_src)
    after_tree = _parse(after_path)

    try:
        before_node, before_loc = _resolve(
            label="before",
            tree=before_tree,
            file=before_path,
            name=symbol,
            enclosing_class=before_class,
        )
    except LookupError as exc:
        return CheckResult(
            symbol=symbol,
            before_loc=SymbolLocation(file=before_path, enclosing_class=before_class, name=symbol),
            after_loc=SymbolLocation(file=after_path, enclosing_class=after_class, name=symbol),
            passed=False,
            detail=f"BEFORE lookup failed: {exc}",
        )

    try:
        after_node, after_loc = _resolve(
            label="after",
            tree=after_tree,
            file=after_path,
            name=symbol,
            enclosing_class=after_class,
        )
    except LookupError as exc:
        return CheckResult(
            symbol=symbol,
            before_loc=before_loc,
            after_loc=SymbolLocation(file=after_path, enclosing_class=after_class, name=symbol),
            passed=False,
            detail=f"AFTER lookup failed: {exc}",
        )

    before_dump = _dump(before_node)
    after_dump = _dump(after_node)

    if before_dump != after_dump:
        # Build a compact diff hint that points to the first divergence so a
        # reviewer can eyeball the change without diffing the full AST dump.
        diff_hint = _first_diff_hint(before_dump, after_dump)
        return CheckResult(
            symbol=symbol,
            before_loc=before_loc,
            after_loc=after_loc,
            passed=False,
            detail=f"AST bodies differ; first divergence: {diff_hint}",
        )

    # Stronger guarantee for the mechanical split: the moved symbol must
    # also be ABSENT from its OLD location in the post-split ``oracle.py``.
    # If a reviewer accidentally reintroduces it (e.g. a half-applied
    # cherry-pick) we want a hard FAIL, not a silent PASS. Only the
    # catalog entries for the ``oracle.py`` move have a meaningful
    # ``before_file`` to check against; skips for other shapes are
    # accepted via ``post_split_file=None`` at the call site.
    if before_file == after_file:
        # Same file — the absence check is redundant (and would always
        # pass since we just resolved the symbol at this exact location).
        absence_detail = "absence-from-source check skipped (before==after file)"
    else:
        post_split_path = REPO_ROOT / POST_SPLIT_ORACLE_FILE
        post_split_tree = _parse(post_split_path)
        try:
            _find_in_tree(
                post_split_tree,
                name=symbol,
                enclosing_class=before_class,
            )
        except LookupError:
            absence_detail = (
                f"and symbol correctly absent from post-split {POST_SPLIT_ORACLE_FILE} "
                f"at {before_class or 'top level'}"
            )
        else:
            return CheckResult(
                symbol=symbol,
                before_loc=before_loc,
                after_loc=after_loc,
                passed=False,
                detail=(
                    f"moved symbol still present at its OLD location in "
                    f"{POST_SPLIT_ORACLE_FILE} ({before_class or 'top level'}.{symbol}); "
                    f"a mechanical move requires the symbol to be REMOVED from the source."
                ),
            )

    return CheckResult(
        symbol=symbol,
        before_loc=before_loc,
        after_loc=after_loc,
        passed=True,
        detail="AST bodies match exactly; " + absence_detail,
    )


def _first_diff_hint(before: str, after: str, *, context: int = 60) -> str:
    """Return the first ~``context`` chars around the first byte-level diff."""
    n = min(len(before), len(after))
    for i in range(n):
        if before[i] != after[i]:
            start = max(0, i - context // 2)
            end = min(n, i + context // 2)
            b = before[start:end].replace("\n", "\\n")
            a = after[start:end].replace("\n", "\\n")
            return f"byte {i}; BEFORE=…{b!r}…; AFTER=…{a!r}…"
    # One side is a prefix of the other.
    if len(before) != len(after):
        i = n
        start = max(0, i - context // 2)
        end = min(max(len(before), len(after)), i + context // 2)
        b = before[start:end].replace("\n", "\\n")
        a = after[start:end].replace("\n", "\\n")
        return f"length differs (before={len(before)} after={len(after)}); BEFORE=…{b!r}…; AFTER=…{a!r}…"
    return "(no diff found — should not happen)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_result(result: CheckResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    return (
        f"[{status}] {result.symbol}\n"
        f"    before: {result.before_loc.short()}\n"
        f"    after:  {result.after_loc.short()}\n"
        f"    detail: {result.detail}"
    )


def _run_all(before_ref: str) -> Iterable[CheckResult]:
    for entry in DEFAULT_SYMBOLS:
        yield check_symbol(before_ref=before_ref, **entry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that symbols moved out of mnemos/persistence/oracle.py "
            "into mnemos/persistence/oracle_audit.py are AST-identical."
        )
    )
    parser.add_argument(
        "--before",
        default="HEAD~1",
        help=(
            "Git ref for the BEFORE tree (default: HEAD~1, i.e. the parent "
            "of the split commit). The verifier is designed to compare the "
            "post-split source (read from disk / index) against the "
            "pre-split source pulled from git history. HEAD itself is the "
            "AFTER tree once the split commit is checked in, so the parent "
            "commit is what you want. Pass an older ref to diff against an "
            "explicit historical revision, or pass HEAD explicitly if you "
            "have an unusual workflow where the split is uncommitted."
        ),
    )
    parser.add_argument(
        "--before-file",
        help=("Repo-relative path of the BEFORE source file (read from git)."),
    )
    parser.add_argument(
        "--after-file",
        help=("Repo-relative path of the AFTER source file (read from disk)."),
    )
    parser.add_argument(
        "--symbol",
        help="Name of the class or method to verify.",
    )
    parser.add_argument(
        "--symbol-class",
        default=None,
        help=(
            "Enclosing class name when --symbol is a method (single-symbol "
            "mode). Default: search both BEFORE and AFTER with the same "
            "class name. To handle a class rename during the move, also "
            "pass --after-symbol-class."
        ),
    )
    parser.add_argument(
        "--after-symbol-class",
        default=None,
        help=("Enclosing class name on the AFTER side, if different from --symbol-class. Single-symbol mode only."),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "Run every default symbol in the v7 Feature 3/5 catalog and "
            "print a summary. Returns nonzero if any symbol fails."
        ),
    )

    args = parser.parse_args(argv)

    if args.report:
        results = list(_run_all(before_ref=args.before))
        for r in results:
            print(_format_result(r))
        failed = [r for r in results if not r.passed]
        passed = [r for r in results if r.passed]
        print()
        print(f"summary: {len(passed)} passed, {len(failed)} failed (out of {len(results)})")
        return 0 if not failed else 1

    # Single-symbol mode requires the four positional fields.
    missing = [
        name
        for name, val in (
            ("--before-file", args.before_file),
            ("--after-file", args.after_file),
            ("--symbol", args.symbol),
        )
        if not val
    ]
    if missing:
        parser.error(f"single-symbol mode requires: {', '.join(missing)}; or pass --report to run all default symbols.")

    result = check_symbol(
        symbol=args.symbol,
        before_ref=args.before,
        before_file=args.before_file,
        after_file=args.after_file,
        before_class=args.symbol_class,
        after_class=args.after_symbol_class or args.symbol_class,
    )
    print(_format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
