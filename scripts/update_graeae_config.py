#!/usr/bin/env python3
"""
Update GRAEAE config.toml when provider models change in the MNEMOS model registry.

Triggered daily by graeae-model-sync.service, after sync_provider_models.py finishes.

Modes
-----
Default (safe):   Only update providers whose current model has become unavailable/
                  deprecated in the registry. Prevents circuit-breaker storms when a
                  provider retires a model ID.
--upgrade:        Also proactively update to the registry-best model per provider,
                  even if the current model is still working.
--dry-run:        Print changes without writing config.toml or restarting service.

Updates
-------
* config.toml  [graeae.providers.<name>] model field
* config.toml  [graeae.providers.gemini] url field (Gemini embeds model in URL)
* graeae/elo_sync.py  _ELO_MODEL_MAP search terms (keeps Arena score matching live)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger("update_graeae_config")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://mnemos_user@localhost/mnemos")
CONFIG_PATH  = Path(os.getenv("MNEMOS_CONFIG",   "/opt/mnemos/config.toml"))
ELO_PATH     = Path(os.getenv("MNEMOS_ELO_PATH", "/opt/mnemos/graeae/elo_sync.py"))

DRY_RUN = "--dry-run" in sys.argv
UPGRADE  = "--upgrade"  in sys.argv

# GRAEAE provider key → model_registry provider name
_GRAEAE_TO_DB: dict[str, str] = {
    "perplexity":  "perplexity",
    "groq":        "groq",
    "claude_opus": "anthropic",
    "xai":         "xai",
    "openai":      "openai",
    "gemini":      "gemini",   # stored as "gemini" in model_registry (not "google")
    "nvidia":      "nvidia",
    "together":    "together",
}

# Providers whose URL contains the model slug — both model= and url= need updating
_URL_EMBEDS_MODEL = {"gemini"}


# ── TOML helpers (no external library needed) ─────────────────────────────────

def _read_toml_model(content: str, section: str) -> str | None:
    """Extract the model= value from a [graeae.providers.<section>] block."""
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(rf'^\[graeae\.providers\.{re.escape(section)}\]', stripped):
            in_section = True
            continue
        if stripped.startswith("[") and in_section:
            break
        if in_section:
            m = re.match(r'^model\s*=\s*"([^"]*)"', stripped)
            if m:
                return m.group(1)
    return None


def _write_toml_field(content: str, section: str, field: str, new_value: str) -> tuple[str, bool]:
    """Update field= in [graeae.providers.<section>]. Returns (new_content, changed)."""
    lines = content.splitlines(keepends=True)
    in_section = False
    changed = False
    result = []
    for line in lines:
        stripped = line.strip()
        if re.match(rf'^\[graeae\.providers\.{re.escape(section)}\]', stripped):
            in_section = True
        elif stripped.startswith("["):
            in_section = False
        if in_section and re.match(rf'^{re.escape(field)}\s*=\s*"[^"]*"', stripped):
            new_line = re.sub(r'"[^"]*"', f'"{new_value}"', line, count=1)
            if new_line != line:
                changed = True
                result.append(new_line)
                continue
        result.append(line)
    return "".join(result), changed


def _update_elo_sync(elo_path: Path, graeae_provider: str, new_model_id: str) -> bool:
    """Replace the search term for graeae_provider in _ELO_MODEL_MAP."""
    content = elo_path.read_text()
    # Match: "provider":  "old_search_term",
    pattern = rf'((?:^|\s)"{re.escape(graeae_provider)}"\s*:\s*)"[^"]*"'
    new_content, count = re.subn(pattern, rf'\1"{new_model_id}"', content)
    if count:
        if not DRY_RUN:
            elo_path.write_text(new_content)
        return True
    return False


# ── DB queries ────────────────────────────────────────────────────────────────

async def _is_available(pool, db_provider: str, model_id: str) -> bool | None:
    """True=available, False=deprecated/gone, None=not in registry."""
    row = await pool.fetchrow(
        "SELECT available, deprecated FROM model_registry "
        "WHERE provider=$1 AND model_id=$2",
        db_provider, model_id,
    )
    if row is None:
        return None
    return bool(row["available"]) and not bool(row["deprecated"])


async def _best_model(pool, db_provider: str) -> str | None:
    row = await pool.fetchrow(
        """SELECT model_id FROM model_registry
           WHERE provider=$1 AND available=TRUE AND deprecated=FALSE
           ORDER BY graeae_weight DESC NULLS LAST,
                    arena_score   DESC NULLS LAST,
                    last_seen     DESC NULLS LAST
           LIMIT 1""",
        db_provider,
    )
    return row["model_id"] if row else None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    mode = "upgrade" if UPGRADE else "safe"
    logger.info(f"[CONFIG-UPDATE] mode={mode}{' DRY-RUN' if DRY_RUN else ''}")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    config_content = CONFIG_PATH.read_text()
    changes: list[tuple[str, str, str, str]] = []  # (graeae_name, old, new, reason)

    try:
        for graeae_name, db_name in _GRAEAE_TO_DB.items():
            current_model = _read_toml_model(config_content, graeae_name)
            if current_model is None:
                continue  # provider not in config

            available = await _is_available(pool, db_name, current_model)

            if available is True and not UPGRADE:
                # Safe mode: current model is fine
                continue

            if available is True and UPGRADE:
                # Upgrade mode: check if registry has something better
                best = await _best_model(pool, db_name)
                if not best or best == current_model:
                    continue
                new_model = best
                reason = "upgrade"
            elif available is False:
                # Deprecated/gone: must replace
                new_model = await _best_model(pool, db_name)
                if not new_model or new_model == current_model:
                    if not new_model:
                        logger.warning(
                            f"[CONFIG-UPDATE] {graeae_name}: current model deprecated "
                            f"but no available replacement found"
                        )
                    continue
                reason = "deprecated"
            else:
                # available is None (not in registry): log and skip
                logger.debug(
                    f"[CONFIG-UPDATE] {graeae_name}: model {current_model!r} not in registry — skipping"
                )
                continue

            logger.info(
                f"[CONFIG-UPDATE] {graeae_name}: {current_model!r} → {new_model!r}  ({reason})"
            )
            changes.append((graeae_name, current_model, new_model, reason))

            if not DRY_RUN:
                config_content, _ = _write_toml_field(
                    config_content, graeae_name, "model", new_model
                )
                if graeae_name in _URL_EMBEDS_MODEL:
                    # Also update the URL (Gemini pattern: .../models/<slug>:generateContent)
                    old_url_match = re.search(
                        r'^url\s*=\s*"([^"]*)"',
                        "\n".join(
                            l for l in config_content.splitlines()
                            if re.match(r'url\s*=', l.strip())
                        ),
                    )
                    new_url = re.sub(
                        r'(/models/)[^/:]+(:)',
                        rf'\g<1>{new_model}\2',
                        _get_section_url(config_content, graeae_name),
                    )
                    if new_url:
                        config_content, _ = _write_toml_field(
                            config_content, graeae_name, "url", new_url
                        )

    finally:
        await pool.close()

    if not changes:
        logger.info("[CONFIG-UPDATE] All provider models current — nothing to do.")
        return 0

    if DRY_RUN:
        print(f"\n{'DRY RUN — '}{len(changes)} change(s) pending:")
        for name, old, new, reason in changes:
            print(f"  {name:15s}  {old!r:50s}  →  {new!r}  ({reason})")
        return 0

    CONFIG_PATH.write_text(config_content)
    logger.info(f"[CONFIG-UPDATE] Wrote {CONFIG_PATH} ({len(changes)} change(s))")

    # Update elo_sync.py search terms
    for graeae_name, _, new_model, _ in changes:
        if _update_elo_sync(ELO_PATH, graeae_name, new_model):
            logger.info(f"[CONFIG-UPDATE] elo_sync.py updated for {graeae_name} → {new_model!r}")

    # Restart service
    result = subprocess.run(
        ["systemctl", "restart", "mnemos.service"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("[CONFIG-UPDATE] mnemos.service restarted")
    else:
        logger.error(f"[CONFIG-UPDATE] restart failed: {result.stderr.strip()}")
        return 1

    return 0


def _get_section_url(content: str, section: str) -> str:
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(rf'^\[graeae\.providers\.{re.escape(section)}\]', stripped):
            in_section = True
            continue
        if stripped.startswith("[") and in_section:
            break
        if in_section:
            m = re.match(r'^url\s*=\s*"([^"]*)"', stripped)
            if m:
                return m.group(1)
    return ""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
