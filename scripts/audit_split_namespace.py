from __future__ import annotations

import argparse
from pathlib import Path
import sys


DEFAULT_ROOTS = ("core", "pantheon", "knemon", "graeae", "charon")
FORBIDDEN_NAMESPACE_MARKERS = {
    "mnemos/__init__.py",
    "mnemos/domain/__init__.py",
    "mnemos/api/__init__.py",
    "mnemos/api/routes/__init__.py",
    # mnemos.tools spans core + the charon add-on (adapters / MPF tools), and the
    # umbrella image runs with core's source tree on sys.path AHEAD of the
    # installed wheels. A regular-package __init__.py here pins mnemos.tools.__path__
    # to the core source dir and hides the add-on's tools submodules, so it MUST
    # stay a PEP 420 namespace package.
    "mnemos/tools/__init__.py",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit split package mnemos namespace files before wheel build.",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        default=list(DEFAULT_ROOTS),
        help="Package source roots to audit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    owners: dict[str, str] = {}
    errors: list[str] = []

    for root in args.roots:
        base = Path(root) / "mnemos"
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not (path.is_file() or path.is_symlink()):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in FORBIDDEN_NAMESPACE_MARKERS:
                errors.append(f"{root}: forbidden namespace marker {rel}")
            previous = owners.setdefault(rel, root)
            if previous != root:
                errors.append(f"duplicate mnemos namespace file {rel}: {previous}, {root}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
