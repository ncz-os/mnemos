from __future__ import annotations

"""
MNEMOS Model Registry — llm_provider_registry.json Price Ingest

Parses llm_provider_registry.json and upserts per-model pricing
(price_in / price_out / price_cached) into model_registry.  When
pricing changes, writes a price_history audit row.

Design: ~/knemon-design-draft.md sec3 — KNEMON Step 2 groq/xai tier.

KNEMON subscription-mode: the JSON file is the authoritative pricing
source, re-ingested daily (or on-demand) so the registry stays current.
Provider keys in the JSON are mapped to model_registry provider columns.

Usage (standalone):
  python3 -m mnemos.domain.graeae.sync_provider_models --dry-run
  python3 -m mnemos.domain.graeae.sync_provider_models --provider groq xai
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Provider-name mapping: llm_provider_registry.json key → model_registry.provider ─

_REGISTRY_TO_DB_PROVIDER: dict[str, str] = {
    "groq": "groq",
    "xai": "xai",
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "together": "together",
    "deepseek_direct": "deepseek-direct",
    "deepseek-direct": "deepseek-direct",
    "ngc_integrate": "nvidia",
    "ngc_inference": "nvidia",
    "siliconflow": "siliconflow",
    "perplexity": "perplexity",
}

# Providers that have model_registry entries and are eligible for pricing ingest.
# groq and xai are the MVP Step 2 focus (per task spec).
_DEFAULT_PRICE_PROVIDERS: list[str] = [
    "groq",
    "xai",
    "deepseek-direct",
    "openai",
    "anthropic",
    "gemini",
    "together",
    "nvidia",
    "perplexity",
]


def _load_registry_json(path: Optional[Path] = None) -> dict:
    """Load llm_provider_registry.json and return parsed dict."""
    if path is None:
        # Canonical path: data/llm_provider_registry.json under the mnemos repo root
        path = Path(__file__).resolve().parents[4] / "data" / "llm_provider_registry.json"
    return json.loads(path.read_text())


def _extract_pricing(registry: dict, db_providers: Optional[list[str]] = None) -> list[dict]:
    """Walk the registry JSON and extract (provider, model_id, price_in, price_out, price_cached, raw) tuples.

    Only returns entries where at least one price field is a non-null numeric value
    and the provider is in db_providers (or all providers if db_providers is None).
    """
    providers = registry.get("providers", {})
    results: list[dict] = []

    for json_provider, prov_data in providers.items():
        db_provider = _REGISTRY_TO_DB_PROVIDER.get(json_provider)
        if db_provider is None:
            continue
        if db_providers is not None and db_provider not in db_providers:
            continue

        models = prov_data.get("models", {})
        for model_id, model_data in models.items():
            cost_in = model_data.get("cost_in_per_m")
            cost_out = model_data.get("cost_out_per_m")
            cache_hit = model_data.get("cache_hit_in_per_m")

            # Must have at least one numeric price to be useful
            has_price = False
            for v in (cost_in, cost_out, cache_hit):
                if v is not None and isinstance(v, (int, float)):
                    has_price = True
                    break
            if not has_price:
                continue

            results.append(
                {
                    "provider": db_provider,
                    "model_id": model_id,
                    "price_in": float(cost_in) if cost_in is not None else 0.0,
                    "price_out": float(cost_out) if cost_out is not None else 0.0,
                    "price_cached": float(cache_hit) if cache_hit is not None else 0.0,
                    "raw": model_data,
                }
            )

    return results


async def ingest_pricing(
    backend,
    registry_path: Optional[Path] = None,
    providers: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Ingest pricing from llm_provider_registry.json into model_registry + price_history.

    Returns a summary dict with counts of updated / unchanged / missing models.
    """
    t0 = time.monotonic()
    registry = _load_registry_json(registry_path)
    entries = _extract_pricing(registry, providers)

    if not entries:
        logger.info("[PRICE] no pricing entries extracted from registry JSON")
        return {
            "entries_found": 0,
            "updated": 0,
            "unchanged": 0,
            "not_found": 0,
            "errors": 0,
            "duration_ms": 0,
        }

    logger.info(f"[PRICE] extracted {len(entries)} pricing entries from registry JSON")

    if dry_run:
        for e in entries:
            logger.info(
                f"[PRICE] DRY-RUN: {e['provider']}/{e['model_id']} "
                f"in={e['price_in']:.6f} out={e['price_out']:.6f} "
                f"cached={e['price_cached']:.6f}"
            )
        return {
            "entries_found": len(entries),
            "updated": 0,
            "unchanged": 0,
            "not_found": 0,
            "errors": 0,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    repo = backend.consultations_audit
    updated = unchanged = not_found = errors = 0

    async with backend.transactional() as tx:
        for e in entries:
            try:
                rows_updated, old_prices = await repo.upsert_model_pricing(
                    tx,
                    provider=e["provider"],
                    model_id=e["model_id"],
                    price_in=e["price_in"],
                    price_out=e["price_out"],
                    price_cached=e["price_cached"],
                )
                if rows_updated == 0 and old_prices is None:
                    # Model not found in model_registry — skip
                    not_found += 1
                    logger.debug(
                        f"[PRICE] SKIP {e['provider']}/{e['model_id']} — not in model_registry"
                    )
                elif rows_updated > 0 and old_prices is not None:
                    # Pricing changed — write history
                    updated += 1
                    logger.info(
                        f"[PRICE] UPDATE {e['provider']}/{e['model_id']} "
                        f"in={old_prices['price_in']:.6f}→{e['price_in']:.6f} "
                        f"out={old_prices['price_out']:.6f}→{e['price_out']:.6f} "
                        f"cached={old_prices['price_cached']:.6f}→{e['price_cached']:.6f}"
                    )
                    await repo.write_price_history(
                        tx,
                        provider=e["provider"],
                        model_id=e["model_id"],
                        price_in=e["price_in"],
                        price_out=e["price_out"],
                        price_cached=e["price_cached"],
                        prices={
                            "price_in": e["price_in"],
                            "price_out": e["price_out"],
                            "price_cached": e["price_cached"],
                            "previous": old_prices,
                            "source": "llm_provider_registry.json",
                        },
                    )
                else:
                    # Pricing unchanged
                    unchanged += 1
                    logger.debug(
                        f"[PRICE] UNCHANGED {e['provider']}/{e['model_id']}"
                    )
            except NotImplementedError:
                logger.warning(
                    f"[PRICE] backend does not support pricing ingest — "
                    f"skipping {e['provider']}/{e['model_id']}"
                )
                break
            except Exception as exc:
                errors += 1
                logger.error(
                    f"[PRICE] ERROR {e['provider']}/{e['model_id']}: {exc}",
                    exc_info=True,
                )

    duration_ms = int((time.monotonic() - t0) * 1000)
    summary = {
        "entries_found": len(entries),
        "updated": updated,
        "unchanged": unchanged,
        "not_found": not_found,
        "errors": errors,
        "duration_ms": duration_ms,
    }
    logger.info(
        f"[PRICE] ingest complete: {updated} updated, {unchanged} unchanged, "
        f"{not_found} not-found, {errors} errors ({duration_ms}ms)"
    )
    return summary


async def sync_pricing(
    backend,
    registry_path: Optional[Path] = None,
    providers: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Top-level entry point: parse registry JSON and ingest pricing.

    Compatible signature with the scripts/sync_provider_models.py orchestrator.
    Returns a summary dict suitable for logging/display.
    """
    if backend is None and not dry_run:
        logger.warning("[PRICE] no persistence backend — skipping price ingest")
        return {
            "entries_found": 0,
            "updated": 0,
            "unchanged": 0,
            "not_found": 0,
            "errors": 0,
            "duration_ms": 0,
        }

    return await ingest_pricing(
        backend=backend,
        registry_path=registry_path,
        providers=providers or _DEFAULT_PRICE_PROVIDERS,
        dry_run=dry_run,
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Ingest LLM pricing from llm_provider_registry.json into model_registry"
    )
    parser.add_argument(
        "--provider",
        nargs="+",
        metavar="PROVIDER",
        help="Provider names to ingest pricing for (default: groq + xai tier)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print changes without writing to DB"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ingest pricing for all known providers, not just groq/xai",
    )
    args = parser.parse_args()

    async def _run() -> int:
        backend = None
        if not args.dry_run:
            from mnemos.core import lifecycle as _lc
            from mnemos.core.config import get_settings

            settings = get_settings()
            oracle_dsn = _lc._oracle_dsn_from_settings(settings)
            if oracle_dsn:
                backend = await _lc._build_oracle_backend(oracle_dsn, settings)
            else:
                logger.warning("[PRICE] no Oracle DSN configured; dry-run only")
                return 1

        providers = args.provider if args.provider else None
        if providers is None and not args.all:
            providers = ["groq", "xai"]  # default: groq/xai tier per task spec

        result = await sync_pricing(
            backend=backend,
            providers=providers,
            dry_run=args.dry_run,
        )

        print("\n=== Price Ingest Results ===")
        print(f"  entries found: {result['entries_found']}")
        print(f"  updated:       {result['updated']}")
        print(f"  unchanged:     {result['unchanged']}")
        print(f"  not in reg:    {result['not_found']}")
        print(f"  errors:        {result['errors']}")
        print(f"  duration:      {result['duration_ms']}ms")

        if backend is not None:
            await backend.close()
        return 0 if result["errors"] == 0 else 2

    sys.exit(asyncio.run(_run()))
