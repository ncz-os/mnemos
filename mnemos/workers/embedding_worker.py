"""Backfill embeddings for memories that semantic search cannot reach.

Embedding is otherwise inline-only: ``create_memory`` embeds on the write
path, and nothing ever revisits a row that missed out. Two ordinary paths
leave rows without a usable vector:

* **Federation pulls.** ``/v1/federation/feed`` only ships ``embedding_b64``
  when the peer sets ``copy_embeddings``; the consumer stores what it is
  given and never computes a vector itself. Every pulled row on a peer that
  does not copy embeddings lands unembedded, so a federated replica cannot
  answer a semantic query about anything it replicated.
* **Inline writes on join-table backends.** SQLite and MariaDB read
  ``memory_embeddings`` in ``semantic_search`` while the create path writes
  ``memories.embedding`` (SQLite advertises this as
  ``inline_embedding_searchable = False``). Those rows hold a vector and are
  still unreachable.

Neither is visible in ``/health`` or in search results -- a semantic query
just quietly returns fewer rows -- so this runs as a worker rather than an
operator-remembered script.

The worker is backend-neutral: it drives ``backend.memories`` and never
touches the asyncpg pool, so it runs on every persistence backend.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("mnemos.workers.embedding_worker")

# Rows per pass. Embedding is CPU/GPU-bound and single-flight through the
# in-process embedder, so a pass is deliberately small: the worker is meant to
# catch up steadily in the background, not to monopolise the embedder while
# request-path embeds queue behind it.
DEFAULT_BATCH_SIZE = 64
DEFAULT_INTERVAL_SECS = 60.0
# Backoff after an idle pass. Steady state is "nothing to do", and polling a
# table every minute forever to learn that is waste.
IDLE_INTERVAL_SECS = 300.0


async def embed_pending_batch(backend: Any, *, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Embed one batch. Returns the number of rows successfully embedded."""
    from mnemos.runtime.embedder import embed_text

    async with backend.transactional() as tx:
        rows = await backend.memories.fetch_memories_missing_embeddings(tx, batch_size)
    if not rows:
        return 0

    embedded = 0
    for row in rows:
        memory_id = row["id"]
        content = row["content"]
        if not content:
            continue
        try:
            vector = await embed_text(content)
        except Exception:
            # One unembeddable row must not stall the queue behind it. It stays
            # selected next pass; a permanently bad row is visible in the log
            # rather than silently dropping the whole batch.
            logger.exception("embedding worker: embed failed for memory %s", memory_id)
            continue
        if not vector:
            logger.warning("embedding worker: embedder returned no vector for memory %s", memory_id)
            continue
        async with backend.transactional() as tx:
            await backend.memories.upsert_memory_embedding(tx, memory_id, vector)
        embedded += 1

    return embedded


async def embedding_worker_loop(
    backend: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    interval_secs: float = DEFAULT_INTERVAL_SECS,
    idle_interval_secs: float = IDLE_INTERVAL_SECS,
) -> None:
    """Background loop: embed rows semantic search cannot reach.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    logger.info("embedding worker started (batch_size=%d)", batch_size)
    delay = interval_secs
    while True:
        try:
            await asyncio.sleep(delay)
            embedded = await embed_pending_batch(backend, batch_size=batch_size)
            if embedded:
                logger.info("embedding worker: embedded %d memories", embedded)
                delay = interval_secs
            else:
                delay = idle_interval_secs
        except asyncio.CancelledError:
            logger.info("embedding worker cancelled")
            raise
        except NotImplementedError:
            # Backend cannot enumerate unembedded rows. Stopping is honest:
            # looping forever would look healthy while doing nothing.
            logger.warning(
                "embedding worker: %s cannot enumerate unembedded memories; worker stopping",
                type(backend).__name__,
            )
            return
        except Exception:  # pragma: no cover - defensive, matches sibling workers
            logger.exception("embedding worker iteration failed")
            delay = idle_interval_secs
