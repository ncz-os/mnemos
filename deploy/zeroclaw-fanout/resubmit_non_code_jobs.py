#!/usr/bin/env python3
"""Resubmit hive jobs that didn't produce code, failed, or were cancelled.

Per user directive 2026-05-24: after fleet code-exec rollout, re-run
work that was done as chat-only or didn't complete cleanly.

Filters:
* status=failed → resubmit
* status=cancelled → resubmit
* status=done AND commits=[] AND kind is code-bearing (not smoke/validation)
  → resubmit with [repo:...] directive inferred from kind

Inferred repo by kind prefix:
* mnemos:*           → ncz-os/mnemos (feat/db2-native base)
* mnemos-ic-runtime:*→ ncz-os/mnemos-ic-runtime
* graeae:*           → ncz-os/graeae
* riskyeats:*        → argonautsystems/riskyeats
* cixmini-os:*       → ncz-os/cix-installer
* ncz-os-zeroclaw:*  → ncz-os/zeroclaw
* fleet-infra:*      → no repo (chat-only resubmit)
* others             → no repo (chat-only resubmit)

Usage:
    AGENT_URN=urn:agent:claude:... python3 resubmit_non_code_jobs.py [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BUS_URL = os.environ.get("HIVE_URL", "http://192.168.207.67:5005")
URN = os.environ.get("AGENT_URN")
DRY = "--dry-run" in sys.argv


SMOKE_KIND_RE = re.compile(
    r"^(smoke|halluc|retry-smoke|zeroclaw-validation|hive-stats|"
    r"hive-mind-feature|ping|halluc-smoke|fleet-goose-validate)",
    re.I,
)


REPO_MAP = {
    "mnemos": ("https://gitlab.com/ncz-os/mnemos", "feat/db2-native"),
    "mnemos-ic-runtime": ("https://gitlab.com/ncz-os/mnemos-ic-runtime", "main"),
    "graeae": ("https://gitlab.com/ncz-os/graeae", "main"),
    "riskyeats": ("https://gitlab.com/argonautsystems/riskyeats", "main"),
    "cixmini-os": ("https://gitlab.com/ncz-os/cix-installer", "main"),
    "ncz-os-zeroclaw": ("https://gitlab.com/ncz-os/zeroclaw", "main"),
    "ic-runtime": ("https://gitlab.com/ncz-os/mnemos-ic-runtime", "main"),
}


def http_get_jobs(status: str, limit: int = 1000) -> list[dict]:
    req = urllib.request.Request(f"{BUS_URL}/v1/jobs?status={status}&limit={limit}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("jobs", [])


def http_post_job(payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BUS_URL}/v1/jobs",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def repo_for_kind(kind: str) -> tuple[str | None, str | None]:
    """Return (repo_url, base_branch) for a kind prefix, or (None, None)."""
    prefix = kind.split(":")[0]
    if prefix in REPO_MAP:
        return REPO_MAP[prefix]
    return (None, None)


def build_directive(repo_url: str, base: str, job_id_short: str) -> str:
    branch = f"hive/resubmit-{job_id_short}"
    return f"[repo:{repo_url} branch:{branch} base:{base}]\n\n"


def is_code_bearing_chat_done(j: dict) -> bool:
    kind = j.get("kind") or ""
    if SMOKE_KIND_RE.search(kind):
        return False
    r = j.get("result") or {}
    if r.get("commits"):
        return False
    # Only resubmit if we know the repo
    return repo_for_kind(kind)[0] is not None


def resubmit(j: dict, with_directive: bool) -> dict | None:
    kind = j.get("kind") or "resubmitted"
    desc = j.get("description") or ""
    job_id_short = j["id"][:8]

    if with_directive:
        repo_url, base = repo_for_kind(kind)
        if repo_url:
            directive = build_directive(repo_url, base, job_id_short)
            desc = directive + desc

    payload = {
        "submitter_urn": URN,
        "kind": f"{kind} [resubmit]",
        "description": desc,
        "priority": max(int(j.get("priority", 5)) - 1, 1),
        "eligible_kinds": j.get("eligible_kinds") or ["zeroclaw"],
        "required_capabilities": j.get("required_capabilities") or [],
        "max_cost_tier": j.get("max_cost_tier") or "C",
        "parent_job_id": j["id"],  # link to original
    }
    if DRY:
        print(f"  DRY: would submit kind={payload['kind']} parent={j['id'][:13]}")
        return None
    try:
        out = http_post_job(payload)
        return out
    except urllib.error.HTTPError as e:
        print(f"  ERR: {e.code} {e.read().decode()[:200]}")
        return None


def main() -> int:
    if not URN:
        print("FATAL: set AGENT_URN env var")
        return 1

    print(f"submitter_urn={URN}")
    print(f"bus={BUS_URL}")
    print(f"dry-run={DRY}")
    print()

    failed = http_get_jobs("failed", limit=500)
    cancelled = http_get_jobs("cancelled", limit=500)
    done = http_get_jobs("done", limit=1000)

    chat_done = [j for j in done if is_code_bearing_chat_done(j)]
    print(f"failed     : {len(failed)}")
    print(f"cancelled  : {len(cancelled)}")
    print(f"done-no-code (code-bearing kind only): {len(chat_done)}")
    print()

    total = 0
    submitted = 0
    for batch_name, batch in [("failed", failed), ("cancelled", cancelled), ("done-chat", chat_done)]:
        print(f"=== {batch_name} (n={len(batch)}) ===")
        for j in batch:
            total += 1
            with_dir = batch_name == "done-chat"  # only inject directive when resubmitting done chat-only
            r = resubmit(j, with_directive=with_dir)
            if r and r.get("id"):
                submitted += 1
            if total % 20 == 0:
                time.sleep(1)  # don't flood hive

    print()
    print(f"=== summary ===")
    print(f"total candidates : {total}")
    print(f"submitted        : {submitted}")
    if DRY:
        print("(dry-run; no actual submissions made)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
