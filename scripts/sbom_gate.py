#!/usr/bin/env python3
"""Release gate for image security scans.

Reads a ``grype -o json`` report and fails when a fixable Critical or High
finding is not covered by an unexpired entry in the accepted-risk file. An
entry names a package and a version glob; ``vuln_id`` is optional and narrows
the match. An expired entry also fails the gate, so every accepted entry gets
re-reviewed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from datetime import date
from pathlib import Path

BLOCKING = {"critical", "high"}
REQUIRED = ("package", "version", "reason", "review_by")


def load_accepted(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    entries = data.get("accepted", []) if isinstance(data, dict) else data
    for i, entry in enumerate(entries):
        missing = [k for k in REQUIRED if not str(entry.get(k, "")).strip()]
        if missing:
            raise ValueError(f"accepted-risk entry {i} is missing {', '.join(missing)}")
        date.fromisoformat(entry["review_by"])
    return entries


def fixable_blocking(report: dict) -> list[dict]:
    found = []
    for match in report.get("matches", []):
        vuln = match.get("vulnerability", {})
        if str(vuln.get("severity", "")).lower() not in BLOCKING:
            continue
        if not vuln.get("fix", {}).get("versions"):
            continue
        artifact = match.get("artifact", {})
        found.append(
            {
                "package": artifact.get("name", ""),
                "version": artifact.get("version", ""),
                "vuln_id": vuln.get("id", ""),
                "severity": vuln.get("severity", ""),
                "fixed_in": ",".join(vuln["fix"]["versions"]),
            }
        )
    return found


def covers(entry: dict, finding: dict) -> bool:
    return (
        entry["package"] == finding["package"]
        and entry.get("vuln_id", finding["vuln_id"]) == finding["vuln_id"]
        and fnmatch.fnmatchcase(finding["version"], entry["version"])
    )


def evaluate(report: dict, accepted: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    expired = [e for e in accepted if date.fromisoformat(e["review_by"]) < today]
    live = [e for e in accepted if e not in expired]
    blocked = [f for f in fixable_blocking(report) if not any(covers(e, f) for e in live)]
    return blocked, expired


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grype", required=True, type=Path)
    parser.add_argument("--accepted", required=True, type=Path)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    report = json.loads(args.grype.read_text())
    accepted = load_accepted(args.accepted)
    blocked, expired = evaluate(report, accepted, args.today)

    findings = fixable_blocking(report)
    print(f"fixable Critical/High findings: {len(findings)}; accepted: {len(findings) - len(blocked)}")
    for e in expired:
        print(f"EXPIRED accepted risk: {e['package']} {e['version']} {e['vuln_id']} (review_by {e['review_by']})")
    for f in blocked:
        print(f"BLOCKED: {f['severity']} {f['package']} {f['version']} {f['vuln_id']} (fixed in {f['fixed_in']})")
    return 1 if blocked or expired else 0


if __name__ == "__main__":
    sys.exit(main())
