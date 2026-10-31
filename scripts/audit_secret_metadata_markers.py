#!/usr/bin/env python3
"""Audit memories whose metadata carries a caller-planted ``secret_*`` marker.

M1 (code-health decisions 2026-10-31): ``secret_redact_fields`` /
``secret_redact_spans`` are the SERVER-ONLY stored-span marker the read path
trusts ahead of recomputing the classifier. Before the fix, a caller could
plant a marker through a metadata write (``PATCH /v1/memories/{id}`` and the
other write paths stored ``request.metadata`` as sent), e.g.

    {"secret_redact_fields": {"content": [[0, 0]]}}

a span list that masks nothing -- which permanently switched OFF read-time
masking for that row. Rows written before the fix can still carry a planted
marker, and a degenerate one still disarms ``redact_field_with_stored`` unless
the read path notices it masks nothing (it does now, as a backstop).

This script is READ-ONLY. It runs plain ``SELECT`` statements, writes nothing,
migrates nothing, and takes no locks beyond the scan. Run it deliberately,
against one database at a time:

    # SQLite
    python3 scripts/audit_secret_metadata_markers.py --dsn sqlite:////data/mnemos.db

    # Postgres / MariaDB / Oracle / Db2 (credential comes from the DSN)
    python3 scripts/audit_secret_metadata_markers.py --dsn "$MNEMOS_DATABASE_DSN"

    # Or scan a JSON export instead of a live database
    python3 scripts/audit_secret_metadata_markers.py --json memories.json

Output: one line per suspicious row (id, owner, namespace, the offending keys,
and why it is suspicious), plus a summary on stderr. Exit status is 0 whether
or not rows are found -- this is a report, not a gate. Rows are flagged when:

  * a ``secret_*`` key holds a value the server would never write for that key
    (wrong type/shape), or
  * ``secret_redact_fields`` / ``secret_redact_spans`` hold a span list that
    masks nothing (degenerate: empty, inverted, or zero-length spans), or
  * ``secret_classified_at`` is not one of the server's own stamps.

The script never prints memory CONTENT -- only ids and metadata keys -- so it
can be run in a terminal or pasted into a ticket without leaking a credential.

NOTE: this script is delivered for the operator to run. It must NOT be run
against a live database as part of the change that introduced it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Iterable, Iterator, Mapping

# Stamps the server is allowed to write into ``secret_classified_at``. Kept in
# lockstep with every ``classified_at=`` literal passed to
# ``classify_persisted_text_fields`` in the tree: a missing entry flags
# perfectly normal rows as forged, and a false positive in a security audit
# costs an operator real time. ``import`` / ``migration`` were never written by
# any caller; ``federation`` as a bare stamp is ``federation_pull``.
SERVER_CLASSIFIED_AT = {
    "ingest",
    "bulk_ingest",
    "patch",
    "document_import",
    "session_ingest",
    "mpf_record_import",
    "mpf_compression_import",
    "mpf_compression_manifest",
    "federation_nats",
    "federation_pull",
}

SPAN_KEYS = ("secret_redact_fields", "secret_redact_spans")

# M6 (2026-10-31): ``secret_field_classifiers`` holds the per-field "CLEAN
# under classifier vN" marker the read path trusts to skip its recompute. The
# server writes ``{field: <current classifier version>}`` for fields it
# examined and found clean, so any other shape (unknown version, bool,
# non-object) is not something the server produced. Deliberately NOT imported
# from ``mnemos``: this script carries no package dependency.
MAX_CLASSIFIER_VERSION_KEY = "secret_field_classifiers"
MAX_CLASSIFIER_VERSION = 1


def _decode_metadata(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _spans_mask_anything(spans: Any) -> bool:
    """Whether a span list would replace at least one character of content.

    Content length is unknown here (read-only audit, we deliberately do not
    print or load content), so this checks the structural property: at least
    one span has ``end > start`` with a non-negative start. A list of
    ``[0, 0]`` / ``[]`` / ``[[5, 5]]`` / ``[[10, 3]]`` masks nothing.
    """
    if not isinstance(spans, (list, tuple)):
        return False
    for span in spans:
        if not isinstance(span, (list, tuple)) or len(span) < 2:
            continue
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError):
            continue
        if start < 0:
            continue
        if end > start:
            return True
    return False


def find_suspicious_keys(metadata: Mapping[str, Any]) -> list[str]:
    """Return the ``secret_*`` keys whose value the server would never write."""
    problems: list[str] = []

    for key in SPAN_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        if key == "secret_redact_fields":
            if not isinstance(value, Mapping):
                problems.append(f"{key}:not_an_object")
                continue
            for field, spans in value.items():
                if not _spans_mask_anything(spans):
                    problems.append(f"{key}.{field}:masks_nothing")
            if not value:
                # The server only writes this map when it found spans, so an
                # empty object is a blanked marker, not a clean row.
                problems.append(f"{key}:empty")
        else:
            if not _spans_mask_anything(value):
                problems.append(f"{key}:masks_nothing")

    if MAX_CLASSIFIER_VERSION_KEY in metadata:
        marker = metadata[MAX_CLASSIFIER_VERSION_KEY]
        if not isinstance(marker, Mapping) or not marker:
            problems.append(f"{MAX_CLASSIFIER_VERSION_KEY}:not_a_field_map")
        else:
            for field, version in marker.items():
                if isinstance(version, bool) or not isinstance(version, int):
                    problems.append(f"{MAX_CLASSIFIER_VERSION_KEY}.{field}:not_a_version")
                elif version < 1 or version > MAX_CLASSIFIER_VERSION:
                    problems.append(f"{MAX_CLASSIFIER_VERSION_KEY}.{field}:unknown_version({version!r})")

    stamped = metadata.get("secret_classified_at")
    if stamped is not None and stamped not in SERVER_CLASSIFIED_AT:
        problems.append(f"secret_classified_at:unknown_stamp({stamped!r})")

    return problems


def _iter_sqlite(path: str) -> Iterator[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = conn.execute("SELECT id, owner_id, namespace, metadata FROM memories WHERE metadata IS NOT NULL")
        for row in cursor:
            yield {"id": row[0], "owner_id": row[1], "namespace": row[2], "metadata": row[3]}
    finally:
        conn.close()


def _iter_psql(dsn: str) -> Iterator[dict[str, Any]]:
    # ``psql`` is used rather than a Python driver so the script carries no new
    # dependency and cannot accidentally connect with write permission.
    sql = (
        "COPY (SELECT id, owner_id, namespace, metadata::text "
        "FROM memories WHERE metadata IS NOT NULL) TO STDOUT WITH (FORMAT csv)"
    )
    proc = subprocess.run(
        [shutil.which("psql") or "psql", dsn, "-X", "-q", "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    import csv
    import io

    reader = csv.reader(io.StringIO(proc.stdout))
    for row in reader:
        if len(row) < 4:
            continue
        yield {"id": row[0], "owner_id": row[1], "namespace": row[2], "metadata": row[3]}


def _iter_json(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("memories", payload) if isinstance(payload, Mapping) else payload
    for row in rows or []:
        if isinstance(row, Mapping):
            yield dict(row)


def scan(rows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    """Print one line per suspicious row; return (scanned, flagged)."""
    scanned = 0
    flagged = 0
    for row in rows:
        scanned += 1
        metadata = _decode_metadata(row.get("metadata"))
        if not any(str(k).startswith("secret_") for k in metadata):
            continue
        problems = find_suspicious_keys(metadata)
        if not problems:
            continue
        flagged += 1
        print(
            json.dumps(
                {
                    "id": row.get("id"),
                    "owner_id": row.get("owner_id"),
                    "namespace": row.get("namespace"),
                    "problems": problems,
                    "secret_keys": sorted(str(k) for k in metadata if str(k).startswith("secret_")),
                },
                sort_keys=True,
            )
        )
    return scanned, flagged


def _sqlite_path_from_dsn(dsn: str) -> str:
    if dsn.startswith("sqlite:///"):
        return dsn[len("sqlite:///") :]
    if dsn.startswith("sqlite://"):
        return dsn[len("sqlite://") :]
    return dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dsn", help="MNEMOS DSN: sqlite:///path or postgres://... (read-only scan)")
    source.add_argument("--json", help="Path to a JSON export of memories")
    args = parser.parse_args(argv)

    if args.dsn:
        if args.dsn.startswith("sqlite://"):
            rows = _iter_sqlite(_sqlite_path_from_dsn(args.dsn))
        elif args.dsn.startswith(("postgres://", "postgresql://")):
            rows = _iter_psql(args.dsn)
        else:
            print(
                f"unsupported DSN scheme for this audit script: {args.dsn.split(':', 1)[0]}. "
                "Use --dsn for sqlite/postgres, or export to JSON and use --json.",
                file=sys.stderr,
            )
            return 2
    else:
        rows = _iter_json(args.json)

    scanned, flagged = scan(rows)
    print(
        f"scanned {scanned} memories; flagged {flagged} with a suspicious secret_* marker",
        file=sys.stderr,
    )
    if flagged:
        print(
            "Review each id, then re-save the memory (which re-stamps classification "
            "server-side) or clear the planted metadata key.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
