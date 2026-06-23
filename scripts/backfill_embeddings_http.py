"""
Backfill memories.embedding via an HTTP embed endpoint.
Runs inside the mnemos-api container (has the DB driver + httpx).

Pulls rows where embedding IS NULL, in batches of BATCH_SIZE,
posts batched content to the embed server, UPSERTs the vector column.

Backend-aware: reads MNEMOS_DATABASE_DSN and dispatches on the scheme.
  • oracle://user:pass@host:port/service   -> python-oracledb, VECTOR bind
  • db2://user:pass@host:port/database      -> ibm_db_dbi, VECTOR(?, dim, FLOAT32)
(Postgres has its own pgvector-native path in backfill_embeddings.py.)

Idempotent: re-running picks up where it left off.
"""

import asyncio
import array
import math
import os
import sys
import time
from urllib.parse import unquote, urlparse

import httpx

# ── Config ────────────────────────────────────────────────────────────────
DSN = os.environ.get("MNEMOS_DATABASE_DSN", "oracle://mnemos:mnemos_dev@127.0.0.1:1521/ORCLPDB1")
EMBED_URL = os.environ.get("MNEMOS_EMBED_HTTP_URL", "http://192.168.207.64:8090/v1/embeddings")
EMBED_MODEL = os.environ.get("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")
BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "32"))
MAX_TEXT_CHARS = 6000  # ~1500 tokens safe under bge-m3 n_ctx=8192 with margin

# Rows still missing an embedding. Shared by every backend.
_SELECT_WHERE = (
    "embedding IS NULL AND content IS NOT NULL "
    "AND deleted_at IS NULL AND archived_at IS NULL"
)


def _coerce_text(content) -> str | None:
    """Normalize a driver-returned content value (LOB / bytes / str) to text."""
    if content is None:
        return None
    if hasattr(content, "read"):  # Oracle LOB
        content = content.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return content or None


def _vector_literal(vec) -> str:
    """Format a float vector as a Db2/MySQL VECTOR string literal '[..]'.

    Matches mnemos.persistence._validate_and_format_vector so the bytes we
    write are identical to the inline-embed path in the running backend.
    """
    parts = []
    for i, v in enumerate(vec):
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"embedding[{i}] non-finite ({f!r})")
        parts.append(f"{f:.7f}")
    return "[" + ",".join(parts) + "]"


# ── Backends ────────────────────────────────────────────────────────────────
class _OracleBackend:
    scheme = "oracle"

    def __init__(self, dsn: str):
        # urlparse + unquote (not a regex) so URL-encoded creds/service
        # names — e.g. a password containing %40 — decode correctly.
        rest = dsn.split("://", 1)[1] if "://" in dsn else dsn
        parsed = urlparse(f"oracle://{rest}")
        self.user = unquote(parsed.username) if parsed.username else ""
        self.pwd = unquote(parsed.password) if parsed.password else ""
        self.host = parsed.hostname or "127.0.0.1"
        self.port = str(parsed.port or 1521)
        self.svc = (parsed.path or "/").lstrip("/") or "ORCLPDB1"
        if not self.user:
            raise ValueError(f"unparseable oracle DSN: {dsn!r}")

    def connect(self):
        import oracledb
        print(f"[backfill] oracle host={self.host}:{self.port} svc={self.svc} user={self.user}", flush=True)
        return oracledb.connect(user=self.user, password=self.pwd, dsn=f"{self.host}:{self.port}/{self.svc}")

    def count_remaining(self, conn) -> int:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM memories WHERE {_SELECT_WHERE}")
        n = cur.fetchone()[0]
        cur.close()
        return n

    def fetch_batch(self, conn, batch_size):
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, content FROM memories WHERE {_SELECT_WHERE} "
            f"FETCH FIRST {int(batch_size)} ROWS ONLY"
        )
        rows = cur.fetchall()
        cur.close()
        out = []
        for mid, content in rows:
            text = _coerce_text(content)
            if text:
                out.append((mid, text[:MAX_TEXT_CHARS]))
        return out

    def update(self, conn, items_with_vecs):
        cur = conn.cursor()
        # Oracle VECTOR bind expects array.array('f', ...) for FLOAT32.
        binds = [{"emb": array.array("f", vec), "mid": mid} for mid, vec in items_with_vecs]
        cur.executemany("UPDATE memories SET embedding = :emb WHERE id = :mid", binds)
        conn.commit()
        cur.close()


