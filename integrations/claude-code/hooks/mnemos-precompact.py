#!/usr/bin/env python3
"""
mnemos-precompact.py — Save session working context to MNEMOS before Claude compacts.
Triggered by Claude Code's PreCompact hook.

CORRECTED 2026-08-17 (operator asked how this actually determines context
before compact): PreCompact's payload has no "summary" field -- Claude
Code's own hook docs confirm PostCompact is the one that "receives summary",
PreCompact fires strictly before compaction runs. The only usable field is
transcript_path (+ session_id, trigger); this now reads the transcript's
recent user/assistant turns directly, same file mnemos-stop.sh parses at
session end, but only the tail -- Stop already covers the full session.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get(
    "MNEMOS_TOKEN", "d4e7b2559a222980decfec838dc54ea67f281b67fe9702ab76640409f8d53a05"
)
TIMEOUT = 5

# Bugs fixed 2026-08-17 (operator: "before context compression, commit all
# session work to mnemos"): this hook had been silently failing on EVERY
# invocation, MNEMOS up or down -- wrong endpoint (/memories, missing the
# /v1/ prefix mnemos-autocommit.py's working POST actually uses) and no
# Authorization header at all. Also had no local fallback: a failed POST
# just dropped the content, so an outage at exactly the wrong moment (e.g.
# a MNEMOS upgrade in progress) silently lost the snapshot. Now: correct
# endpoint+auth, and ANY failure (network, auth, MNEMOS down) writes the
# snapshot to a local pending queue that the NEXT successful run flushes
# first, so nothing is dropped only delayed.
PENDING_DIR = Path.home() / ".claude/mnemos-precompact-pending"


def check_mnemos() -> bool:
    try:
        urllib.request.urlopen(f"{MNEMOS_URL}/health", timeout=2)
        return True
    except Exception:
        return False


def post_to_mnemos(content: str, category: str, tags: list[str]) -> bool:
    payload = json.dumps({
        "content": content,
        "category": category,
        "source": "claude-code",
        "metadata": {
            "hook": "precompact",
            "tags": tags,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }).encode()

    try:
        req = urllib.request.Request(
            f"{MNEMOS_URL}/v1/memories",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MNEMOS_TOKEN}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
        return True
    except Exception as e:
        print(f"[mnemos-precompact] POST failed: {e}", file=sys.stderr)
        return False


def queue_pending(content: str, category: str, tags: list[str]) -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = PENDING_DIR / f"{stamp}.json"
    path.write_text(json.dumps({"content": content, "category": category, "tags": tags}))
    print(f"[mnemos-precompact] MNEMOS unreachable — queued locally at {path}", file=sys.stderr)


def flush_pending() -> None:
    if not PENDING_DIR.exists():
        return
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            path.unlink(missing_ok=True)
            continue
        if post_to_mnemos(data["content"], data["category"], data.get("tags", [])):
            path.unlink(missing_ok=True)
        else:
            break  # still down; stop trying the rest, they'll retry next run


def save_to_mnemos(content: str, category: str, tags: list[str]) -> bool:
    flush_pending()
    if post_to_mnemos(content, category, tags):
        return True
    queue_pending(content, category, tags)
    return False


MAX_CONTENT_CHARS = 60_000  # MNEMOS rejects >5MB bodies; keep well under it


def extract_transcript_tail(transcript_path: str, max_messages: int = 40) -> str:
    """Pull the last N user/assistant text turns out of the JSONL transcript.

    PreCompact's own hook payload carries no summary (verified 2026-08-17 --
    that field only exists on PostCompact, after compaction runs). The only
    thing available before compaction is transcript_path, so this reads it
    directly -- same file mnemos-stop.sh already parses at session end, but
    here only the TAIL matters: Stop already captures the whole session, so
    this only needs to cover what compaction is about to fold away.
    Tool-result blocks are skipped (large paste-ins/file dumps, not useful
    as a narrative snapshot); only role + text content survives.
    """
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"(could not read transcript_path {transcript_path}: {e})"

    turns: list[str] = []
    for line in lines[-max_messages * 3:]:  # generous over-read; filtered below
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("isMeta") or entry.get("isCompactSummary"):
            continue
        msg = entry.get("message") or {}
        role = msg.get("role") or entry.get("type")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""
        text = text.strip()
        if text:
            turns.append(f"[{role}] {text}")

    turns = turns[-max_messages:]
    tail = "\n\n".join(turns)
    if len(tail) > MAX_CONTENT_CHARS:
        tail = "...(truncated)...\n" + tail[-MAX_CONTENT_CHARS:]
    return tail or "(transcript had no extractable user/assistant text turns)"


def main() -> None:
    # Always attempt save_to_mnemos regardless of check_mnemos(): it flushes
    # any previously-queued snapshots first and queues locally on failure,
    # so an outage must never turn into a silent no-op here.

    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    trigger = hook_data.get("trigger", "auto")
    session_id = hook_data.get("session_id", "unknown")
    transcript_path = hook_data.get("transcript_path", "")

    if transcript_path:
        body = extract_transcript_tail(transcript_path)
    else:
        body = "(no transcript_path in the PreCompact hook payload)"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"Session context snapshot [{timestamp}] (trigger: {trigger}, about to compact)\n\n{body}"

    tags = ["session-snapshot", "precompact", trigger]

    if save_to_mnemos(content, "patterns", tags):
        print(f"[mnemos-precompact] Context snapshot saved to MNEMOS (session: {session_id[:8]})")


if __name__ == "__main__":
    main()
