#!/usr/bin/env python3
"""
mnemos-context-watch.py — proactive early-warning save to MNEMOS.
Triggered by Claude Code's UserPromptSubmit hook, every turn.

PreCompact only fires once context is already full (see mnemos-precompact.py
for how that boundary works). This computes the REAL effective context size
from the transcript's own Anthropic API usage blocks (input_tokens +
cache_read_input_tokens + cache_creation_input_tokens on the last assistant
turn -- there is no official Claude-Code-exposed percentage field; this is
computed directly from data every hook already has access to via
transcript_path) and, the first time it crosses a threshold, saves a
transcript-tail snapshot to MNEMOS -- giving real lead time instead of only
reacting at the compaction boundary. Edge-triggered: fires once per
threshold-crossing, resets once usage drops back down (e.g. after a real
compaction), so it can fire again later in a long session.
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

# No official window-size field is exposed to hooks either. Default assumes
# the 1M-context beta (this account has demonstrably run past 200K tokens
# without erroring -- see mem_ discussion 2026-08-17). Override per-host if
# a session is known to be on the standard 200K window.
CONTEXT_WINDOW_TOKENS = int(os.environ.get("MNEMOS_CONTEXT_WINDOW", "1000000"))
WARN_THRESHOLD_PCT = float(os.environ.get("MNEMOS_CONTEXT_WARN_PCT", "0.85"))
RESET_THRESHOLD_PCT = float(os.environ.get("MNEMOS_CONTEXT_RESET_PCT", "0.50"))

PENDING_DIR = Path.home() / ".claude/mnemos-context-watch-pending"
STATE_DIR = Path.home() / ".claude/mnemos-context-watch-state"
MAX_CONTENT_CHARS = 60_000


def last_turn_usage(transcript_path: str):
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        usage = (entry.get("message") or {}).get("usage")
        if usage:
            return usage
    return None


def extract_transcript_tail(transcript_path: str, max_messages: int = 40) -> str:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"(could not read transcript_path {transcript_path}: {e})"

    turns: list[str] = []
    for line in lines[-max_messages * 3:]:
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
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
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


def post_to_mnemos(content: str, tags: list[str]) -> bool:
    payload = json.dumps({
        "content": content,
        "category": "patterns",
        "source": "claude-code",
        "metadata": {"hook": "context-watch", "tags": tags, "timestamp": datetime.now(timezone.utc).isoformat()},
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
        print(f"[mnemos-context-watch] POST failed: {e}", file=sys.stderr)
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


def load_state(session_id: str) -> dict:
    p = STATE_DIR / f"{session_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"warned": False}


def save_state(session_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{session_id}.json").write_text(json.dumps(state))


def main() -> None:
    flush_pending()

    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    session_id = hook_data.get("session_id", "unknown")
    transcript_path = hook_data.get("transcript_path", "")

    if not transcript_path:
        print("{}")
        return

    usage = last_turn_usage(transcript_path)
    if not usage:
        print("{}")
        return

    effective_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    pct = effective_tokens / CONTEXT_WINDOW_TOKENS

    state = load_state(session_id)

    if pct < RESET_THRESHOLD_PCT and state.get("warned"):
        state["warned"] = False
        save_state(session_id, state)

    if pct >= WARN_THRESHOLD_PCT and not state.get("warned"):
        body = extract_transcript_tail(transcript_path)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = (
            f"Proactive context-warning snapshot [{timestamp}] "
            f"({effective_tokens:,} tokens, {pct:.0%} of {CONTEXT_WINDOW_TOKENS:,}-token window)\n\n{body}"
        )
        tags = ["session-snapshot", "context-warning"]
        saved = post_to_mnemos(content, tags)
        if not saved:
            queue_pending(content, tags)

        state["warned"] = True
        save_state(session_id, state)

        note = (
            f"Context at {pct:.0%} ({effective_tokens:,}/{CONTEXT_WINDOW_TOKENS:,} tokens) -- "
            f"proactive snapshot {'saved to' if saved else 'queued for'} MNEMOS."
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": note,
            }
        }))
        return

    print("{}")


if __name__ == "__main__":
    main()
