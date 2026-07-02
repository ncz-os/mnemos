# Migrating memory in & out: MIF + IBM Docling

Two migration paths ship with this quickstart:

1. **MIF** — move *memory* portably between systems (backup, or import another mnemos /
   memory system's export).
2. **IBM Docling** — turn *documents* (PDF/DOCX/HTML/PPTX…) into memory the agent can recall.

---

## 1. MIF — portable memory (import / export)

**MIF** (Modeled Information Format, <https://mif-spec.dev>) is a **vendor-neutral data model
for portable AI memory** — dual Markdown (human) + JSON-LD (machine) representations, static
JSON Schemas with stable `$id`. mnemos treats MIF as its **native** interchange format, so
memory isn't trapped in Db2: you can export it, move it, diff it, or re-import it into any
MIF-conformant system.

**Export** (Db2 → MIF bundle) — backup, or hand off to another instance:
```bash
docker exec mnemos mnemos mif export --out /data/mnemos.mif.jsonld
docker cp mnemos:/data/mnemos.mif.jsonld ./mnemos.mif.jsonld
```

**Import** (MIF → Db2) — seed this instance from another system's export:
```bash
docker cp ./incoming.mif.jsonld mnemos:/data/incoming.mif.jsonld
docker exec mnemos mnemos mif import --in /data/incoming.mif.jsonld
```

Import is **idempotent** (dedup by content/id) and **re-embeds** on ingest, so imported
memories land with Db2 `VECTOR` embeddings and are immediately semantically searchable.
Because MIF is the on-disk contract, the same bundle round-trips loss-lessly across mnemos
backends (Db2 ↔ Postgres ↔ Oracle ↔ SQLite).

---

## 2. IBM Docling — documents → memory

[Docling](https://github.com/docling-project/docling) (IBM Research, open source) converts
PDF/DOCX/PPTX/HTML into clean structured Markdown with layout + table fidelity. Pipe that into
mnemos and your agent can semantically recall your document corpus.

`mif-tools/docling_ingest.py` (included) does exactly this: **document → Docling → chunks →
mnemos** (Db2-backed, embedded, searchable).

```bash
pip install docling requests
# ingest a file or a whole directory of docs:
python mif-tools/docling_ingest.py ./whitepapers/ \
    --mnemos-url http://localhost:5002 \
    --category reference --source-tag whitepapers
```

What it does:
- Docling converts each document → Markdown (headings, tables, lists preserved).
- Splits on headings into recall-sized chunks (keeps section context in each).
- `POST`s chunks to mnemos `/v1/memories/bulk` → embedded on CPU → stored in Db2.
- Then any agent (see [`AGENTS.md`](AGENTS.md)) can `search_memory` across the corpus.

For a *portable* pipeline, emit MIF instead of posting directly (`--emit-mif out.mif.jsonld`)
and `mnemos mif import` it — same result, but the intermediate is a vendor-neutral bundle you
can inspect, store, or share.

> Docling handles the *document→structure* problem; MIF handles the *structure→portable-memory*
> contract; Db2 CE handles *durable vector storage + recall*. Each layer is swappable and open.
