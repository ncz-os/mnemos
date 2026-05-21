"""EE feature #5 — JSON Relational Duality View on memories.

Creates a Duality View that exposes the relational `memories` table
as a JSON document collection. Proves bidirectional access:
- INSERT relational row → visible as JSON document
- INSERT JSON document via duality view → row materialized
- UPDATE JSON sub-field → relational column updates
- DELETE JSON document → row deleted

Emits HMAC-signed JSON artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracledb  # noqa: E402


def _str(v):
    """Coerce CLOB / unknown objects to str for JSON serialization."""
    if v is None:
        return None
    if hasattr(v, "read"):
        try:
            return v.read()
        except Exception:
            return str(v)
    return v


DEFAULT_DSN = "192.168.207.25:1521/ORCLPDB1"
DEFAULT_USER = "MNEMOS"
DEFAULT_PWD = "mnemos_dev"
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY or HMAC_KEY == "mnemos-oracle-proof-v1":
    print("ERROR: MNEMOS_PROOF_HMAC_KEY env var required (fail-closed).", file=sys.stderr)
    sys.exit(1)
HMAC_KEY = HMAC_KEY.encode("utf-8")


def _hmac(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac_new(HMAC_KEY, canon, sha256).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PWD)
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    conn = oracledb.connect(user=args.user, password=args.password, dsn=args.dsn)
    cur = conn.cursor()
    probes = []

    def probe(name: str, ok: bool, evidence: dict, error: str | None = None) -> None:
        probes.append(
            {
                "name": name,
                "outcome": "pass" if ok else "fail",
                "evidence": evidence,
                "error": error,
            }
        )

    # 1. Drop+create duality view
    try:
        cur.execute("DROP VIEW memory_doc_v")
    except Exception:
        pass

    # Duality view must reference the table by name + define which columns
    # the JSON document exposes + which are updatable.
    # Schema reference: https://docs.oracle.com/en/database/oracle/oracle-database/23/jsnvu/
    try:
        cur.execute(
            """
            CREATE OR REPLACE JSON RELATIONAL DUALITY VIEW memory_doc_v AS
            memories @insert @update @delete
            {
                _id          : id,
                owner_id     : owner_id,
                namespace    : namespace,
                content      : content,
                category     : category,
                subcategory  : subcategory,
                created      : created,
                updated      : updated
            }
            """
        )
        conn.commit()
        probe("duality_view.create", True, {"view": "MEMORY_DOC_V"})
    except Exception as exc:
        probe("duality_view.create", False, {}, str(exc))
        # Fail-fast; downstream probes need the view
        raise

    # 2. Insert via SQL row, read via JSON document
    relational_id = str(uuid.uuid4())
    try:
        cur.execute(
            """
            INSERT INTO memories (id, owner_id, namespace, content, category,
                                  created, updated, permission_mode)
            VALUES (:id, 'ee-dual', 'docproof', 'inserted via SQL row',
                    'demo', SYSTIMESTAMP, SYSTIMESTAMP, 0)
            """,
            id=relational_id,
        )
        conn.commit()
        cur.execute(
            "SELECT JSON_SERIALIZE(data) FROM memory_doc_v WHERE JSON_VALUE(data, '$._id') = :id",
            id=relational_id,
        )
        row = cur.fetchone()
        doc = json.loads(row[0]) if row else None
        ok = doc is not None and doc.get("content") == "inserted via SQL row"
        probe("relational_to_json", ok, {"id": relational_id, "doc_content": _str(doc.get("content")) if doc else None})
    except Exception as exc:
        probe("relational_to_json", False, {"id": relational_id}, str(exc))

    # 3. Insert via JSON document, read via SQL row
    json_id = str(uuid.uuid4())
    new_doc = {
        "_id": json_id,
        "owner_id": "ee-dual",
        "namespace": "docproof",
        "content": "inserted via JSON document",
        "category": "demo",
        "subcategory": "json-insert",
    }
    try:
        cur.execute(
            "INSERT INTO memory_doc_v VALUES (:d)",
            d=json.dumps(new_doc),
        )
        conn.commit()
        cur.execute("SELECT content FROM memories WHERE id = :id", id=json_id)
        relational_row = cur.fetchone()
        ok = relational_row is not None and _str(relational_row[0]) == "inserted via JSON document"
        probe(
            "json_to_relational",
            ok,
            {"id": json_id, "row_content": _str(relational_row[0]) if relational_row else None},
        )
    except Exception as exc:
        probe("json_to_relational", False, {"id": json_id}, str(exc))

    # 4. UPDATE JSON field reflects in relational column
    try:
        cur.execute(
            """
            UPDATE memory_doc_v
            SET data = JSON_TRANSFORM(data, SET '$.content' = 'updated via JSON_TRANSFORM')
            WHERE JSON_VALUE(data, '$._id') = :id
            """,
            id=json_id,
        )
        conn.commit()
        cur.execute("SELECT content FROM memories WHERE id = :id", id=json_id)
        row = cur.fetchone()
        ok = row and _str(row[0]) == "updated via JSON_TRANSFORM"
        probe("json_update_to_relational", ok, {"id": json_id, "row_content": _str(row[0]) if row else None})
    except Exception as exc:
        probe("json_update_to_relational", False, {"id": json_id}, str(exc))

    # 5. UPDATE relational column reflects in JSON doc
    try:
        cur.execute(
            "UPDATE memories SET content = 'updated via SQL UPDATE' WHERE id = :id",
            id=relational_id,
        )
        conn.commit()
        cur.execute(
            "SELECT JSON_VALUE(data, '$.content') FROM memory_doc_v WHERE JSON_VALUE(data, '$._id') = :id",
            id=relational_id,
        )
        row = cur.fetchone()
        ok = row and _str(row[0]) == "updated via SQL UPDATE"
        probe("relational_update_to_json", ok, {"id": relational_id, "doc_content": _str(row[0]) if row else None})
    except Exception as exc:
        probe("relational_update_to_json", False, {"id": relational_id}, str(exc))

    # 6. DELETE via JSON view → relational row gone
    try:
        cur.execute(
            "DELETE FROM memory_doc_v WHERE JSON_VALUE(data, '$._id') = :id",
            id=json_id,
        )
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM memories WHERE id = :id", id=json_id)
        cnt = cur.fetchone()[0]
        probe("json_delete_relational_gone", cnt == 0, {"id": json_id, "count_after": cnt})
    except Exception as exc:
        probe("json_delete_relational_gone", False, {"id": json_id}, str(exc))

    # Cleanup
    try:
        cur.execute("DELETE FROM memories WHERE owner_id = 'ee-dual'")
        conn.commit()
    except Exception:
        pass

    cur.execute("SELECT BANNER FROM v$version FETCH FIRST 1 ROWS ONLY")
    db_version = cur.fetchone()[0]
    finished = datetime.now(timezone.utc).isoformat()

    evidence = {
        "schema": "mnemos-oracle-ee-duality-view/v1",
        "run_id": uuid.uuid4().hex[:12],
        "started_utc": started,
        "finished_utc": finished,
        "db_version": db_version,
        "dsn_redacted": f"oracle://{args.user}:<redacted>@{args.dsn}",
        "view_definition": "memories @insert @update @delete { _id, owner_id, namespace, content, category, subcategory, created, updated }",
        "probes": probes,
        "passed": sum(1 for p in probes if p["outcome"] == "pass"),
        "total": len(probes),
    }

    artifact = {
        "evidence": evidence,
        "hmac_key_id": sha256(HMAC_KEY).hexdigest()[:16],
        "hmac_sha256": _hmac(evidence),
    }
    out = (
        REPO_ROOT / "docs" / "proof" / f"oracle-ee-duality-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"wrote {out}")
    print(f"passed: {evidence['passed']}/{evidence['total']}")
    for p in probes:
        print(f"  {p['name']:35} {p['outcome']:6}", p.get("error") or "")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
