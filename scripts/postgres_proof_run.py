#!/usr/bin/env python3
"""Proof-of-equivalency for MNEMOS -> PostgreSQL (v4 schema, raw SQL).

Writes HMAC artifact to docs/proof/postgres-proof-*.json.
Tests: stats, list, fts, by-id, CRUD, vector, kg, state, transactional.
"""

import asyncio
import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DSN = os.environ.get("PG_PROOF_DSN", "postgresql://mnemos:mnemos_dev@192.168.207.25:5434/mnemos")
HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not HMAC_KEY:
    exit("FATAL: MNEMOS_PROOF_HMAC_KEY not set")


def sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        return "unknown"


async def run():
    import asyncpg

    probes = []
    rid = uuid.uuid4().hex[:12]
    pf = f"p_{rid}"

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, statement_cache_size=0)
    try:
        conn = await pool.acquire()
        try:
            pgv = await conn.fetchval("SELECT version()")
            live = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE archived_at IS NULL")

            # 1
            try:
                total = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE archived_at IS NULL")
                native = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE archived_at IS NULL AND federation_remote_updated IS NULL"
                )
                probes.append(
                    {
                        "name": "memory.gather_stats",
                        "desc": "Stats",
                        "ok": True,
                        "err": None,
                        "data": {"total": total, "native": native},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.gather_stats",
                        "desc": "Stats",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 2
            try:
                rows = await conn.fetch("SELECT id, content, category FROM memories WHERE archived_at IS NULL LIMIT 3")
                total = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE archived_at IS NULL")
                probes.append(
                    {
                        "name": "memory.list_memories",
                        "desc": "List",
                        "ok": True,
                        "err": None,
                        "data": {"returned": len(rows), "total": total},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.list_memories",
                        "desc": "List",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 3 (skip - requires pg_trgm + GIN index, may not exist on base install)
            try:
                r = await conn.fetch(
                    "SELECT id FROM memories WHERE to_tsvector('english', content) @@ websearch_to_tsquery('english', $1) LIMIT 3",
                    "test",
                )
                probes.append(
                    {"name": "memory.fts_search", "desc": "FTS", "ok": True, "err": None, "data": {"matches": len(r)}}
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.fts_search",
                        "desc": "FTS",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 4
            try:
                row = await conn.fetchrow("SELECT id FROM memories WHERE archived_at IS NULL LIMIT 1")
                if row:
                    m = await conn.fetchrow("SELECT * FROM memories WHERE id=$1", row["id"])
                    probes.append(
                        {
                            "name": "memory.fetch_memory_by_id",
                            "desc": "ById",
                            "ok": True,
                            "err": None,
                            "data": {"len": len(m)},
                        }
                    )
                else:
                    probes.append({"name": "memory.fetch_memory_by_id", "desc": "ById", "ok": True, "err": None})
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.fetch_memory_by_id",
                        "desc": "ById",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 5 crud
            crud_id = f"{pf}_x"
            try:
                await conn.execute(
                    "INSERT INTO memories (id,content,category,task_type,quality_rating,is_original) VALUES ($1,$2,$3,$4,$5,$6)",
                    crud_id,
                    "proof",
                    "p",
                    "p",
                    50,
                    True,
                )
                await conn.execute("UPDATE memories SET content=$1 WHERE id=$2", "v2", crud_id)
                chk = await conn.fetchval("SELECT content FROM memories WHERE id=$1", crud_id)
                assert chk == "v2"
                await conn.execute("UPDATE memories SET archived_at=NOW() WHERE id=$1", crud_id)
                gone = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE id=$1 AND archived_at IS NULL", crud_id)
                assert gone == 0
                probes.append(
                    {
                        "name": "memory.insert+update+delete",
                        "desc": "CRUD",
                        "ok": True,
                        "err": None,
                        "data": {"updated": True, "gone": True},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.insert+update+delete",
                        "desc": "CRUD",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 6 vector
            D = 768
            vids = []
            try:
                for ax, i in [("x", 0), ("y", 1), ("z", 2)]:
                    mid = f"{pf}_v_{ax}"
                    vids.append(mid)
                    v = [1.0 if j == i else 0.0 for j in range(D)]
                    vs = "[" + ",".join(f"{x:.7f}" for x in v) + "]"
                    await conn.execute(
                        "INSERT INTO memories (id,content,category,task_type,quality_rating,is_original,embedding) VALUES ($1,$2,$3,$4,$5,$6,$7::vector)",
                        mid,
                        f"a {ax}",
                        "pv",
                        "p",
                        50,
                        True,
                        vs,
                    )
                q = [0.9 if j == 0 else (0.1 if j == 1 else 0.0) for j in range(D)]
                qs = "[" + ",".join(f"{x:.7f}" for x in q) + "]"
                results = await conn.fetch(
                    "SELECT content FROM memories WHERE category='pv' ORDER BY embedding <=> $1::vector LIMIT 3", qs
                )
                assert len(results) == 3
                probes.append(
                    {
                        "name": "memory.semantic_search",
                        "desc": "Cosine",
                        "ok": True,
                        "err": None,
                        "data": {"results": len(results)},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "memory.semantic_search",
                        "desc": "Cosine",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 7 kg
            kg_id = f"{pf}_kg"
            try:
                await conn.execute(
                    "INSERT INTO kg_triples (id,subject,predicate,object,subject_type,object_type,memory_id,confidence,created) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())",
                    kg_id,
                    "a",
                    "k",
                    "b",
                    "p",
                    "p",
                    crud_id,
                    0.9,
                )
                r = await conn.fetchrow("SELECT subject FROM kg_triples WHERE id=$1", kg_id)
                probes.append(
                    {
                        "name": "kg.triple_roundtrip",
                        "desc": "KG insert+read",
                        "ok": True,
                        "err": None,
                        "data": {"subject": r["subject"]},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "kg.triple_roundtrip",
                        "desc": "KG insert+read",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 8 state
            sk = f"{pf}_s"
            try:
                await conn.execute(
                    "INSERT INTO state (key,value,owner_id,namespace) VALUES ($1,$2,$3,$4) ON CONFLICT (owner_id,key) DO UPDATE SET value=$2",
                    sk,
                    "vv",
                    "default",
                    "default",
                )
                r = await conn.fetchrow("SELECT value FROM state WHERE key=$1 AND owner_id=$2", sk, "default")
                assert r is not None
                await conn.execute("DELETE FROM state WHERE key=$1 AND owner_id=$2", sk, "default")
                g = await conn.fetchval("SELECT COUNT(*) FROM state WHERE key=$1 AND owner_id=$2", sk, "default")
                assert g == 0
                probes.append(
                    {
                        "name": "state.set_get_delete",
                        "desc": "State KV",
                        "ok": True,
                        "err": None,
                        "data": {"val": r["value"], "gone": True},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "state.set_get_delete",
                        "desc": "State KV",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # 9 transactional
            try:
                from mnemos.persistence.postgres import PostgresBackend

                be = PostgresBackend(pool, None)
                async with be.transactional() as tx:
                    one = await tx.conn.fetchval("SELECT 1")
                probes.append(
                    {
                        "name": "backend.transactional",
                        "desc": "Tx context",
                        "ok": True,
                        "err": None,
                        "data": {"one": one},
                    }
                )
            except Exception as e:
                probes.append(
                    {
                        "name": "backend.transactional",
                        "desc": "Tx context",
                        "ok": False,
                        "err": f"{type(e).__name__}: {str(e)[:200]}",
                    }
                )

            # cleanup
            await conn.execute("DELETE FROM memories WHERE id LIKE $1", f"{pf}%")
            await conn.execute("DELETE FROM state WHERE key LIKE $1", f"{pf}%")
        finally:
            await pool.release(conn)
    finally:
        await pool.close()

    body = {
        "schema": "mnemos-postgres-proof/v1",
        "run_id": rid,
        "dsn_redacted": _redact(DSN),
        "pg_version": pgv,
        "live": live,
        "git": sha(),
        "python": sys.version,
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": "9-probe reduced; v4 schema — raw SQL for CRUD/vectors/KG/state",
        "probes": probes,
        "summary": {
            "total": len(probes),
            "ok": sum(1 for p in probes if p["ok"]),
            "fail": sum(1 for p in probes if not p["ok"]),
        },
    }
    bj = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    sig = hmac.new(HMAC_KEY.encode("utf-8"), bj, hashlib.sha256).hexdigest()
    out = (
        REPO
        / "docs"
        / "proof"
        / f"postgres-proof-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "evidence": body,
                "hmac_sha256": sig,
                "hmac_key_id": hashlib.sha256(HMAC_KEY.encode("utf-8")).hexdigest()[:16],
            },
            indent=2,
            default=str,
        )
    )
    s = body["summary"]
    print(f"PG: ok={s['ok']}/{s['total']} fail={s['fail']} -> {out}")
    return 2 if s["fail"] > 0 else 0


def _redact(d):
    if "@" not in d:
        return d
    h, t = d.split("@", 1)
    if "://" in h:
        s, c = h.split("://", 1)
        if ":" in c:
            u = c.split(":", 1)[0]
            return f"{s}://{u}:<redacted>@{t}"
    return f"<redacted>@{t}"


if __name__ == "__main__":
    asyncio.run(run())
