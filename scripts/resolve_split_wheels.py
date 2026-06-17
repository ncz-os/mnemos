from __future__ import annotations

import argparse
from pathlib import Path
import sys


EXPECTED_WHEEL_STEMS = (
    "mnemos_core",
    "mnemos_pantheon",
    "mnemos_knemon",
    "mnemos_graeae",
    "mnemos_charon",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve first-party split wheels to an install requirements file.",
    )
    parser.add_argument("--wheelhouse", default="/wheelhouse", help="Directory containing built wheels.")
    parser.add_argument(
        "--output",
        default="/tmp/first-party-wheels.txt",
        help="Path to write resolved wheel paths.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wheelhouse = Path(args.wheelhouse)
    resolved: list[str] = []

    for stem in EXPECTED_WHEEL_STEMS:
        matches = sorted(wheelhouse.glob(f"{stem}-*.whl"))
        if len(matches) != 1:
            print(f"expected exactly one {stem} wheel, found {len(matches)}", file=sys.stderr)
            for match in matches:
                print(match, file=sys.stderr)
            return 1
        resolved.append(str(matches[0]))

    Path(args.output).write_text("\n".join(resolved) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
