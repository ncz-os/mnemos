"""Bounded execution of persisted MORPHEUS jobs, outside HTTP requests."""

import asyncio
import logging

from mnemos.persistence.morpheus_jobs import PostgresMorpheusJobs

from .runner import execute_existing_run, fail_run

logger = logging.getLogger(__name__)


async def run_next_job(backend, *, timeout_seconds: float = 600) -> bool:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    queue = PostgresMorpheusJobs(backend)
    async with queue.execution_slot() as acquired:
        if not acquired:
            return False
        job = await queue.claim()
        if job is None:
            return False
        run_id, config = job
        try:
            async with asyncio.timeout(timeout_seconds):
                await execute_existing_run(backend._pool, run_id, config=config)
        except asyncio.CancelledError:
            await asyncio.shield(fail_run(backend, run_id, "queue worker cancelled; inspect partial run before retry"))
            raise
        except Exception as exc:
            await fail_run(backend, run_id, f"queue execution failed: {type(exc).__name__}: {exc}")
            logger.exception("MORPHEUS queued run %s failed", run_id)
        return True


async def morpheus_job_worker(backend) -> None:
    while True:
        try:
            if await run_next_job(backend):
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MORPHEUS queue polling failed")
        await asyncio.sleep(1)
