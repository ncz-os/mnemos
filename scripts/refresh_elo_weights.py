#!/usr/bin/env python3
"""
Arena.ai Elo weight refresh script.

Intended to run quarterly via systemd timer:

  /etc/systemd/system/graeae-elo-sync.service
  /etc/systemd/system/graeae-elo-sync.timer

Usage:
  python3 scripts/refresh_elo_weights.py          # normal refresh
  python3 scripts/refresh_elo_weights.py --force  # force even if cache is fresh

After a successful refresh, restart mnemos.service so the engine picks up
the new weights on next startup:
  sudo systemctl restart mnemos.service
"""

import argparse
import logging
import sys
from pathlib import Path

# Make sure the package is importable when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("refresh_elo_weights")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh GRAEAE provider weights from Arena.ai Elo leaderboard")
    parser.add_argument("--force", action="store_true", help="Bypass cache and fetch fresh data")
    args = parser.parse_args()

    from mnemos.core.config import get_settings
    from mnemos.domain.graeae.elo_sync import get_elo_weights

    logger.info("=== Arena.ai Elo weight refresh ===")
    logger.info("Registry path: %s", get_settings().graeae.elo_registry.expanduser())

    weights = get_elo_weights(force_refresh=args.force)
    if not weights:
        logger.error("Fetch failed — weights unchanged. Check logs for details.")
        return 1

    logger.info("Provider weights after refresh:")
    for provider, w in sorted(weights.items(), key=lambda x: -x[1]):
        logger.info(f"  {provider:15s}  {w:.4f}")

    logger.info("=== Refresh complete — restart mnemos.service to apply ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
