#!/usr/bin/env python3
"""
mnemos-commit-capture.py — record every successful git commit/push to MNEMOS.

Triggered by Claude Code's PostToolUse hook on Bash.

WHY THIS EXISTS
    Fleet work commits on REMOTE hosts over ssh (cixmini, TYDEUS, ...), so there
    is no local repo to inspect and nothing on this machine changes when a
    commit lands. The commit trail therefore depended entirely on someone
    remembering to write it down, and on 2026-08-14 a full day of fixes across
    four repos was reconstructed by hand at the end of the session.

    So this parses the TOOL OUTPUT rather than a working tree: the `git log
    --oneline` line printed after a commit, and the `a..b  HEAD -> branch` line
    printed by a push. That is the only signal available for a remote commit.

CONTRACT
    Best effort, never blocks. Any failure — MNEMOS down, malformed JSON, no
    match — exits 0 silently. A hook that breaks the tool call it observes is
    worse than no hook.
"""

import json
import os
import re
import sys
import urllib.request

MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get(
    "MNEMOS_TOKEN", "d4e7b2559a222980decfec838dc54ea67f281b67fe9702ab76640409f8d53a05"
)
TIMEOUT = 4

# `git log --oneline -1` style: <sha> <subject>. Anchored so it cannot match a
# sha appearing mid-sentence in prose.
COMMIT_RE = re.compile(r"^([0-9a-f]{7,40})\s+(\S.*)$", re.M)
# `git push` success: "   a1b2c3d..e4f5g6h  HEAD -> branch" (or "master -> master")
PUSH_RE = re.compile(r"^\s*([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})\s+(\S+)\s*->\s*(\S+)\s*$", re.M)
# ssh target, to attribute the commit to the host it actually happened on.
# user@host is matched FIRST because option-skipping is unreliable: an earlier
# version walked past "-o " and happily reported the host as "ConnectTimeout"
# from `ssh -o ConnectTimeout=20 user@host`. Caught by the pipe test.
SSH_USER_HOST_RE = re.compile(r"(?:\w[\w.-]*)@([\w.-]+)")
# Fallback for `ssh host '...'` with no user: first token after ssh that is
# neither a flag nor an option assignment.
SSH_BARE_RE = re.compile(r"\bssh\s+((?:-\S+\s+|\S+=\S+\s+)*)([\w.-]+)")
# `cd <path>` or `cd ~/path` inside the command, to name the repo
CD_RE = re.compile(r"cd\s+(~?[\w./~-]+)")


def emit(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            f"{MNEMOS_URL}/v1/memories",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MNEMOS_TOKEN}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        # Deliberately silent: see CONTRACT above.
        pass


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    if data.get("tool_name") != "Bash":
        return

    command = (data.get("tool_input") or {}).get("command", "") or ""
    resp = data.get("tool_response")
    if isinstance(resp, dict):
        output = " ".join(
            str(resp.get(k, "")) for k in ("stdout", "output", "stderr", "content")
        )
    else:
        output = str(resp or "")

    # Only care about commit/push activity. Checking the COMMAND for the intent
    # and the OUTPUT for the evidence: a `git commit` that failed prints no sha,
    # so it correctly records nothing.
    wants_commit = bool(re.search(r"git\b.*\bcommit\b", command))
    pushes = PUSH_RE.findall(output)
    commits = COMMIT_RE.findall(output) if wants_commit else []

    if not commits and not pushes:
        return

    host_m = SSH_USER_HOST_RE.search(command)
    if host_m:
        host = host_m.group(1)
    else:
        bare = SSH_BARE_RE.search(command)
        host = bare.group(2) if bare else "local"
    cd_m = CD_RE.search(command)
    repo = cd_m.group(1) if cd_m else "unknown"

    lines = []
    for sha, subject in commits[:4]:
        # Skip lines that are obviously not commit subjects (paths, counts).
        if subject.startswith(("/", "-")) or subject.isdigit():
            continue
        lines.append(f"COMMIT {sha} on {host} [{repo}]: {subject.strip()[:160]}")
    for old, new, src, dst in pushes[:4]:
        lines.append(f"PUSH   {old}..{new} {src} -> {dst} from {host} [{repo}]")

    if not lines:
        return

    emit(
        {
            "content": (
                "AUTO-CAPTURED GIT ACTIVITY (mnemos-commit-capture hook).\n"
                + "\n".join(lines)
                + f"\n\nsession: {data.get('session_id', 'unknown')}"
                + "\nRecorded automatically at the moment the commit/push succeeded, "
                "because fleet commits happen on remote hosts over ssh and leave no "
                "local trace to reconstruct later."
            ),
            "category": "projects",
            "subcategory": "commits",
        }
    )


if __name__ == "__main__":
    main()
