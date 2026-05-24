"""
SQLite backfill for MEDUSA replica.
Reads memories where embedding IS NULL, batches to HTTP embed endpoint,
writes vector back as raw bytes (SQLite mnemos stores VECTOR as BLOB).
"""

import os
import time
import array
import sqlite3
import urllib.request
import json

DB = os.environ.get("SQLITE_PATH", "/data/mnemos.db")
URL = os.environ.get("EMBED_URL", "http://192.168.207.61:8090/v1/embeddings")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
BATCH = int(os.environ.get("BATCH", "32"))
MAX_CHARS = 6000


def embed_batch(texts):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"model": MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return [list(d["data"][i]["embedding"]) for i in range(len(d["data"]))]


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM memories WHERE embedding IS NULL "
        "AND content IS NOT NULL "
        "AND deleted_at IS NULL AND archived_at IS NULL"
    )
    total = cur.fetchone()[0]
    print(f"[sqlite-backfill] {total} rows need embedding; DB={DB}", flush=True)
    if total == 0:
        return
    done = 0
    t0 = time.monotonic()
    while True:
        cur.execute(
            "SELECT id, content FROM memories WHERE embedding IS NULL "
            "AND content IS NOT NULL "
            "AND deleted_at IS NULL AND archived_at IS NULL "
            f"LIMIT {BATCH}"
        )
        rows = cur.fetchall()
        if not rows:
            break
        items = []
        for mid, content in rows:
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if content:
                items.append((mid, content[:MAX_CHARS]))
        if not items:
            break
        ids = [x[0] for x in items]
        texts = [x[1] for x in items]
        t1 = time.monotonic()
        try:
            vecs = embed_batch(texts)
        except Exception as exc:
            # Possibly oversized batch — fallback per-row
            print(f"  batch fail ({exc}); per-row retry", flush=True)
            vecs = []
            for t in texts:
                try:
                    vecs.append(embed_batch([t])[0])
                except Exception:
                    vecs.append(None)
        dt = time.monotonic() - t1
        # SQLite mnemos stores VECTOR as raw float32 BLOB
        binds = []
        for mid, v in zip(ids, vecs):
            if v is None:
                continue
            blob = sqlite3.Binary(array.array("f", v).tobytes())
            binds.append((blob, mid))
        if binds:
            cur.executemany("UPDATE memories SET embedding = ? WHERE id = ?", binds)
            conn.commit()
        done += len(binds)
        elapsed = time.monotonic() - t0
        rate = done / elapsed if elapsed > 0 else 0
        remaining = total - done
        eta = remaining / rate if rate > 0 else float("inf")
        print(
            f"  batch +{len(binds)}/{len(ids)} ({done}/{total}) embed={dt*1000:.0f}ms rate={rate:.1f}/s eta={eta:.0f}s",
            flush=True,
        )
    conn.close()
    print("[sqlite-backfill] done", flush=True)


if __name__ == "__main__":
    main()
