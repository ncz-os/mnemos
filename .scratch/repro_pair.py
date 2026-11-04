import asyncio
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, ".scratch")
from _pytest.monkeypatch import MonkeyPatch
import pair

async def main():
    tmp = Path(tempfile.mkdtemp(dir=".scratch"))
    mp = MonkeyPatch()
    try:
        source = await pair.open_backend(tmp/"src.db")
        replica = await pair.open_backend(tmp/"rep.db")
        pair.build_source_app(source, mp)
        peer = await pair.register_peer(replica)
        await pair.sync(replica, peer["id"])

        # Rows the source holds in various shapes
        await pair.insert(source, "plain")
        await pair.insert(source, "ns2", namespace="ns2")
        await pair.insert(source, "private", permission_mode=640)
        await pair.insert(source, "vaulted", namespace="vault")
        await pair.insert(source, "frompeer", federation_source="other")
        await pair.insert(source, "archived", archived_at=pair.NOW.isoformat())
        await pair.insert(source, "consolidated", consolidated_into="plain")
        await pair.insert(source, "deleted", deleted_at=pair.NOW.isoformat())
        # unicode id
        await pair.insert(source, "ümlaut-ü")
        # id that sorts after others
        await pair.insert(source, "zzz")

        await pair.sync(replica, peer["id"])

        async with source.transactional() as tx:
            exportable = {r["id"] for r in await source.federation.fetch_source_export_keyset_page(tx, limit=1000)}
        served = await pair.feed_served_ids(source)
        rep = await pair.replica_ids(replica)

        print("exportable:", sorted(exportable))
        print("served    :", sorted(served))
        print("replica   :", sorted(rep))
        print("exportable - served:", sorted(exportable - set(served)))
        print("exportable - replica:", sorted(exportable - rep))
        print("served - exportable:", sorted(set(served) - exportable))

        # by-id route on the source
        for mid in sorted(exportable):
            async with source.transactional() as tx:
                row = await source.federation.get_feed_memory(tx, mid, namespaces=[], categories=[])
            if row is None:
                print("  BYID 404:", mid)
        await source.close()
        await replica.close()
    finally:
        mp.undo()

asyncio.run(main())
