"""
Backfill memories.embedding via MEDUSA HTTP embed endpoint.
Runs inside mnemos-api container (has python-oracledb + httpx).

Pulls rows where embedding IS NULL, in batches of BATCH_SIZE,
posts batched content to MEDUSA :8090, UPSERTs the vector column.

Idempotent: re-running picks up where it left off.
"""

import asyncio
import os
import sys
import time
import array
import httpx

# Oracle DSN from container env
DSN = os.environ.get("MNEMOS_DATABASE_DSN", "oracle://mnemos:mnemos_dev@127.0.0.1:1521/ORCLPDB1")
# oracle://user:pass@host:port/svc
import re

m = re.match(r"oracle://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)", DSN)
user, pwd, host, port, svc = m.groups()

EMBED_URL = os.environ.get("MNEMOS_EMBED_HTTP_URL", "http://192.168.207.64:8090/v1/embeddings")
EMBED_MODEL = os.environ.get("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")
BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "32"))
MAX_TEXT_CHARS = 6000  # ~1500 tokens safe under bge-m3 n_ctx=8192 with margin


async def fetch_batch(conn, batch_size):
    """Fetch next batch of rows missing embeddings."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, content FROM memories "
        "WHERE embedding IS NULL "
        "AND content IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND archived_at IS NULL "
        f"FETCH FIRST {int(batch_size)} ROWS ONLY"
    )
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        mid, content = r
        # content may be LOB; read
        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if content:
            out.append((mid, content[:MAX_TEXT_CHARS]))
    return out


async def embed_batch(client, texts):
    """POST batch to MEDUSA; returns list of embeddings."""
    t0 = time.monotonic()
    r = await client.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60.0,
    )
    dt = time.monotonic() - t0
    if r.status_code != 200:
        print(f"  EMBED FAIL status={r.status_code} body={r.text[:200]}", flush=True)
        return None, dt
    data = r.json()
    vecs = [d["embedding"] for d in data["data"]]
    return vecs, dt


def update_embeddings(conn, items_with_vecs):
    """Bulk UPDATE memories SET embedding=? WHERE id=?."""
    cur = conn.cursor()
    # Oracle VECTOR bind expects array.array('f',...) for FLOAT32
    binds = []
    for mid, vec in items_with_vecs:
        arr = array.array("f", vec)
        binds.append({"emb": arr, "mid": mid})
    cur.executemany(
        "UPDATE memories SET embedding = :emb WHERE id = :mid",
        binds,
    )
    conn.commit()
    cur.close()


async def main():
    import oracledb

    print(f"[backfill] DSN host={host}:{port} svc={svc} user={user}", flush=True)
    print(f"[backfill] EMBED_URL={EMBED_URL} MODEL={EMBED_MODEL} BATCH={BATCH_SIZE}", flush=True)
    conn = oracledb.connect(user=user, password=pwd, dsn=f"{host}:{port}/{svc}")
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM memories WHERE embedding IS NULL "
        "AND content IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL"
    )
    total_remaining = cur.fetchone()[0]
    cur.close()
    print(f"[backfill] {total_remaining} rows need embedding", flush=True)

    if total_remaining == 0:
        print("[backfill] nothing to do", flush=True)
        return

    async with httpx.AsyncClient() as client:
        done = 0
        t_start = time.monotonic()
        while True:
            batch = await fetch_batch(conn, BATCH_SIZE)
            if not batch:
                break
            ids_texts = batch
            ids = [x[0] for x in ids_texts]
            texts = [x[1] for x in ids_texts]
            vecs, dt = await embed_batch(client, texts)
            if vecs is None:
                print(f"  skip batch (embed fail), batch_ids[0]={ids[0]}", flush=True)
                # Without persistence the same rows come back; bail to avoid loop
                print("[backfill] ABORT — embed endpoint failing", flush=True)
                sys.exit(1)
            if len(vecs) != len(ids):
                print(f"  WARN length mismatch vecs={len(vecs)} ids={len(ids)}; using min", flush=True)
            n = min(len(vecs), len(ids))
            items_with_vecs = [(ids[i], vecs[i]) for i in range(n)]
            update_embeddings(conn, items_with_vecs)
            done += n
            elapsed = time.monotonic() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            remaining = total_remaining - done
            eta = remaining / rate if rate > 0 else float("inf")
            print(
                f"  batch ok: +{n} rows ({done}/{total_remaining}) "
                f"embed={dt*1000:.0f}ms rate={rate:.1f} rows/s eta={eta:.0f}s",
                flush=True,
            )
    conn.close()
    print("[backfill] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
