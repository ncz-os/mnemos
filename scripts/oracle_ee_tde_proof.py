"""EE feature #3 — TDE on USERS tablespace.

Configures Oracle 23ai TDE wallet at CDB level, opens it, creates a
master encryption key, then ALTERs USERS tablespace to encrypt with
AES256 ONLINE. Verifies via DBA_TABLESPACES.ENCRYPTED column and by
checking V$ENCRYPTED_TABLESPACES.

Designed to be run from STUDIO against the EE PDB on PROTEUS.
Uses sysdba via the container shell since wallet management requires
local-host privileges.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY or HMAC_KEY == "mnemos-oracle-proof-v1":
    print("ERROR: MNEMOS_PROOF_HMAC_KEY env var required (fail-closed).", file=sys.stderr)
    sys.exit(1)
HMAC_KEY = HMAC_KEY.encode("utf-8")


def _hmac(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac_new(HMAC_KEY, canon, sha256).hexdigest()


def docker_sqlplus(args, sql: str, service: str = "ORCLCDB", sysdba: bool = True) -> tuple[int, str]:
    """Run sqlplus inside the EE container via SSH. ``args`` is required
    (carries host/container/sys_pwd) — no hardcoded fallback so the
    Workstream C4 secret-hygiene fix cannot be silently bypassed.
    """
    if args is None or not all(hasattr(args, a) for a in ("host", "container", "sys_pwd")):
        raise ValueError("docker_sqlplus requires args with host, container, sys_pwd")
    auth = f"sys/{args.sys_pwd}@localhost:1521/" + service + (" as sysdba" if sysdba else "")
    full = f"set heading on feedback on serveroutput on\n{sql}\nEXIT\n"
    cmd = [
        "ssh",
        f"jasonperlow@{args.host}",
        f"sudo docker exec -i {args.container} bash -c \"sqlplus -s '{auth}'\"",
    ]
    result = subprocess.run(cmd, input=full, capture_output=True, text=True, timeout=120)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Oracle EE TDE proof (Workstream C2-C4).")
    ap.add_argument("--host", default=os.environ.get("PROTEUS_HOST", "192.168.207.25"), help="Target host IP")
    ap.add_argument(
        "--container", default=os.environ.get("ORACLE_CONTAINER", "mnemos-oracle-ee"), help="Docker container name"
    )
    ap.add_argument("--sys-pwd", default=os.environ.get("ORACLE_SYS_PWD", "mnemos_dev"), help="SYS password")
    ap.add_argument(
        "--wallet-pwd", default=os.environ.get("MNEMOS_TDE_WALLET_PWD", "Welcome1Wallet!"), help="TDE wallet password"
    )
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    probes = []

    def probe(name: str, ok: bool, ev: dict, err: str | None = None) -> None:
        probes.append({"name": name, "outcome": "pass" if ok else "fail", "evidence": ev, "error": err})

    # 1. Configure WALLET_ROOT + TDE_CONFIGURATION (already set? check)
    rc, out = docker_sqlplus(
        args, "SELECT NAME, VALUE FROM v$parameter WHERE NAME IN ('wallet_root', 'tde_configuration');"
    )
    probe("wallet_param.read", rc == 0, {"out": out[-600:]})

    # 2. Set WALLET_ROOT if unset
    set_sql = """
ALTER SYSTEM SET WALLET_ROOT='/opt/oracle/admin/ORCLCDB/wallet' SCOPE=SPFILE;
"""
    rc, out = docker_sqlplus(args, set_sql)
    probe("wallet_root.set", rc == 0 and ("System altered" in out or "ORA-32017" in out), {"out": out[-300:]})

    # 3. TDE_CONFIGURATION
    rc, out = docker_sqlplus(args, "ALTER SYSTEM SET TDE_CONFIGURATION='KEYSTORE_CONFIGURATION=FILE' SCOPE=SPFILE;")
    probe("tde_config.set", rc == 0 and "System altered" in out, {"out": out[-300:]})

    # 4. Restart container to pick up SPFILE changes
    restart = subprocess.run(
        ["ssh", f"jasonperlow@{args.host}", f"sudo docker restart {args.container}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    probe("container.restart", restart.returncode == 0, {"rc": restart.returncode, "out": restart.stdout[:200]})

    # Wait for ready with success tracking
    ready = False
    for i in range(60):
        chk = subprocess.run(
            [
                "ssh",
                f"jasonperlow@{args.host}",
                f"sudo docker exec {args.container} bash -c \"echo 'SELECT 1 FROM DUAL;' | sqlplus -s sys/{args.sys_pwd}@localhost:1521/ORCLPDB1 as sysdba 2>&1\"",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if chk.returncode == 0 and "1" in chk.stdout and "ORA-" not in chk.stdout.upper():
            ready = True
            break
        time.sleep(5)
    probe("container.restart_ready", ready, {"ready": ready, "attempts": i + 1})
    if not ready:
        print("ERROR: Container did not become ready after restart.", file=sys.stderr)
        return 1

    # 5. Create + open keystore at CDB level
    setup_sql = f"""
