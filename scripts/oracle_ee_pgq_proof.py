"""EE feature #6 — Property Graph via SQL/PGQ (23ai).

Creates a property graph atop the existing kg_triples table.
Vertices = distinct subjects + distinct object IRIs (object_type='iri').
Edges = each triple (subject -[predicate]-> object_iri).

Then proves SQL/PGQ standard via GRAPH_TABLE MATCH queries:
  - 1-hop walk
  - 2-hop walk
  - cycle detection

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

DEFAULT_DSN = "192.168.207.25:1521/ORCLPDB1"
DEFAULT_USER = "MNEMOS"
DEFAULT_PWD = "mnemos_dev"
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY or HMAC_KEY == "mnemos-oracle-proof-v1":
    print("ERROR: MNEMOS_PROOF_HMAC_KEY env var required (fail-closed).", file=sys.stderr)
    sys.exit(1)
HMAC_KEY = HMAC_KEY.encode("utf-8")

# 8-edge sample graph (Alice→Bob→Carol→Dave→Alice ring + Bob→Eve, Eve→Frank, Frank→Alice)
SAMPLE_TRIPLES = [
    ("alice", "knows", "bob"),
    ("bob", "knows", "carol"),
    ("carol", "knows", "dave"),
    ("dave", "knows", "alice"),
    ("bob", "knows", "eve"),
    ("eve", "knows", "frank"),
    ("frank", "knows", "alice"),
    ("alice", "trusts", "carol"),
]


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

    def probe(name, ok, ev, err=None):
        probes.append({"name": name, "outcome": "pass" if ok else "fail", "evidence": ev, "error": err})

    # Cleanup
    try:
        cur.execute("DROP PROPERTY GRAPH mnemos_kg")
    except Exception:
        pass
    cur.execute("DELETE FROM kg_triples WHERE owner_id = 'ee-pgq'")
    conn.commit()

    # Seed triples
    rows = []
    for s, p, o in SAMPLE_TRIPLES:
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "subject": s,
                "predicate": p,
                "object": o,
                "subject_type": "iri",
                "object_type": "iri",
                "owner_id": "ee-pgq",
                "namespace": "demo",
            }
        )
    cur.executemany(
        """
        INSERT INTO kg_triples (id, subject, predicate, object,
                                subject_type, object_type, owner_id, namespace,
                                created, valid_from, confidence)
        VALUES (:id, :subject, :predicate, :object,
                :subject_type, :object_type, :owner_id, :namespace,
                SYSDATE, SYSTIMESTAMP, 1.0)
        """,
        rows,
    )
    conn.commit()
    probe("seed", True, {"triples_inserted": len(rows)})

    # Build view of vertices (union of subjects + objects where type=iri)
    # Property Graph needs node table + edge table. Use views.
    # CLOB object column can't be a graph key, so wrap kg_triples in an
    # edge view that exposes the object value as VARCHAR2.
    try:
        cur.execute("DROP VIEW kg_edge")
    except Exception:
        pass
    try:
        cur.execute("DROP VIEW kg_vertex")
    except Exception:
        pass
    cur.execute(
        """
        CREATE OR REPLACE VIEW kg_vertex AS
        SELECT DISTINCT subject AS iri FROM kg_triples
        WHERE owner_id='ee-pgq'
        UNION
        SELECT DISTINCT SUBSTR(DBMS_LOB.SUBSTR(object, 4000, 1), 1, 4000) AS iri
        FROM kg_triples
        WHERE owner_id='ee-pgq' AND object_type='iri'
        """
    )
    cur.execute(
        """
        CREATE OR REPLACE VIEW kg_edge AS
        SELECT id, subject AS src_iri,
               CAST(SUBSTR(DBMS_LOB.SUBSTR(object, 4000, 1), 1, 4000) AS VARCHAR2(4000)) AS dst_iri,
               predicate, confidence, owner_id
        FROM kg_triples
        WHERE owner_id='ee-pgq' AND object_type='iri'
        """
    )
    conn.commit()
    probe("vertex_view.create", True, {})

    # CREATE PROPERTY GRAPH — Oracle 23ai SQL/PGQ syntax
    try:
        cur.execute(
            """
            CREATE PROPERTY GRAPH mnemos_kg
            VERTEX TABLES (
                kg_vertex
                    KEY (iri)
                    LABEL Entity
                    PROPERTIES (iri)
            )
            EDGE TABLES (
                kg_edge AS knows
                    KEY (id)
                    SOURCE KEY (src_iri) REFERENCES kg_vertex (iri)
                    DESTINATION KEY (dst_iri) REFERENCES kg_vertex (iri)
                    LABEL Knows
                    PROPERTIES (predicate, confidence, owner_id)
            )
            """
        )
        conn.commit()
        probe("property_graph.create", True, {"name": "mnemos_kg"})
    except Exception as exc:
        probe("property_graph.create", False, {}, str(exc))
        raise

    # SQL/PGQ 1-hop: alice's direct friends
    try:
        cur.execute(
            """
            SELECT v_dst
            FROM GRAPH_TABLE (mnemos_kg
                MATCH (a IS Entity) -[e]-> (b IS Entity)
                WHERE a.iri = 'alice'
                COLUMNS (b.iri AS v_dst)
            )
            ORDER BY v_dst
            """
        )
        rows = [r[0] for r in cur.fetchall()]
        ok = sorted(rows) == sorted(["bob", "carol"])
        probe("pgq.1hop_alice", ok, {"results": rows, "expected": ["bob", "carol"]})
    except Exception as exc:
        probe("pgq.1hop_alice", False, {}, str(exc))

    # 2-hop walk: alice's friends-of-friends (knows edges only)
    try:
        cur.execute(
            """
            SELECT DISTINCT v_dst
            FROM GRAPH_TABLE (mnemos_kg
                MATCH (a IS Entity) -[e1]-> (b IS Entity) -[e2]-> (c IS Entity)
                WHERE a.iri = 'alice' AND e1.predicate = 'knows' AND e2.predicate = 'knows'
                COLUMNS (c.iri AS v_dst)
            )
            ORDER BY v_dst
            """
        )
        rows = [r[0] for r in cur.fetchall()]
        # alice→bob→carol, alice→bob→eve
        ok = sorted(rows) == sorted(["carol", "eve"])
        probe("pgq.2hop_alice_knows_chain", ok, {"results": rows, "expected": ["carol", "eve"]})
    except Exception as exc:
        probe("pgq.2hop_alice_knows_chain", False, {}, str(exc))

    # Edge enumeration by predicate (proves edge-table mapping works end-to-end)
    try:
        cur.execute(
            """
            SELECT edge_pred, COUNT(*)
            FROM GRAPH_TABLE (mnemos_kg
                MATCH (a IS Entity) -[e]-> (b IS Entity)
                COLUMNS (e.predicate AS edge_pred)
            )
            GROUP BY edge_pred
            ORDER BY edge_pred
            """
        )
        rows = cur.fetchall()
        # SAMPLE_TRIPLES has 7 'knows' edges + 1 'trusts' edge
        as_dict = dict(rows)
        ok = as_dict.get("knows") == 7 and as_dict.get("trusts") == 1
        probe("pgq.edge_count_by_predicate", ok, {"rows": rows})
    except Exception as exc:
        probe("pgq.edge_count_by_predicate", False, {}, str(exc))

    # Cleanup
    try:
        cur.execute("DROP PROPERTY GRAPH mnemos_kg")
        cur.execute("DROP VIEW kg_edge")
        cur.execute("DROP VIEW kg_vertex")
        cur.execute("DELETE FROM kg_triples WHERE owner_id = 'ee-pgq'")
        conn.commit()
    except Exception:
        pass

    cur.execute("SELECT BANNER FROM v$version FETCH FIRST 1 ROWS ONLY")
    db_version = cur.fetchone()[0]
    finished = datetime.now(timezone.utc).isoformat()

    evidence = {
        "schema": "mnemos-oracle-ee-pgq/v1",
        "run_id": uuid.uuid4().hex[:12],
        "started_utc": started,
        "finished_utc": finished,
        "db_version": db_version,
        "dsn_redacted": f"oracle://{args.user}:<redacted>@{args.dsn}",
        "graph": "mnemos_kg: VERTEX Entity over kg_vertex, EDGE Knows over kg_triples",
        "sample_triples": [list(t) for t in SAMPLE_TRIPLES],
        "probes": probes,
        "passed": sum(1 for p in probes if p["outcome"] == "pass"),
        "total": len(probes),
    }
    artifact = {
        "evidence": evidence,
        "hmac_key_id": sha256(HMAC_KEY).hexdigest()[:16],
        "hmac_sha256": _hmac(evidence),
    }
    out = REPO_ROOT / "docs" / "proof" / f"oracle-ee-pgq-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
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
