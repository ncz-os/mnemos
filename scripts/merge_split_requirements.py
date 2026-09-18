from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib


FIRST_PARTY = {
    "mnemos-core",
    "mnemos-pantheon",
    "mnemos-knemon",
    "mnemos-graeae",
}
ADDON_PACKAGES = ("pantheon", "knemon", "graeae")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge split-image third-party requirements.",
    )
    parser.add_argument("--core", default="core", help="Path to the mnemos-core source tree.")
    parser.add_argument(
        "--addons",
        nargs="*",
        default=list(ADDON_PACKAGES),
        help="Paths to add-on source trees.",
    )
    parser.add_argument(
        "--output",
        default="/tmp/requirements.split.txt",
        help="Path to write the merged requirements file.",
    )
    return parser.parse_args()


def parse_req(req: str) -> tuple[str, tuple[str, ...]]:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(?:\[([^\]]+)\])?", req)
    if match is None:
        return "", ()
    name = match.group(1).lower().replace("_", "-")
    extras = tuple(
        extra.strip().lower().replace("_", "-") for extra in (match.group(2) or "").split(",") if extra.strip()
    )
    return name, extras


def main() -> int:
    args = parse_args()
    core_path = Path(args.core)
    output_path = Path(args.output)
    reqs: list[str] = []
    seen_extras: set[str] = set()

    core = tomllib.loads((core_path / "pyproject.toml").read_text())
    optional = core.get("project", {}).get("optional-dependencies", {})

    def add_core_line(line: str, lineno: int) -> None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return
        if stripped.startswith("-"):
            print(
                "[split] dropping pip option from "
                f"{core_path / 'requirements.txt'}:{lineno}: {stripped} "
                "(only plain requirement specs are merged)",
                file=sys.stderr,
            )
            return
        reqs.append(stripped)

    def expand_core_extra(extra: str) -> None:
        if extra in seen_extras:
            return
        seen_extras.add(extra)
        for dep in optional.get(extra, []):
            add_req(dep)

    def add_req(req: str) -> None:
        req = req.strip()
        if not req:
            return
        name, extras = parse_req(req)
        if name == "mnemos-core":
            for extra in extras:
                expand_core_extra(extra)
            return
        if name in FIRST_PARTY:
            return
        reqs.append(req)

    for lineno, line in enumerate((core_path / "requirements.txt").read_text().splitlines(), 1):
        add_core_line(line, lineno)

    for pkg in args.addons:
        data = tomllib.loads((Path(pkg) / "pyproject.toml").read_text())
        for dep in data.get("project", {}).get("dependencies", []):
            add_req(dep)

    reqs.append("oracledb>=4.0.1")
    merged = list(dict.fromkeys(reqs))
    output_path.write_text("\n".join(merged) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
