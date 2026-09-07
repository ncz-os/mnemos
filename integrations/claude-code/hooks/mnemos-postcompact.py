#!/usr/bin/env python3
"""
mnemos-postcompact.py — Save the real compaction summary to MNEMOS.
Triggered by Claude Code's PostCompact hook (2026-08-17: this is the event
that actually carries "summary" — PreCompact never does; see
mnemos-precompact.py's header for how that was confirmed). Complements it:
PreCompact grabs the raw transcript tail before compaction, this grabs
Claude's own condensed summary after compaction produces one.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get(
    "MNEMOS_TOKEN", "d4e7b2559a222980decfec838dc54ea67f281b67fe9702ab76640409f8d53a05"
)
TIMEOUT = 5
PENDING_DIR = Path.home() / ".claude/mnemos-postcompact-pending"


def post_to_mnemos(content: str, tags: list[str]) -> bool:
    payload = json.dumps({
        "content": content,
        "category": "patterns",
        "source": "claude-code",
        "metadata": {"hook": "postcompact", "tags": tags, "timestamp": datetime.now(timezone.utc).isoformat()},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{MNEMOS_URL}/v1/memories",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {MNEMOS_TOKEN}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
        return True
    except Exception as e:
        print(f"[mnemos-postcompact] POST failed: {e}", file=sys.stderr)
        return False


def queue_pending(content: str, tags: list[str]) -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (PENDING_DIR / f"{stamp}.json").write_text(json.dumps({"content": content, "tags": tags}))


def flush_pending() -> None:
    if not PENDING_DIR.exists():
        return
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            path.unlink(missing_ok=True)
            continue
        if post_to_mnemos(data["content"], data.get("tags", [])):
            path.unlink(missing_ok=True)
        else:
            break


def main() -> None:
    flush_pending()
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    summary = hook_data.get("compact_summary", "")
    trigger = hook_data.get("trigger", "auto")
    session_id = hook_data.get("session_id", "unknown")
    if not summary:
        return  # nothing to save this time

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"Compaction summary [{timestamp}] (trigger: {trigger})\n\n{summary}"
    tags = ["session-snapshot", "postcompact", trigger]

    if not post_to_mnemos(content, tags):
        queue_pending(content, tags)
    else:
        print(f"[mnemos-postcompact] Summary saved to MNEMOS (session: {session_id[:8]})")


if __name__ == "__main__":
    main()
