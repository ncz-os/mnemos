#!/usr/bin/env python3
"""Reversible secret-vault backfill.

Scans live memories, classifies them with mnemos.core.secret_detection,
and moves VAULT-class rows into namespace='vault'. Every change is
recorded to a JSON ledger (id, original namespace, original metadata)
so the move is fully reversible. Run with --apply to write; default is
dry-run.

Reverse: feed the ledger to --revert.
"""

import argparse
import json
import os
import re
import sys
import datetime
import oracledb
from mnemos.core.secret_detection import classify, SecretClass, VAULT_NAMESPACE


def connect():
    dsn = os.environ["MNEMOS_DATABASE_DSN"]
    m = re.match(r"oracle://([^:]+):([^@]+)@([^:/]+):?([0-9]+)?/?(.*)", dsn)
    u, pw, h, p, s = m.groups()
    p = p or "1521"
    return oracledb.connect(user=u, password=pw, dsn=f"{h}:{p}/{s}")


def _read(v):
    return v.read() if hasattr(v, "read") else v


def scan(conn):
    """Return (vault_rows, redact_rows).

    ``vault_rows``  — credential-RECORD memories (classify VAULT): moved to
                      namespace='vault' so the default read path excludes
                      them entirely.
    ``redact_rows`` — memories with an INCIDENTAL credential span (classify
                      REDACT): stay in their namespace but get tagged with
                      ``secret_redact_spans`` so the span is masked at
                      retrieval. (redact-at-retrieval also masks them live
                      regardless of the tag; the tag is for auditability.)
    Both are reversible via the same ledger.
    """
    cur = conn.cursor()
    cur.execute("SELECT id, namespace, content, metadata FROM memories WHERE deleted_at IS NULL")
    vault, redact = [], []
    for mid, ns, content, meta in cur:
        if ns == VAULT_NAMESPACE:
            continue
        f = classify(_read(content))
        rec = {"id": mid, "orig_namespace": ns, "orig_metadata": _read(meta), "reasons": f.reasons}
        if f.cls is SecretClass.VAULT:
            vault.append(rec)
        elif f.cls is SecretClass.REDACT:
            rec["spans"] = f.spans
            redact.append(rec)
    return vault, redact


def apply(conn, vault_rows, redact_rows, ledger_path):
    json.dump(
        {
            "created": datetime.datetime.utcnow().isoformat() + "Z",
            "action": "vault-backfill",
            "rows": vault_rows,  # ledger key kept as "rows" for revert compat
            "redact_rows": redact_rows,
        },
        open(ledger_path, "w"),
        indent=2,
    )
    cur = conn.cursor()
    # 1) VAULT moves
    for r in vault_rows:
        try:
            orig = json.loads(r["orig_metadata"]) if r["orig_metadata"] else {}
        except Exception:
            orig = {}
        orig["secret_vaulted"] = True
        orig["secret_reasons"] = r["reasons"]
        orig["secret_original_namespace"] = r["orig_namespace"]
        orig["secret_classified_at"] = "backfill"
        cur.execute(
            "UPDATE memories SET namespace=:ns, metadata=:md, updated=SYSTIMESTAMP WHERE id=:id AND deleted_at IS NULL",
            {"ns": VAULT_NAMESPACE, "md": json.dumps(orig), "id": r["id"]},
        )
    # 2) REDACT tags (namespace UNCHANGED — just metadata)
    for r in redact_rows:
        try:
            orig = json.loads(r["orig_metadata"]) if r["orig_metadata"] else {}
        except Exception:
            orig = {}
        orig["secret_redact_spans"] = r.get("spans")
        orig["secret_reasons"] = r["reasons"]
        orig["secret_classified_at"] = "backfill"
        cur.execute(
            "UPDATE memories SET metadata=:md, updated=SYSTIMESTAMP WHERE id=:id AND deleted_at IS NULL",
            {"md": json.dumps(orig), "id": r["id"]},
        )
    conn.commit()
    return len(vault_rows), len(redact_rows)


def revert(conn, ledger_path):
    led = json.load(open(ledger_path))
    cur = conn.cursor()
    n = 0
    # VAULT moves: restore original namespace + metadata.
    for r in led.get("rows", []):
        cur.execute(
            "UPDATE memories SET namespace=:ns, metadata=:md, updated=SYSTIMESTAMP WHERE id=:id",
            {"ns": r["orig_namespace"], "md": r["orig_metadata"], "id": r["id"]},
        )
        n += cur.rowcount
    # REDACT tags: restore original metadata (namespace was never changed).
    for r in led.get("redact_rows", []):
        cur.execute(
            "UPDATE memories SET metadata=:md, updated=SYSTIMESTAMP WHERE id=:id",
            {"md": r["orig_metadata"], "id": r["id"]},
        )
        n += cur.rowcount
    conn.commit()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", metavar="LEDGER")
    ap.add_argument("--ledger", default="/data/vault_backfill_ledger.json")
    a = ap.parse_args()
    conn = connect()
    if a.revert:
        print("reverted rows:", revert(conn, a.revert))
        sys.exit(0)
    vault_rows, redact_rows = scan(conn)
    print(f"VAULT candidates (move to vault): {len(vault_rows)}")
    print(f"REDACT-tag candidates (incidental span, stay in place): {len(redact_rows)}")
    for r in vault_rows[:30]:
        print("  [VAULT] ", r["id"], r["orig_namespace"], r["reasons"])
    if a.apply:
        nv, nr = apply(conn, vault_rows, redact_rows, a.ledger)
        print(f"APPLIED: vaulted {nv} memories, redact-tagged {nr}; ledger -> {a.ledger}")
    else:
        print("(dry-run; pass --apply to write)")
