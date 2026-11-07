"""F2 reproduction: exportable set vs pullable set on a real SQLite source."""
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
os.environ.setdefault("MNEMOS_EMBEDDING_DIM", "768")

from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

class S:
    def __init__(self):
        self.database = type("D", (), {"embedding_dim": 768})()
        self.providers = type("P", (), {"inference_embed_model": "bge-m3"})()

async def insert(be, mid, **kw):
    cols = dict(id=mid, content=kw.get("content", f"c-{mid}"), category="facts",
                owner_id=kw.get("owner_id", "alice"), namespace=kw.get("namespace", "default"),
                permission_mode=kw.get("permission_mode", 644),
                created=NOW.isoformat(), updated=NOW.isoformat(),
                federation_source=kw.get("federation_source"),
                archived_at=kw.get("archived_at"), consolidated_into=kw.get("consolidated_into"),
                deleted_at=kw.get("deleted_at"))
    async with be.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id,content,category,owner_id,namespace,permission_mode,created,updated,"
            "federation_source,archived_at,consolidated_into,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            *[cols[k] for k in ("id","content","category","owner_id","namespace","permission_mode","created","updated","federation_source","archived_at","consolidated_into","deleted_at")])

async def rows(be, sql, *p):
    async with be.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchall(sql, *p)

async def feed_served(be, limit=100):
    served = {}
    seq = 0
    while True:
        async with be.transactional() as tx:
            r = await be.federation.feed_query(tx, since_updated=None, since_id=f"journal:{seq}",
                                               namespaces=[], categories=[], limit=limit, prefer_compressed=False)
        if not r:
            break
        for row in r:
            served.setdefault(str(row["id"]), row.get("type"))
        last = r[-1]
        cursor = last.get("cursor_id")
        nxt = int(cursor[len("journal:"):]) if cursor and cursor.startswith("journal:") else seq
        if len(r) < limit:
            break
        if nxt <= seq:
            break
        seq = nxt
    return served

async def main():
    d = Path(".scratch/repro_src.db")
    if d.exists():
        d.unlink()
    be = SqliteBackend(d, S())
    await be.open()
    # a spread of row shapes
    await insert(be, "plain")
    await insert(be, "other-ns", namespace="other")
    await insert(be, "vaulted", namespace="vault")
    await insert(be, "from-peer", federation_source="other")
    await insert(be, "archived", archived_at=NOW.isoformat())
    await insert(be, "consolidated", consolidated_into="plain")
    await insert(be, "deleted", deleted_at=NOW.isoformat())
    await insert(be, "private", permission_mode=640)

    async with be.transactional() as tx:
        exportable = [r["id"] for r in await be.federation.fetch_source_export_keyset_page(tx, limit=1000)]
    served = await feed_served(be)
    byid = {}
    for mid in exportable:
        async with be.transactional() as tx:
            row = await be.federation.get_feed_memory(tx, mid, namespaces=[], categories=[])
        byid[mid] = row is not None
    print("exportable:", sorted(exportable))
    print("served    :", sorted(served))
    print("byid 404  :", sorted(m for m,v in byid.items() if not v))
    print("exportable-not-served:", sorted(set(exportable)-set(served)))
    print("served-not-exportable:", sorted(set(served)-set(exportable)))
    # journal rows
    j = await rows(be, "SELECT memory_id, seq, old_exportable, new_exportable FROM federation_changes ORDER BY event_id")
    for r in j:
        print("  journal:", dict(r))
    await be.close()

asyncio.run(main())
