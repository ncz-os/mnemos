#!/usr/bin/env python3
"""
mpf_validate.py — validate an MPF envelope against the packaged MPF v0.1 schema.

Standalone. Any memory system (Mem0, Letta, Graphiti, Cognee, MNEMOS,
MemPalace) can use this to validate its own MPF emissions before
shipping them. The schema file it validates against is the authoritative
wire-format definition.

Usage:
  python -m mnemos.tools.mpf_validate --file export.json
  python -m mnemos.tools.mpf_validate --file - < export.json        # stdin
  python -m mnemos.tools.mpf_validate --file export.json --schema schema.json

Exit codes:
  0 — envelope validates
  1 — validation failed (prose error list printed to stderr)
  2 — I/O or schema-load error

The required `jsonschema` dependency performs authoritative validation.
If it is unavailable in a broken installation, validation fails closed;
operators can explicitly choose structural-only checks with --no-schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Resolve inside the installed package, not against a repository-only docs path.
DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "domain" / "portability" / "vendor" / "mpf-v0.1.json"


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _structural_check(env: Any) -> List[str]:
    """Minimal shape check used before schema validation or by --no-schema."""
    errs: List[str] = []
    if not isinstance(env, dict):
        return ["envelope must be a JSON object"]
    for k in ("mpf_version", "exported_at", "records"):
        if k not in env:
            errs.append(f"envelope missing required field: {k!r}")
    records = env.get("records")
    if records is not None and not isinstance(records, list):
        errs.append("'records' must be an array")
        return errs
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            errs.append(f"records[{i}] is not an object")
            continue
        for k in ("id", "kind", "payload_version", "payload"):
            if k not in rec:
                errs.append(f"records[{i}] missing required field: {k!r}")
    # Record-id uniqueness (critical round-trip invariant)
    seen: Dict[str, int] = {}
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if not isinstance(rid, str):
            continue
        if rid in seen:
            errs.append(f"records[{i}] duplicate id {rid!r} (already seen at records[{seen[rid]}])")
        else:
            seen[rid] = i
    return errs


def _full_check(env: Any, schema: Any) -> List[str]:
    """Run the full JSON Schema validation via the jsonschema package."""
    try:
        from jsonschema.validators import Draft202012Validator

        # JSON Schema's `format` keyword (date-time, email, uri, ...) is
        # advisory by default; without a FormatChecker, `format: date-time`
        # in the bundled schema accepts invalid values like
        # "2026-13-40T99:99:99Z". Wire the format checkers in so
        # `date-time` is actually validated (a sidecar that round-trips
        # a malformed value would otherwise reach the DB layer and be
        # silently coerced to None or to NOW).
        from jsonschema import FormatChecker
    except ImportError as exc:
        return [f"schema validation unavailable: {exc}"]
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
    except Exception as exc:
        return [f"schema load error: {exc}"]
    errs: List[str] = []
    for e in sorted(validator.iter_errors(env), key=lambda x: list(x.path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"{loc}: {e.message}")
    return errs


def validate(env: Any, schema: Optional[Any]) -> List[str]:
    """Run structural + full schema checks. Structural always runs; full
    runs whenever a schema was loaded and fails if jsonschema is unavailable."""
    errs = _structural_check(env)
    # Avoid duplicating structural errors when full-schema would catch
    # the same thing; run full-schema only if structural passed.
    if not errs and schema is not None:
        errs.extend(_full_check(env, schema))
    return errs


def summary(env: Any) -> str:
    if not isinstance(env, dict):
        return "(not an envelope)"
    records = env.get("records") or []
    by_kind: Dict[str, int] = {}
    for rec in records:
        if isinstance(rec, dict):
            k = rec.get("kind", "<missing>")
            by_kind[k] = by_kind.get(k, 0) + 1
    sidecar_counts = {
        k: len(env.get(k) or [])
        for k in ("kg_triples", "relations", "compression_manifest", "memory_versions", "attestations")
        if env.get(k)
    }
    parts = [
        f"mpf_version={env.get('mpf_version')!r}",
        f"source_system={env.get('source_system')!r}",
        f"records={len(records)}",
    ]
    if by_kind:
        parts.append("kinds=" + ",".join(f"{k}:{v}" for k, v in sorted(by_kind.items())))
    if sidecar_counts:
        parts.append("sidecars=" + ",".join(f"{k}:{v}" for k, v in sidecar_counts.items()))
    return " ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mpf_validate",
        description=(
            "Validate an MPF envelope against the packaged MPF v0.1 schema. Part "
            "of CHARON, MNEMOS's memory portability subsystem. Schema "
            "file is authoritative; this tool is a convenience runner."
        ),
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="Path to envelope JSON file, or '-' for stdin")
    parser.add_argument(
        "--schema", default=str(DEFAULT_SCHEMA), metavar="PATH", help=f"Path to schema file (default: {DEFAULT_SCHEMA})"
    )
    parser.add_argument("--quiet", action="store_true", help="Only print errors; no summary line")
    parser.add_argument("--no-schema", action="store_true", help="Skip full JSON Schema check; run structural only")
    args = parser.parse_args(argv)

    try:
        env = _load_json(args.file)
    except Exception as exc:
        print(f"ERROR reading {args.file}: {exc}", file=sys.stderr)
        return 2

    schema: Optional[Any] = None
    if not args.no_schema:
        try:
            schema = _load_json(args.schema)
        except Exception as exc:
            print(f"ERROR loading schema {args.schema}: {exc}", file=sys.stderr)
            return 2

    errs = validate(env, schema)

    if not args.quiet:
        print(summary(env), file=sys.stderr)

    if errs:
        print(f"VALIDATION FAILED ({len(errs)} error(s)):", file=sys.stderr)
        for e in errs:
            print(f"  {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
