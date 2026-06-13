#!/usr/bin/env python3
"""Reversible secret-vault backfill.

Scans live memories, classifies them with mnemos.core.secret_detection,
and moves VAULT-class rows into namespace='vault'. Every change is
recorded to a JSON ledger (id, original namespace, original metadata)
so the move is fully reversible. Run with --apply to write; default is
dry-run.

Reverse: feed the ledger to --revert.
"""
import argparse, json, os, re, sys, datetime
import oracledb
from mnemos.core.secret_detection import classify, SecretClass, VAULT_NAMESPACE

def connect():
    dsn = os.environ["MNEMOS_DATABASE_DSN"]
    m = re.match(r"oracle://([^:]+):([^@]+)@([^:/]+):?([0-9]+)?/?(.*)", dsn)
    u, pw, h, p, s = m.groups(); p = p or "1521"
    return oracledb.connect(user=u, password=pw, dsn=f"{h}:{p}/{s}")

def _read(v):
    return v.read() if hasattr(v, "read") else v

def scan(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, namespace, content, metadata FROM memories WHERE deleted_at IS NULL")
    vault = []
    for mid, ns, content, meta in cur:
        if ns == VAULT_NAMESPACE:
            continue
        f = classify(_read(content))
        if f.cls is SecretClass.VAULT:
            vault.append({"id": mid, "orig_namespace": ns,
                          "orig_metadata": _read(meta), "reasons": f.reasons})
    return vault

def apply(conn, rows, ledger_path):
    json.dump({"created": datetime.datetime.utcnow().isoformat()+"Z",
               "action": "vault-backfill", "rows": rows},
              open(ledger_path, "w"), indent=2)
    cur = conn.cursor()
    for r in rows:
        try:
            orig = json.loads(r["orig_metadata"]) if r["orig_metadata"] else {}
        except Exception:
            orig = {}
        orig["secret_vaulted"] = True
        orig["secret_reasons"] = r["reasons"]
        orig["secret_original_namespace"] = r["orig_namespace"]
        orig["secret_classified_at"] = "backfill"
        cur.execute(
            "UPDATE memories SET namespace=:ns, metadata=:md, updated=SYSTIMESTAMP "
            "WHERE id=:id AND deleted_at IS NULL",
            {"ns": VAULT_NAMESPACE, "md": json.dumps(orig), "id": r["id"]},
        )
    conn.commit()
    return len(rows)

def revert(conn, ledger_path):
    led = json.load(open(ledger_path))
    cur = conn.cursor()
    n = 0
    for r in led["rows"]:
        cur.execute(
            "UPDATE memories SET namespace=:ns, metadata=:md, updated=SYSTIMESTAMP "
            "WHERE id=:id",
            {"ns": r["orig_namespace"], "md": r["orig_metadata"], "id": r["id"]},
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
        print("reverted rows:", revert(conn, a.revert)); sys.exit(0)
    rows = scan(conn)
    print(f"VAULT candidates: {len(rows)}")
    for r in rows[:50]:
        print(" ", r["id"], r["orig_namespace"], r["reasons"])
    if a.apply:
        n = apply(conn, rows, a.ledger)
        print(f"APPLIED: vaulted {n} memories; ledger -> {a.ledger}")
    else:
        print("(dry-run; pass --apply to write)")
