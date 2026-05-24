"""Re-encode memory_embeddings.embedding BLOB -> JSON text per mnemos SQLite convention."""

import sqlite3
import json
import array
import time

c = sqlite3.connect("/data/mnemos.db")
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM memory_embeddings WHERE LENGTH(embedding)=4096")
total = cur.fetchone()[0]
print(f"BLOB-encoded rows to fix: {total}")
done = 0
t0 = time.monotonic()
batch_n = 500
while True:
    cur.execute(
        "SELECT memory_id, embedding FROM memory_embeddings " "WHERE LENGTH(embedding)=4096 LIMIT ?", (batch_n,)
    )
    rows = cur.fetchall()
    if not rows:
        break
    binds = []
    for mid, blob in rows:
        arr = array.array("f")
        arr.frombytes(blob)
        binds.append((json.dumps(arr.tolist()), mid))
    cur.executemany("UPDATE memory_embeddings SET embedding=? WHERE memory_id=?", binds)
    c.commit()
    done += len(binds)
    print(f"  re-encoded {done}/{total} ({done/(time.monotonic()-t0):.0f} /s)")
print("done")
# also sync memories.embedding (used by other queries)
cur.execute("SELECT COUNT(*) FROM memories WHERE LENGTH(embedding)=4096")
print(f"memories.embedding BLOB rows to fix: {cur.fetchone()[0]}")
done = 0
t0 = time.monotonic()
while True:
    cur.execute("SELECT id, embedding FROM memories WHERE LENGTH(embedding)=4096 LIMIT ?", (batch_n,))
    rows = cur.fetchall()
    if not rows:
        break
    binds = []
    for mid, blob in rows:
        arr = array.array("f")
        arr.frombytes(blob)
        binds.append((json.dumps(arr.tolist()), mid))
    cur.executemany("UPDATE memories SET embedding=? WHERE id=?", binds)
    c.commit()
    done += len(binds)
    print(f"  re-encoded {done} ({done/(time.monotonic()-t0):.0f} /s)")
print("all done")
