"""
Migrate embeddings from memories.embedding -> memory_embeddings join table.
"""

import sqlite3
import time

c = sqlite3.connect("/data/mnemos.db")
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
total = cur.fetchone()[0]
print(f"source rows: {total}")
done = 0
t0 = time.monotonic()
while True:
    cur.execute(
        "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL "
        "AND id NOT IN (SELECT memory_id FROM memory_embeddings) LIMIT 500"
    )
    rows = cur.fetchall()
    if not rows:
        break
    binds = [(mid, emb, time.time()) for mid, emb in rows]
    cur.executemany(
        "INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, updated_at) VALUES (?,?,?)",
        binds,
    )
    c.commit()
    done += len(binds)
    print(f"  migrated {done}/{total} ({done/(time.monotonic()-t0):.0f} rows/s)")
cur.execute("SELECT COUNT(*) FROM memory_embeddings")
print(f"memory_embeddings final: {cur.fetchone()[0]}")
