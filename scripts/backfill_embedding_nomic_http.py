"""
Nomic backfill via HTTP — points at TYPHON :8091 (CUDA nomic-embed-text).
Writes embedding_nomic VECTOR(768) column.
"""

import asyncio
import os
import time
import array
import re
import httpx

DSN = os.environ.get("MNEMOS_DATABASE_DSN", "oracle://mnemos:mnemos_dev@127.0.0.1:1521/ORCLPDB1")
m = re.match(r"oracle://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)", DSN)
user, pwd, host, port, svc = m.groups()

URL = os.environ.get("NOMIC_HTTP_URL", "http://192.168.207.61:8091/v1/embeddings")
MODEL = os.environ.get("NOMIC_HTTP_MODEL", "nomic-embed-text")
BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "16"))
MAX_TEXT_CHARS = 5000  # ~1250 tok safe under nomic n_ctx=2048


async def main():
    import oracledb

    print(f"[nomic-backfill] URL={URL} MODEL={MODEL} BATCH={BATCH_SIZE}", flush=True)
    conn = oracledb.connect(user=user, password=pwd, dsn=f"{host}:{port}/{svc}")
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM memories WHERE embedding_nomic IS NULL "
        "AND content IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL"
    )
    total = cur.fetchone()[0]
    cur.close()
    print(f"[nomic-backfill] {total} rows need nomic embedding", flush=True)
    if total == 0:
        return
    async with httpx.AsyncClient(timeout=60.0) as client:
        done = 0
        t_start = time.monotonic()
        while True:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, content FROM memories "
                "WHERE embedding_nomic IS NULL AND content IS NOT NULL "
                "AND deleted_at IS NULL AND archived_at IS NULL "
                f"FETCH FIRST {int(BATCH_SIZE)} ROWS ONLY"
            )
            rows = cur.fetchall()
            cur.close()
            if not rows:
                break
            items = []
            for mid, content in rows:
                if hasattr(content, "read"):
                    content = content.read()
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                if content:
                    items.append((mid, content[:MAX_TEXT_CHARS]))
            if not items:
                break
            t0 = time.monotonic()
            # send each row independently to avoid one bad row blocking batch
            vecs_pairs = []
            skipped = []
            for mid, text in items:
                rr = await client.post(URL, json={"model": MODEL, "input": text})
                if rr.status_code != 200:
                    # likely token-count exceed; mark with zero vec to skip retries
                    print(f"  skip {mid} status={rr.status_code} ({len(text)} chars)", flush=True)
                    skipped.append(mid)
                    continue
                d = rr.json()
                vec = d["data"][0]["embedding"]
                vecs_pairs.append((mid, vec))
            dt = time.monotonic() - t0
            cur = conn.cursor()
            if vecs_pairs:
                binds = [{"emb": array.array("f", v), "mid": mid} for mid, v in vecs_pairs]
                cur.executemany("UPDATE memories SET embedding_nomic = :emb WHERE id = :mid", binds)
            # mark skipped rows with empty vector so they won't loop back
            if skipped:
                # use a single-element zero vec sentinel (won't match anything)
                zero_vec = array.array("f", [0.0] * 768)
                cur.executemany(
                    "UPDATE memories SET embedding_nomic = :emb WHERE id = :mid",
                    [{"emb": zero_vec, "mid": mid} for mid in skipped],
                )
            conn.commit()
            cur.close()
            done += len(vecs_pairs) + len(skipped)
            elapsed = time.monotonic() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            remaining = total - done
            eta = remaining / rate if rate > 0 else float("inf")
            print(
                f"  batch +{len(binds)} ({done}/{total}) embed={dt*1000:.0f}ms rate={rate:.1f} rows/s eta={eta:.0f}s",
                flush=True,
            )
    conn.close()
    print("[nomic-backfill] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
