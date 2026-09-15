"""Optional PERSEPHONE archival worker.

The standalone ``main`` is now backend-neutral: it builds the configured
persistence backend (any of Postgres / SQLite / Oracle / Db2 / MySQL /
MariaDB) and hands the backend to the archival loop, which delegates the
cold-set sweep to :func:`mnemos.persistence.worker_lifecycle.sweep_for_archival`
when given an object that exposes ``transactional``. The legacy asyncpg-pool
call shape is preserved for the API lifespan, which still passes an
asyncpg pool from the Postgres-only legacy path; ``sweep_for_archival``
dispatches on ``hasattr(handle, "transactional") and not
hasattr(handle, "acquire")`` so both shapes are accepted without code
changes at the call site.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mnemos.core.config import get_settings
from mnemos.core.extras import is_extra_installed

logger = logging.getLogger(__name__)


async def persephone_archival_worker_loop(
    pool: Any,
    *,
    on_started: Any = None,
    on_success: Any = None,
    on_error: Any = None,
) -> None:
    """Run periodic PERSEPHONE archival sweeps when explicitly enabled.

    ``pool`` may be either a :class:`PersistenceBackend` (the standalone
    main path and every non-Postgres backend) or an asyncpg pool (the
    legacy API lifespan path); the underlying ``sweep_for_archival``
    dispatches on the handle's surface so the same loop works for both.
    """
    if not is_extra_installed("persephone"):
        logger.info("PERSEPHONE worker disabled (extra not installed)")
        return

    from mnemos.domain.persephone.runner import sweep_for_archival

    settings = get_settings().persephone
    if not settings.enabled:
        logger.info("PERSEPHONE archival worker disabled")
        return

    logger.info(
        "PERSEPHONE archival worker enabled namespace=%s archive_after_days=%d batch_size=%d interval=%.1fs",
        settings.namespace,
        settings.archive_after_days,
        settings.batch_size,
        settings.check_interval_seconds,
    )
    if on_started is not None:
        on_started()
    while True:
        try:
            archived = await sweep_for_archival(
                pool,
                namespace=settings.namespace,
                archive_after_days=settings.archive_after_days,
                batch_size=settings.batch_size,
            )
            if on_success is not None:
                on_success()
            if archived:
                logger.info("PERSEPHONE archival sweep archived %d row(s)", archived)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            logger.exception("PERSEPHONE archival sweep failed")
        await asyncio.sleep(settings.check_interval_seconds)


async def main() -> None:
    """Run against the configured persistence backend.

    Mirrors :func:`mnemos.workers.deletion_request_worker.main` -- the
    factory selects the backend from ``MNEMOS_DATABASE_DSN`` / settings,
    the loop runs against that backend, and ``backend.close()`` is
    invoked via ``finally`` on normal exit, errors, and cancellation.
    """
    from mnemos.core.lifecycle import build_configured_persistence_backend

    _backend_type, backend = await build_configured_persistence_backend()
    try:
        await persephone_archival_worker_loop(backend)
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
