"""Compatibility entrypoint for the PANTHEON catalog refresh job."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from mnemos.domain.pantheon.pricing import main as pantheon_main
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "<unknown>"
        print(
            "refresh-pantheon-catalog requires the mnemos-pantheon add-on package "
            "and its dependencies. The PANTHEON add-on or one of its dependencies "
            f"could not be imported ({missing}: {exc}). Repair with: "
            "pip install mnemos-pantheon and its dependencies.",
            file=sys.stderr,
        )
        return 2

    return int(pantheon_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
