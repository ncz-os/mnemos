#!/usr/bin/env python3
"""
mnemos-autocommit.py — Sync new/modified Claude Code memory files to MNEMOS.
Triggered by Claude Code's Stop hook after each turn.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MNEMOS_MEMORY_DIR", str(Path.home() / ".claude/projects/-Users-jasonperlow/memory")))
MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get(
    "MNEMOS_TOKEN", "d4e7b2559a222980decfec838dc54ea67f281b67fe9702ab76640409f8d53a05"
)
STATE_FILE = Path.home() / ".claude/mnemos-sync-state.json"
TIMEOUT = 5

# Maps Claude Code memory types → MNEMOS categories
# MNEMOS categories: infrastructure, solutions, patterns, decisions, projects, standards
TYPE_TO_CATEGORY = {
    "user": "patterns",       # user preferences → behavioral patterns
    "feedback": "decisions",  # corrective feedback → decision rationale
    "project": "projects",    # project context → projects
    "reference": "infrastructure",  # reference info → infrastructure/config
}


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
    STATE_FILE.write_text(json.dumps(state, indent=2))


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (meta dict, body string) from a markdown file with YAML frontmatter."""
    meta: dict = {}
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return meta, raw

    end_idx = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return meta, raw

    for line in lines[1:end_idx]:
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()

    body = "\n".join(lines[end_idx + 1:]).strip()
    return meta, body


def commit_file(filepath: Path) -> bool:
    try:
        raw = filepath.read_text()
    except Exception:
        return False

    meta, body = parse_frontmatter(raw)
    if not body:
        return False

    name = meta.get("name", filepath.stem)
    ftype = meta.get("type", "reference")
    desc = meta.get("description", "")
    category = TYPE_TO_CATEGORY.get(ftype, "facts")

    # Build content string: name + description + body
    parts = [name]
    if desc:
        parts.append(desc)
    parts.append(body)
    content = "\n\n".join(parts)

    payload = json.dumps({
        "content": content,
        "category": category,
        "source": "claude-code",
        "metadata": {
            "source_file": filepath.name,
            "description": desc,
            "memory_name": name,
            "memory_type": ftype,
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
    except urllib.error.HTTPError as e:
        print(f"[mnemos-autocommit] HTTP {e.code} for {filepath.name}: {e.read().decode()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[mnemos-autocommit] Error committing {filepath.name}: {e}", file=sys.stderr)
        return False


def main() -> None:
    if not MEMORY_DIR.exists():
        return

    if not check_mnemos():
        # Silent exit — MNEMOS offline is not an error
        return

    state = load_state()
    new_state = dict(state)
    committed = []

    for filepath in sorted(MEMORY_DIR.glob("*.md")):
        if filepath.name == "MEMORY.md":
            continue

        mtime = str(int(filepath.stat().st_mtime))
        if state.get(filepath.name) == mtime:
            continue  # Unchanged since last sync

        if commit_file(filepath):
            committed.append(filepath.name)
            new_state[filepath.name] = mtime

    if committed:
        save_state(new_state)
        print(f"[mnemos-autocommit] Synced {len(committed)} file(s): {', '.join(committed)}")


if __name__ == "__main__":
    main()
