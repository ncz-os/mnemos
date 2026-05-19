#!/usr/bin/env python3
"""
Full relative performance harness: PYTHIA (Postgres + MNEMOS API) vs PROTEUS (Oracle raw).
Run from STUDIO. Requires token for PYTHIA and ssh access to PROTEUS.
"""

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

PYTHIA = "http://192.168.207.67:5002"
PROTEUS_HOST = os.environ.get("PROTEUS_HOST", "192.168.207.25")
PROTEUS_USER = os.environ.get("PROTEUS_USER", "root")
PROTEUS = f"{PROTEUS_USER}@{PROTEUS_HOST}"
REQUIRED_ENV = ("MNEMOS_TOKEN", "PROTEUS_SSH_PASS", "ORACLE_PASS")


def require_env():
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Export MNEMOS_TOKEN, PROTEUS_SSH_PASS, and ORACLE_PASS before running."
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


def time_pythia_export(token, limit=100):
    t0 = time.time()
    req = urllib.request.Request(f"{PYTHIA}/v1/export?limit={limit}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return round(time.time() - t0, 3), len(data.get("records", []))


def time_oracle_query(query, proteus_ssh_pass, oracle_pass):
    remote_script = """
import json, oracledb, sys, time
payload = json.load(sys.stdin)
conn = oracledb.connect(user="mnemos", password=payload["oracle_pass"], dsn="localhost:1521/FREEPDB1")
cur = conn.cursor()
t0 = time.time()
cur.execute(payload["query"])
rows = cur.fetchall()
if payload.get("is_count"):
    val = rows[0][0] if rows else 0
    print(val)
else:
    print(len(rows))
print(round(time.time()-t0, 3))
conn.close()
"""
    env = os.environ.copy()
    env["SSHPASS"] = proteus_ssh_pass
    cmd = [
        "sshpass",
        "-e",
        "ssh",
        "-o",
        "StrictHostKeyChecking=yes",
        PROTEUS,
        f"/tmp/oracle-test/bin/python -c {shlex.quote(remote_script)}",
    ]
    payload = {
        "oracle_pass": oracle_pass,
        "query": query,
        "is_count": query.strip().upper().startswith("SELECT COUNT("),
    }
    out = subprocess.check_output(
        cmd,
        env=env,
        input=json.dumps(payload),
        text=True,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    lines = out.strip().split("\n")
    return int(lines[0]), float(lines[1])


def main():
    try:
        env = require_env()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("=== Relative Perf: PYTHIA vs PROTEUS (Oracle) ===\n")

    # PYTHIA API
    py_time, py_count = time_pythia_export(env["MNEMOS_TOKEN"], 100)
    print(f"PYTHIA export100: {py_time}s ({py_count} records)")

    # PROTEUS Oracle
    count, t = time_oracle_query("SELECT COUNT(*) FROM memories", env["PROTEUS_SSH_PASS"], env["ORACLE_PASS"])
    print(f"PROTEUS COUNT: {count} in {t}s")

    infra, t = time_oracle_query(
        "SELECT * FROM memories WHERE category = 'infrastructure'",
        env["PROTEUS_SSH_PASS"],
        env["ORACLE_PASS"],
    )
    print(f"PROTEUS infra filter: {infra} in {t}s")

    scan, t = time_oracle_query(
        "SELECT * FROM memories WHERE ROWNUM <= 100", env["PROTEUS_SSH_PASS"], env["ORACLE_PASS"]
    )
    print(f"PROTEUS scan100: {scan} in {t}s")

    print("\nNote: PYTHIA = full MNEMOS stack + Postgres; PROTEUS = raw Oracle. Need MNEMOS-on-Oracle for parity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


if __name__ == "__main__":
    main()
