#!/usr/bin/env python3
"""Backfill NULL embeddings on memories using the in-process embedder.

Architectural decision (mem_1779334716543_f8ebd4, operator-locked 2026-05-21):
MNEMOS embedding generation is ALWAYS in-process. After shipping the
runtime change, 2575 memories on PYTHIA (last 90 days) have NULL
embedding because the previous HTTP-based path pointed at an Ollama
container that was no longer running.

This script selects all memories WHERE embedding IS NULL AND content
is non-empty, batches them, embeds via mnemos.runtime.embedder, and
UPDATEs each row in place. Idempotent — safe to re-run; only operates
on still-NULL rows.

Usage:
    # In the mnemos container (uses MNEMOS_EMBED_MODEL_PATH default):
    docker exec mnemos-v3x-podman_mnemos_1 python -m scripts.backfill_embeddings

    # Outside the container, with explicit DSN:
    PG_DSN=postgresql://mnemos_user:mnemos_local@localhost:5433/mnemos \
        MNEMOS_EMBED_MODEL_PATH=/path/to/nomic-embed-text-v1.5.Q8_0.gguf \
        python scripts/backfill_embeddings.py

Env knobs:
    PG_DSN                  full DSN (overrides PG_HOST/PG_USER/...)
    PG_HOST PG_PORT PG_USER PG_PASSWORD PG_DATABASE  (read if PG_DSN unset)
    BACKFILL_BATCH_SIZE     rows per UPDATE chunk (default 50)
    BACKFILL_LIMIT          cap total rows processed (default 0 = no cap)
    BACKFILL_DRY_RUN        set non-empty to skip UPDATE (default unset)
    MNEMOS_EMBED_*          embedder knobs — see mnemos/runtime/embedder.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg

logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill")


def _dsn_from_env() -> str:
    if dsn := os.environ.get("PG_DSN"):
        return dsn
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    user = os.environ.get("PG_USER", "mnemos_user")
    pwd = os.environ.get("PG_PASSWORD", "mnemos_local")
    db = os.environ.get("PG_DATABASE", "mnemos")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


async def main() -> int:
    from mnemos.runtime.embedder import get_embedder

    batch_size = int(os.environ.get("BACKFILL_BATCH_SIZE", "50"))
    limit = int(os.environ.get("BACKFILL_LIMIT", "0"))
    dry_run = bool(os.environ.get("BACKFILL_DRY_RUN", ""))

    dsn = _dsn_from_env()
    log.info("[backfill] connecting to PG (DSN host=%s db=%s)", dsn.split("@")[-1], dsn.rsplit("/", 1)[-1])
    conn = await asyncpg.connect(dsn)
    try:
        total = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL AND content <> ''"
        )
        log.info("[backfill] %d memories with NULL embedding (non-empty content)", total)
        if total == 0:
            return 0
        if limit and limit < total:
            log.info("[backfill] BACKFILL_LIMIT=%d caps processing", limit)
            total = limit

        embedder = get_embedder()
        # Force-load + report dim before we burn time on selects
        await embedder.embed("warmup")
        log.info(
            "[backfill] embedder ready: model=%s embed_dim=%s gpu_layers=%d",
            embedder.model_path,
            embedder.embed_dim,
            embedder.n_gpu_layers,
        )

        done = 0
        failed = 0
        skipped_empty = 0
        offset_id: str | None = None
        started = time.perf_counter()

        while done < total:
            params: tuple = ()
            sql_offset = ""
            if offset_id is not None:
                sql_offset = "AND id > $1 "
                params = (offset_id,)
            chunk_sql = (
                "SELECT id, content FROM memories "
                "WHERE embedding IS NULL AND content IS NOT NULL AND content <> '' "
                f"{sql_offset}ORDER BY id LIMIT {batch_size}"
            )
            rows = await conn.fetch(chunk_sql, *params)
            if not rows:
                break

            for row in rows:
                if done >= total:
                    break
                mem_id: str = row["id"]
                content: str = row["content"] or ""
                if not content.strip():
                    skipped_empty += 1
                    offset_id = mem_id
                    continue
                vec = await embedder.embed(content)
                if not vec:
                    failed += 1
                    log.warning("[backfill] empty embed for id=%s len=%d", mem_id, len(content))
                    offset_id = mem_id
                    continue
                if not dry_run:
                    # pgvector format: '[v1,v2,...]'
                    vec_str = "[" + ",".join(repr(float(v)) for v in vec) + "]"
                    await conn.execute(
                        "UPDATE memories SET embedding = $1::vector, updated = now() WHERE id = $2",
                        vec_str,
                        mem_id,
                    )
                done += 1
                offset_id = mem_id

                if done % 50 == 0 or done == total:
                    elapsed = time.perf_counter() - started
                    rate = done / max(elapsed, 1e-6)
                    eta = (total - done) / max(rate, 1e-6)
                    log.info(
                        "[backfill] %d/%d (%.1f rec/s, eta %.0fs) failed=%d skipped_empty=%d",
                        done,
                        total,
                        rate,
                        eta,
                        failed,
                        skipped_empty,
                    )

        elapsed = time.perf_counter() - started
        log.info(
            "[backfill] DONE in %.1fs: filled=%d failed=%d skipped_empty=%d dry_run=%s",
            elapsed,
            done,
            failed,
            skipped_empty,
            bool(dry_run),
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
