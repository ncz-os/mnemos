#!/usr/bin/env python3
"""Refresh the PANTHEON external pricing/catalog cache."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mnemos.domain.pantheon.pricing import main

if __name__ == "__main__":
    sys.exit(main())
