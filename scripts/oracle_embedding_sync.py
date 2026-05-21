"""Copy 768-d embeddings from PYTHIA Postgres → PROTEUS Oracle.

For the perf harness to make a fair semantic_search comparison both
backends need populated embeddings. This script walks the PG memories
that have a non-null embedding, finds the matching Oracle row by id,
and writes the vector via ``TO_VECTOR(:lit)``.

Run::

    .venv/bin/python scripts/oracle_embedding_sync.py

Adds indexes on the Oracle side to even up the list_page benchmark:

- ``CREATE INDEX idx_memories_created_desc ON memories (created DESC)``
- ``CREATE INDEX idx_memories_owner_ns_created
       ON memories (owner_id, namespace, created DESC)``
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ORACLE_DSN = os.environ.get("ORACLE_BENCH_DSN", "oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1")
PG_HOST = os.environ.get("PG_BENCH_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PG_BENCH_PORT", "5433"))
PG_DB = os.environ.get("PG_BENCH_DB", "mnemos")
PG_USER = os.environ.get("PG_BENCH_USER", "mnemos_user")
PG_PASSWORD = os.environ.get("PG_BENCH_PASSWORD", "mnemos_secure_password")


async def main() -> int:
    import asyncpg

    from mnemos.persistence.oracle import create_oracle_pool

    print(f"[sync] connecting PG {PG_HOST}:{PG_PORT}…")
    pg = await asyncpg.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASSWORD)

    print(f"[sync] connecting Oracle {ORACLE_DSN.split('@')[1]}…")
    pool = await create_oracle_pool(ORACLE_DSN, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()
            # Indexes first — cheap to attempt, ORA-00955 if already exist.
            for ddl in (
                "CREATE INDEX idx_memories_created_desc " "ON memories (created DESC)",
                "CREATE INDEX idx_memories_owner_ns_created " "ON memories (owner_id, namespace, created DESC)",
            ):
                try:
                    await cur.execute(ddl)
                    print(f"  [+] {ddl.splitlines()[0]} …")
                except Exception as e:
                    if "ORA-00955" in str(e):
                        print(f"  [=] index already exists ({ddl.split('(')[0].strip()})")
                    else:
                        print(f"  [!] index DDL failed: {e}")

            # Fetch ids present on Oracle so we only attempt updates for
            # memory ids that actually exist on the Oracle side. The
            # PROTEUS baseline is the CHARON snapshot from earlier in
            # this work, so we expect ~8157 ids.
            await cur.execute("SELECT id FROM memories WHERE deleted_at IS NULL")
            oracle_ids = {row[0] for row in await cur.fetchall()}
            cur.close()
            print(f"[sync] Oracle live memory ids: {len(oracle_ids)}")

            print("[sync] streaming PG embeddings…")
            rows = await pg.fetch("SELECT id, embedding::text AS emb " "FROM memories WHERE embedding IS NOT NULL")
            print(f"[sync] PG rows with embedding: {len(rows)}")

            updated = 0
            skipped = 0
            t0 = time.perf_counter()
            cur = conn.cursor()
            for r in rows:
                mid = r["id"]
                if mid not in oracle_ids:
                    skipped += 1
                    continue
                await cur.execute(
                    "UPDATE memories SET embedding = TO_VECTOR(:v) WHERE id = :id",
                    {"v": r["emb"], "id": mid},
                )
                updated += 1
                if updated % 500 == 0:
                    await conn.commit()
                    print(f"  [{updated:5d}/{len(rows)}] " f"elapsed={time.perf_counter() - t0:.1f}s")
            await conn.commit()
            cur.close()
            elapsed = time.perf_counter() - t0
            print(
                f"[sync] DONE updated={updated} skipped_missing_in_oracle={skipped} "
                f"elapsed={elapsed:.1f}s ({updated / elapsed:.0f} rows/s)"
            )
    finally:
        await pool.close()
        await pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