class _Db2Backend:
    scheme = "db2"

    def __init__(self, dsn: str):
        # db2://user:pass@host:port/database  → ibm_db_dbi conn-string kwargs.
        rest = dsn.split("://", 1)[1] if "://" in dsn else dsn
        parsed = urlparse(f"db2://{rest}")
        self.database = (parsed.path or "/").lstrip("/") or "MNEMOS"
        self.host = parsed.hostname or "localhost"
        self.port = str(parsed.port or 50000)
        self.uid = unquote(parsed.username) if parsed.username else "db2inst1"
        self.pwd = unquote(parsed.password) if parsed.password else ""

    def connect(self):
        import ibm_db_dbi
        conn_str = (
            f"DATABASE={self.database};HOSTNAME={self.host};PORT={self.port};"
            f"PROTOCOL=TCPIP;UID={self.uid};PWD={self.pwd};"
        )
        print(f"[backfill] db2 host={self.host}:{self.port} db={self.database} uid={self.uid}", flush=True)
        return ibm_db_dbi.connect(conn_str, "", "")

    def count_remaining(self, conn) -> int:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM memories WHERE {_SELECT_WHERE}")
        n = cur.fetchone()[0]
        cur.close()
        return n

    def fetch_batch(self, conn, batch_size):
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, content FROM memories WHERE {_SELECT_WHERE} "
            f"FETCH FIRST {int(batch_size)} ROWS ONLY"
        )
        rows = cur.fetchall()
        cur.close()
        out = []
        for mid, content in rows:
            text = _coerce_text(content)
            if text:
                out.append((mid, text[:MAX_TEXT_CHARS]))
        return out

    def update(self, conn, items_with_vecs):
        # Db2 native VECTOR column: bind the '[..]' literal into
        # VECTOR(?, dim, FLOAT32) — same path as upsert_memory_embedding.
        # ibm_db_dbi has no executemany VECTOR support, so loop per row.
        cur = conn.cursor()
        for mid, vec in items_with_vecs:
            lit = _vector_literal(vec)
            cur.execute(
                f"UPDATE memories SET embedding = VECTOR(?, {len(vec)}, FLOAT32) WHERE id = ?",
                (lit, mid),
            )
        conn.commit()
        cur.close()


def _make_backend(dsn: str):
    scheme = dsn.split("://", 1)[0].lower() if "://" in dsn else ""
    if scheme == "oracle":
        return _OracleBackend(dsn)
    if scheme == "db2":
        return _Db2Backend(dsn)
    raise SystemExit(
        f"[backfill] unsupported DSN scheme {scheme!r}. This script handles "
        f"oracle:// and db2://; use scripts/backfill_embeddings.py for postgres."
    )


# ── Embed ─────────────────────────────────────────────────────────────────
async def embed_batch(client, texts):
    """POST batch to the embed server; returns (vectors, elapsed_seconds)."""
    t0 = time.monotonic()
    r = await client.post(EMBED_URL, json={"model": EMBED_MODEL, "input": texts}, timeout=60.0)
    dt = time.monotonic() - t0
    if r.status_code != 200:
        print(f"  EMBED FAIL status={r.status_code} body={r.text[:200]}", flush=True)
        return None, dt
    data = r.json()
    items = data["data"]
    # OpenAI-compatible embedding APIs do not guarantee response order;
    # re-order by `index` when present so vectors pair with the right
    # memory id (otherwise embeddings get written to the wrong rows).
    if items and all(isinstance(d, dict) and "index" in d for d in items):
        items = sorted(items, key=lambda d: d["index"])
    return [d["embedding"] for d in items], dt


async def main():
    backend = _make_backend(DSN)
    print(f"[backfill] backend={backend.scheme} EMBED_URL={EMBED_URL} MODEL={EMBED_MODEL} BATCH={BATCH_SIZE}", flush=True)
    conn = backend.connect()

    total_remaining = backend.count_remaining(conn)
    print(f"[backfill] {total_remaining} rows need embedding", flush=True)
    if total_remaining == 0:
        print("[backfill] nothing to do", flush=True)
        return

    async with httpx.AsyncClient() as client:
        done = 0
        t_start = time.monotonic()
        while True:
            batch = backend.fetch_batch(conn, BATCH_SIZE)
            if not batch:
                break
            ids = [x[0] for x in batch]
            texts = [x[1] for x in batch]
            vecs, dt = await embed_batch(client, texts)
            if vecs is None:
                # Without persistence the same rows come back; bail to avoid a hot loop.
                print(f"  skip batch (embed fail), batch_ids[0]={ids[0]}", flush=True)
                print("[backfill] ABORT — embed endpoint failing", flush=True)
                sys.exit(1)
            if len(vecs) != len(ids):
                print(f"  WARN length mismatch vecs={len(vecs)} ids={len(ids)}; using min", flush=True)
            n = min(len(vecs), len(ids))
            backend.update(conn, [(ids[i], vecs[i]) for i in range(n)])
            done += n
            elapsed = time.monotonic() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total_remaining - done) / rate if rate > 0 else float("inf")
            print(
                f"  batch ok: +{n} rows ({done}/{total_remaining}) "
                f"embed={dt*1000:.0f}ms rate={rate:.1f} rows/s eta={eta:.0f}s",
                flush=True,
            )
    conn.close()
    print("[backfill] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
