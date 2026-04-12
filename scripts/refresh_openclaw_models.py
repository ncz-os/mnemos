#!/usr/bin/env python3
"""
Daily refresh of ~/.openclaw/openclaw.json from the MNEMOS model registry.

Run daily via launchd ~30–60 min after PYTHIA's graeae-model-sync.timer (03:15 UTC).

Actions
-------
1. Sort each provider's model list by quality score
   (graeae_weight DESC → arena_score DESC → context_window DESC)
2. Replace any deprecated/unavailable model with the best available substitute
3. Optionally (--upgrade) add registry-best model as primary if not already listed

Flags
-----
  --dry-run    Show planned changes, do not write
  --upgrade    Proactively insert registry-best model as primary (not just fix deprecated)
  --restart    Send SIGHUP to openclaw after writing (triggers config reload)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

REGISTRY_BASE = "http://192.0.2.67:5002"
OPENCLAW_PATH = Path.home() / ".openclaw" / "openclaw.json"

# openclaw provider key → MNEMOS registry provider name
_OPENCLAW_TO_REGISTRY: dict[str, str] = {
    "google":     "google",
    "groq":       "groq",
    "nvidia":     "nvidia",
    "openai":     "openai",
    "perplexity": "perplexity",
    "together":   "together",
    "xai":        "xai",
    # "ollama": local, not in MNEMOS registry — always skipped
}


# ── Registry helpers ──────────────────────────────────────────────────────────

def _fetch(path: str) -> list | dict:
    url = f"{REGISTRY_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(f"[ERROR] Cannot reach MNEMOS at {url}: {exc}", file=sys.stderr)
        sys.exit(1)


def _build_registry(all_models: list) -> dict[str, dict[str, dict]]:
    """Return {db_provider: {model_id_lower: entry_dict}}."""
    out: dict[str, dict[str, dict]] = {}
    for entry in all_models:
        p = entry["provider"]
        out.setdefault(p, {})[entry["model_id"].lower()] = entry
    return out


def _score(entry: dict) -> tuple:
    """Higher = better. Models not in registry score (0, 0, 0)."""
    return (
        entry.get("graeae_weight") or 0.0,
        entry.get("arena_score") or 0.0,
        (entry.get("context_window") or 0) / 1_000_000,
    )


def _best_available(prov_reg: dict[str, dict], family_hint: str = "") -> dict | None:
    candidates = [
        e for e in prov_reg.values()
        if e.get("available", True) and not e.get("deprecated", False)
    ]
    if not candidates:
        return None
    if family_hint:
        family_matches = [
            e for e in candidates
            if (e.get("family") or "").startswith(family_hint)
        ]
        if family_matches:
            candidates = family_matches
    return max(candidates, key=_score)


# ── openclaw model-entry factory ──────────────────────────────────────────────

def _make_entry(reg: dict, api_type: str = "openai-completions") -> dict:
    caps = set(reg.get("capabilities") or [])
    return {
        "id":            reg["model_id"],
        "name":          reg.get("display_name") or reg["model_id"],
        "reasoning":     "reasoning" in caps,
        "input":         ["text"] + (["image"] if "vision" in caps else []),
        "cost": {
            "input":       reg.get("input_cost_per_mtok")  or 0,
            "output":      reg.get("output_cost_per_mtok") or 0,
            "cacheRead":   0,
            "cacheWrite":  0,
        },
        "contextWindow": reg.get("context_window")   or 131072,
        "maxTokens":     reg.get("max_output_tokens") or 32768,
        "api":           api_type,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh openclaw.json from MNEMOS model registry")
    ap.add_argument("--dry-run",  action="store_true", help="Show changes without writing")
    ap.add_argument("--upgrade",  action="store_true",
                    help="Insert registry-best model as primary if not already present")
    ap.add_argument("--restart",  action="store_true",
                    help="Send SIGHUP to openclaw process after writing")
    args = ap.parse_args()

    # Pull full catalog (including deprecated, for lookup)
    all_models: list = _fetch("/model-registry/?available_only=false&limit=500")
    registry = _build_registry(all_models)

    if not OPENCLAW_PATH.exists():
        print(f"[ERROR] {OPENCLAW_PATH} not found", file=sys.stderr)
        return 1

    config = json.loads(OPENCLAW_PATH.read_text())
    providers: dict = config.get("models", {}).get("providers", {})
    changes: list[str] = []

    for oc_prov, pcfg in providers.items():
        reg_prov = _OPENCLAW_TO_REGISTRY.get(oc_prov)
        if not reg_prov:
            continue  # ollama and any unknown providers

        prov_reg = registry.get(reg_prov)
        if not prov_reg:
            print(f"[WARN] no registry data for {reg_prov} — skipping {oc_prov}")
            continue

        models: list[dict] = pcfg.get("models", [])
        if not models:
            continue

        api_type = models[0].get("api") or pcfg.get("api") or "openai-completions"

        # -- 1. Fix deprecated / unavailable models --
        updated_models: list[dict] = []
        for m in models:
            reg_entry = prov_reg.get(m["id"].lower())
            if reg_entry and (
                not reg_entry.get("available", True) or reg_entry.get("deprecated", False)
            ):
                family = (reg_entry.get("family") or "").split("-")[0]
                replacement = _best_available(prov_reg, family_hint=family)
                if replacement and replacement["model_id"] != m["id"]:
                    msg = f"{oc_prov}: {m['id']!r} deprecated → {replacement['model_id']!r}"
                    print(f"[FIX]  {msg}")
                    changes.append(msg)
                    if not args.dry_run:
                        updated_models.append(_make_entry(replacement, api_type))
                    continue
            updated_models.append(m)

        # -- 2. Sort by registry quality score (best first) --
        def _model_score(m: dict) -> tuple:
            e = prov_reg.get(m["id"].lower(), {})
            return _score(e)

        sorted_models = sorted(updated_models, key=_model_score, reverse=True)

        old_ids = [m["id"] for m in updated_models]
        new_ids = [m["id"] for m in sorted_models]
        if old_ids != new_ids:
            msg = f"{oc_prov}: reordered [{', '.join(old_ids)}] → [{', '.join(new_ids)}]"
            print(f"[SORT] {msg}")
            changes.append(msg)

        # -- 3. Upgrade: prepend registry-best if not already listed --
        if args.upgrade:
            best = _best_available(prov_reg)
            if best:
                current_ids_lower = {m["id"].lower() for m in sorted_models}
                if best["model_id"].lower() not in current_ids_lower:
                    msg = f"{oc_prov}: adding registry-best {best['model_id']!r} as primary"
                    print(f"[NEW]  {msg}")
                    changes.append(msg)
                    if not args.dry_run:
                        sorted_models = [_make_entry(best, api_type)] + sorted_models

        if not args.dry_run:
            pcfg["models"] = sorted_models

    if not changes:
        print("[OK] openclaw.json is up-to-date — no changes needed.")
        return 0

    if args.dry_run:
        print(f"\n--- Dry run: {len(changes)} change(s) would be applied ---")
        return 0

    OPENCLAW_PATH.write_text(json.dumps(config, indent=2))
    print(f"\n[DONE] Wrote {OPENCLAW_PATH} ({len(changes)} change(s))")

    if args.restart:
        import subprocess
        r = subprocess.run(
            ["pkill", "-HUP", "-f", "openclaw"],
            capture_output=True, text=True,
        )
        if r.returncode in (0, 1):  # 1 = process not found
            print("[RESTART] openclaw reload signal sent")
        else:
            print(f"[RESTART] pkill error: {r.stderr.strip()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
