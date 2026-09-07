#!/usr/bin/env python3
"""
mnemos-rehydrate.py — Tier-based context injection from MNEMOS on session start.

Triggered by: UserPromptSubmit hook (fires on every user prompt)
Behavior: Fires only ONCE per session (tracked by session_id). Silent on
          subsequent prompts. Silent if MNEMOS is offline.

Tier priority (stops when budget exhausted):
  T1 — infrastructure   (~1500 chars) always included
  T2 — decisions        (~1000 chars) architectural decisions
  T2 — solutions        (~1000 chars) known fixes/workarounds
  T3 — projects         (~1500 chars) active project context
  T4 — patterns         (~500 chars)  behavioral patterns (budget permitting)

Total budget: ~5500 chars ≈ ~1375 tokens
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
STATE_FILE = Path.home() / ".claude/session-rehydration-state.json"
TIMEOUT = 4

# Tier definitions: (label, category, limit, char_budget)
# Lower tiers skipped when cumulative budget is exhausted.
TIERS = [
    ("Infrastructure",  "infrastructure",  2, 1500),
    ("Decisions",       "decisions",       3, 1000),
    ("Solutions",       "solutions",       3, 1000),
    ("Active Projects", "projects",        2, 1500),
    ("Patterns",        "patterns",        2,  500),
]

TOTAL_BUDGET = sum(t[3] for t in TIERS)  # 5500 chars


def check_mnemos() -> bool:
    try:
        urllib.request.urlopen(f"{MNEMOS_URL}/health", timeout=2)
        return True
    except Exception:
        return False


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def fetch_tier(category: str, limit: int) -> list[dict]:
    """Fetch recent memories for a category via GET /memories."""
    try:
        url = f"{MNEMOS_URL}/v1/memories?limit={limit}&category={category}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception:
        return []


def main() -> None:
    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    session_id = hook_data.get("session_id", "unknown")

    # Only inject once per session
    state = load_state()
    if state.get("session_id") == session_id:
        return

    if not check_mnemos():
        return  # Silent — MNEMOS offline is non-fatal

    sections = []
    total_chars = 0

    for label, category, limit, tier_budget in TIERS:
        if total_chars >= TOTAL_BUDGET:
            break

        remaining_global = TOTAL_BUDGET - total_chars
        effective_budget = min(tier_budget, remaining_global)

        memories = fetch_tier(category, limit)
        if not memories:
            continue

        parts = []
        tier_chars = 0
        for mem in memories:
            content = (mem.get("content") or "").strip()
            if not content:
                continue
            # Truncate to whatever budget remains in this tier
            available = effective_budget - tier_chars
            if available <= 0:
                break
            chunk = content[:available]
            parts.append(chunk)
            tier_chars += len(chunk)

        if parts:
            sections.append(f"### {label}\n" + "\n\n---\n\n".join(parts))
            total_chars += tier_chars

    if not sections:
        # Nothing retrieved — still mark session to avoid repeated attempts
        save_state({
            "session_id": session_id,
            "rehydrated_at": datetime.now(timezone.utc).isoformat(),
            "chars_injected": 0,
            "empty": True,
        })
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    output = (
        f'<mnemos-context timestamp="{timestamp}" '
        f'chars="{total_chars}/{TOTAL_BUDGET}">\n\n'
        + "\n\n".join(sections)
        + "\n\n</mnemos-context>\n"
    )
    print(output)

    save_state({
        "session_id": session_id,
        "rehydrated_at": datetime.now(timezone.utc).isoformat(),
        "chars_injected": total_chars,
        "tiers_loaded": [t[0] for t in TIERS if total_chars > 0],
    })


if __name__ == "__main__":
    main()
