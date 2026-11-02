"""F-3 — embed the pulled federation rows that arrived without a vector.

A replica stores a pulled row with ``embedding IS NULL`` whenever it cannot
use the peer's vector: the peer did not opt into ``copy_embeddings``, the
fail-closed model/dimension gate declined it, or the write failed.  Vector
search excludes NULL-embedding rows, so those rows are invisible to semantic
recall until something embeds them HERE, in this node's own vector space.

That "something" is this module.  It is the in-app counterpart of
``scripts/backfill_embeddings*.py`` -- the same repair, run by the service
instead of by hand -- and it uses the SAME machinery: the vector is written
through the memories repository's ``upsert_memory_embedding``, so the
per-backend shape (inline column, join table, vec0 mirror) is the backend's
business, not this worker's.

The queue is the PREDICATE itself
(``federation_source IS NOT NULL AND embedding IS NULL``), exposed through
``FederationRepository.list_federated_missing_embeddings`` /
``count_federated_missing_embeddings``.  Nothing is held in memory between
passes, so the queue is durable by construction: a restart resumes, and a row
can never be lost between the sync that created it and the pass that embeds
it.

Three properties the field incident demands:

* **bounded** -- a pass embeds at most ``max_rows`` rows, and walks the
  queue by id so one stuck row never blocks the rows behind it;
* **idempotent** -- a row is embedded exactly once: the vector lands in the
  same transaction, and the row leaves the predicate;
* **honest** -- a row that could not be embedded is counted as failed and
  stays queued, and the queue depth is reported per peer so an operator sees
  WHICH peer lags.

The dimension guard (F-4, as far as it is merged) is applied here: a vector
whose width disagrees with the configured dimension is REFUSED, never
written.  A wrong-width vector in the column poisons every later search, so
refusing it and leaving the row queued is the only safe outcome.

Content is secret-redacted (stored ingest spans, else recomputed) before it
reaches the embedder, matching the local write path, so a credential never
enters the vector index.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

WORKER_NAME = "federation_local_embed"
DEFAULT_INTERVAL_SECS = 60.0
MAX_BACKOFF_SECS = 3600.0
DEFAULT_BATCH = 64
DEFAULT_MAX_ROWS_PER_PASS = 2000

EmbedFn = Callable[[str], Awaitable[list]]


def _expected_dim(backend: Any) -> int | None:
    """The dimension this node's column expects, or None when unknown.

    The memories repository owns the guard (``_require_dim``); this is the
    same number, read the same way, so the worker refuses a wrong-width
    vector BEFORE it reaches the backend.
    """
    repo = getattr(backend, "memories", None)
    dim = getattr(repo, "_expected_embedding_dim", None)
    if isinstance(dim, int) and dim > 0:
        return dim
    from mnemos.core.config import embedding_dim_env

    return embedding_dim_env()


async def _default_embed(text: str) -> list:
    from mnemos.runtime.embedder import embed_text

    return await embed_text(text)


def _row_metadata(row: Any) -> Any:
    """The row's metadata, parsed from JSON when the backend returns text."""
    metadata = row.get("metadata") if hasattr(row, "get") else None
    if isinstance(metadata, (str, bytes, bytearray)):
        try:
            return json.loads(metadata)
        except (TypeError, ValueError):
            return {}
    return metadata


async def embed_pending_once(
    backend: Any,
    *,
    embed: Optional[EmbedFn] = None,
    batch_size: int = DEFAULT_BATCH,
    max_rows: int = DEFAULT_MAX_ROWS_PER_PASS,
) -> dict[str, int]:
    """One pass over the queue.  Returns ``embedded``, ``failed``, ``remaining``.

    The queue is walked by ``id`` (keyset), so a row that keeps failing is
    attempted once per pass and never blocks the rows behind it.  Each
    vector is written in its own transaction: one bad write must not roll
    back the rows already embedded in this pass.
    """
    from mnemos.core.secret_detection import redact_field_with_stored

    embed = embed or _default_embed
    dim = _expected_dim(backend)
    embedded = failed = 0
    after: Optional[str] = None
    while embedded + failed < max_rows:
        async with backend.transactional() as tx:
            rows = await backend.federation.list_federated_missing_embeddings(
                tx,
                after_id=after,
                limit=min(batch_size, max_rows - embedded - failed),
            )
        if not rows:
            break
        for row in rows:
            memory_id = str(row["id"])
            after = memory_id
            try:
                text = redact_field_with_stored(row.get("content"), _row_metadata(row), "content")
                vec = await embed(text) if (text or "").strip() else []
                if not vec:
                    failed += 1
                    logger.warning(
                        "[federation/local-embed] %s produced no vector; still queued",
                        memory_id,
                    )
                    continue
                if dim is not None and len(vec) != dim:
                    # F-4's dimension guard: a wrong-width vector must never
                    # reach the column. Leave the row queued and say so.
                    failed += 1
                    logger.warning(
                        "[federation/local-embed] %s produced a %s-dim vector but the "
                        "column expects %s-dim; refusing it, the row stays queued",
                        memory_id,
                        len(vec),
                        dim,
                    )
                    continue
                async with backend.transactional() as tx:
                    await backend.memories.upsert_memory_embedding(tx, memory_id, [float(v) for v in vec])
                embedded += 1
            except Exception as exc:  # one bad row must not stop the pass
                failed += 1
                logger.warning(
                    "[federation/local-embed] %s failed: %s: %s",
                    memory_id,
                    type(exc).__name__,
                    exc,
                )
    async with backend.transactional() as tx:
        remaining = await backend.federation.count_federated_missing_embeddings(tx)
    return {"embedded": embedded, "failed": failed, "remaining": remaining}


async def federation_local_embed_loop(
    backend: Any,
    *,
    embed: Optional[EmbedFn] = None,
    interval: float = DEFAULT_INTERVAL_SECS,
) -> None:
    """The background loop.

    Backs off exponentially while nothing can be embedded (no local model,
    dimension mismatch, embedder down) and reports ``healthy`` /
    ``degraded`` / ``error`` in ``lifecycle._worker_status``.
    """
    from mnemos.core import lifecycle

    status = lifecycle._worker_status
    status[WORKER_NAME] = "starting"
    stalls = 0
    logger.info("federation local-embed worker started")
    while True:
        try:
            result = await embed_pending_once(backend, embed=embed)
            if result["failed"] and not result["embedded"]:
                stalls += 1
                status[WORKER_NAME] = "degraded"
                status[f"{WORKER_NAME}_last_error"] = (
                    f"{result['failed']} pulled rows could not be embedded locally; "
                    f"{result['remaining']} queued"
                )
                if stalls == 1:
                    logger.warning(
                        "[federation/local-embed] no row could be embedded (%s queued); "
                        "check the local embedder and MNEMOS_EMBEDDING_DIM",
                        result["remaining"],
                    )
            else:
                stalls = 0
                status[WORKER_NAME] = "healthy"
                status[f"{WORKER_NAME}_last_success"] = time.time()
            if result["embedded"]:
                logger.info(
                    "[federation/local-embed] embedded %s, failed %s, queued %s",
                    result["embedded"],
                    result["failed"],
                    result["remaining"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            stalls += 1
            status[WORKER_NAME] = "error"
            status[f"{WORKER_NAME}_last_error"] = "local-embed pass failed"
            logger.exception("[federation/local-embed] pass failed")
        await asyncio.sleep(min(interval * (2**stalls), MAX_BACKOFF_SECS))
