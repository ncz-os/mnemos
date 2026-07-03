# IBM Db2 Community forum — announcement post (draft)

> Ready to paste into the Db2 Community forum. Written for that audience: Db2-forward,
> no vendor comparisons. Edit freely.

---

**Title:** Try Db2 12.1.5's vector search with your AI agents — a free, open-source "mnemos" Quick Start (memory over MCP)

Hi all —

I've published an open-source **Quick Start** that turns **Db2 Community Edition** into persistent, semantically-searchable **memory for AI agents** — and I wanted to share it here because it's built to show off Db2 12.1.5's vector capabilities with essentially zero setup.

### What it is

`mnemos` is an open-source agent-memory server. You store notes and facts as you work; your AI coding assistant reads and writes them back over **MCP** (Model Context Protocol), so context survives across sessions. The Quick Start runs the whole thing on your laptop with two commands — **CPU-only, no GPU, no cloud, no proprietary vector store.**

### What it demonstrates about Db2 12.1.5

Every memory is embedded to a **1024-dimension vector** (a CPU `bge-m3` model ships in the compose) and stored in Db2's **native `VECTOR` data type**. Semantic recall is served by **`VECTOR_DISTANCE`**, with Db2 12.1.5's **vector indexing** enabled via the `DB2_VECTOR_INDEXING` registry variable (a one-line init script turns on the datatype + indexing for you). Ask *"which marsupial demonstrates vector search?"* and you get back the note about a quokka — no shared keywords, pure vector similarity. It's a concrete, hands-on way to see the new vector features working end to end.

**This is a fully Db2-native adapter, not Oracle-compatibility mode.** mnemos emits native Db2 SQL throughout — native `VECTOR`/`VECTOR_DISTANCE`, `MERGE … USING SYSIBM.SYSDUMMY1`, `SYSTOOLS.JSON2BSON` JSON validation, `?` positional binds, `GENERATE_UNIQUE()` keys — with **no Oracle-dialect translation layer** (backend class `Db2BackendNative`). The only registry setting the Quick Start touches, `DB2_COMPATIBILITY_VECTOR`, is Db2's own server-side switch for the 12.1.5 `VECTOR` datatype — a Db2 feature toggle, not an application-side Oracle shim.

And it's on **Db2 Community Edition** — free, permanent license, generous envelope (4 cores / 8 GB / **unlimited** database size), so anyone can evaluate the vector stack at no cost.

### Who it's useful for

- **Db2 folks (and IBMers):** a clean, reproducible demo of the 12.1.5 vector datatype + `VECTOR_DISTANCE` + indexing, with a real workload (agent memory) on top — not a toy, and a **first-class native-Db2 integration** (see the native-adapter note above), not a lowest-common-denominator port.
- **Everyone else:** a genuinely useful tool — durable, private, cross-session memory for your AI assistant, backed by a database many teams already run.

### Use it with your AI agents (over MCP)

Point any MCP-capable client at the local mnemos server and it can store/recall memory as a tool: **Claude Code, the Claude desktop app, ChatGPT (desktop connectors), OpenAI Codex, Cursor, OpenClaw**, and others. The repo includes ready `.mcp.json` / config snippets for each (`quickstart/docs/AGENTS.md`).

Bonus for document-heavy workflows: there's an ingestion helper built on **IBM Docling** — convert PDFs/DOCX/PPTX to clean Markdown, chunk by heading, and load straight into Db2-backed memory (or export as a portable MIF bundle).

### Install (Db2 lane)

```bash
git clone https://github.com/ncz-os/mnemos
cd mnemos/quickstart
cp .env.example .env                 # set DB_PASSWORD
docker compose --profile db2 up -d
./scripts/init-db2-vectors.sh        # one-time: enable Db2 VECTOR datatype + vector indexing
curl -s localhost:5002/health        # {"status":"healthy", ...}
```

Store and recall (note the field is `"semantic": true`):

```bash
curl -s localhost:5002/v1/memories -H 'content-type: application/json' \
  -d '{"content":"Db2 12.1.5 stores agent memory in a native VECTOR column","category":"reference"}'

curl -s localhost:5002/v1/memories/search -H 'content-type: application/json' \
  -d '{"query":"where does mnemos keep memory","limit":3,"semantic":true}'
```

One config rule worth knowing: `MNEMOS_EMBEDDING_DIM` must match your embedder (1024 for bge-m3) — the Quick Start sets this correctly out of the box.

### A small documentation note (for anyone maintaining the Db2 vector docs)

While building this I hit a syntax gotcha worth flagging so the docs can be aligned. On **Db2 12.1.5**, the working vector-index syntax is:

```sql
CREATE VECTOR INDEX ix_name ON tbl(vec_col) WITH DISTANCE EUCLIDEAN;
```

…**after** setting the registry variable `DB2_VECTOR_INDEXING=YES` (which needs a `db2stop`/`db2start`). The alternative `CREATE INDEX … USING HNSW` / `USING DISKANN` form returns **SQL0104N** on 12.1.5. If any examples or docs still show the `CREATE INDEX … USING <algo>` form, they may be worth correcting — flagging it here in case it helps the doc team and other early adopters.

### Links

- Repo + Quick Start: `github.com/ncz-os/mnemos` → `quickstart/` (also on GitLab and Codeberg)
- Fully open source; Db2 CE, and the other backends it supports, are all free.

Happy to answer questions here — feedback from the Db2 community very welcome.

— Jason