ADMINISTER KEY MANAGEMENT CREATE KEYSTORE IDENTIFIED BY "{args.wallet_pwd}";
ADMINISTER KEY MANAGEMENT SET KEYSTORE OPEN IDENTIFIED BY "{args.wallet_pwd}" CONTAINER=ALL;
ADMINISTER KEY MANAGEMENT SET KEY IDENTIFIED BY "{args.wallet_pwd}" WITH BACKUP CONTAINER=ALL;
SELECT STATUS, WRL_TYPE, WRL_PARAMETER FROM v$encryption_wallet WHERE con_id=1;
"""
    rc, out = docker_sqlplus(args, setup_sql)
    probe(
        "keystore.create_open_setkey",
        rc == 0 and ("WALLET" in out.upper() or "OPEN" in out.upper()),
        {"out": out[-600:]},
    )

    # 6. Move into ORCLPDB1 and open keystore there
    pdb_setup = f"""
ALTER SESSION SET CONTAINER=ORCLPDB1;
ADMINISTER KEY MANAGEMENT SET KEY IDENTIFIED BY "{args.wallet_pwd}" WITH BACKUP;
SELECT STATUS, WRL_TYPE FROM v$encryption_wallet;
"""
    rc, out = docker_sqlplus(args, pdb_setup)
    probe("pdb.keystore_setkey", rc == 0, {"out": out[-500:]})

    # 7. Encrypt USERS tablespace ONLINE
    encrypt_sql = """
ALTER SESSION SET CONTAINER=ORCLPDB1;
ALTER TABLESPACE USERS ENCRYPTION ONLINE USING 'AES256' ENCRYPT;
SELECT TABLESPACE_NAME, ENCRYPTED FROM dba_tablespaces WHERE tablespace_name='USERS';
SELECT TS.NAME, ETS.ENCRYPTIONALG FROM v$encrypted_tablespaces ETS JOIN v$tablespace TS ON ETS.TS#=TS.TS#;
"""
    rc, out = docker_sqlplus(args, encrypt_sql)
    ok = rc == 0 and "AES256" in out.upper() and "YES" in out.upper()
    probe("tablespace.encrypt_users", ok, {"out": out[-800:]})

    # 8. Sanity-check: existing data still readable
    rc, out = docker_sqlplus(
        args,
        "ALTER SESSION SET CONTAINER=ORCLPDB1;\nSELECT COUNT(*) FROM MNEMOS.MEMORIES;",
    )
    probe("data.still_readable_post_encrypt", rc == 0 and "ORA-" not in out.upper(), {"out": out[-300:]})

    finished = datetime.now(timezone.utc).isoformat()

    # Pull DB version
    rc, out = docker_sqlplus(args, "SELECT BANNER FROM v$version FETCH FIRST 1 ROWS ONLY;")
    db_version = out.split("\n")[-3].strip() if "Oracle" in out else "?"

    evidence = {
        "schema": "mnemos-oracle-ee-tde/v1",
        "run_id": uuid.uuid4().hex[:12],
        "started_utc": started,
        "finished_utc": finished,
        "db_version": db_version,
        "host": args.host,
        "container": args.container,
        "wallet_root": "/opt/oracle/admin/ORCLCDB/wallet",
        "encryption_algorithm": "AES256",
        "probes": probes,
        "passed": sum(1 for p in probes if p["outcome"] == "pass"),
        "total": len(probes),
    }

    artifact = {
        "evidence": evidence,
        "hmac_key_id": sha256(HMAC_KEY).hexdigest()[:16],
        "hmac_sha256": _hmac(evidence),
        "note": "post-key-rotation-2026-05-20",
    }
    out_path = (
        REPO_ROOT / "docs" / "proof" / f"oracle-ee-tde-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"wrote {out_path}")
    print(f"passed: {evidence['passed']}/{evidence['total']}")
    for p in probes:
        print(f"  {p['name']:38} {p['outcome']:6}", p.get("error") or "")
    if not evidence.get("ready", True):
        return 1
    return 0 if evidence["passed"] == evidence["total"] else 2


if __name__ == "__main__":
    sys.exit(main())
