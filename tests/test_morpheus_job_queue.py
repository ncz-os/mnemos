import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mnemos.api.routes import morpheus as routes
from mnemos.domain.morpheus import jobs, runner
from mnemos.persistence.morpheus_jobs import MorpheusQueueFull, PostgresMorpheusJobs
from mnemos.persistence.postgres import PostgresBackend
from mnemos.persistence.schema import ensure_postgres_schema
from tests.test_postgres_concurrent_migration_race import fresh_pg_pool  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("MNEMOS_TEST_DB"), reason="requires disposable MNEMOS_TEST_DB")


@pytest_asyncio.fixture
async def backend(fresh_pg_pool, monkeypatch):  # noqa: F811 - pytest injects imported fixture
    pool, _ = fresh_pg_pool
    await ensure_postgres_schema(pool)
    backend = PostgresBackend(pool, SimpleNamespace())
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    return backend


async def enqueue(queue, namespace="alice"):
    return await queue.enqueue(window_hours=24, cluster_min_size=3, config={}, namespace=namespace)


@pytest.mark.asyncio
async def test_admission_and_claim_are_atomic_across_consumers(backend):
    outcomes = await asyncio.gather(
        *(enqueue(PostgresMorpheusJobs(backend)) for _ in range(12)), return_exceptions=True
    )
    assert sum(isinstance(r, str) for r in outcomes) == 1
    assert sum(isinstance(r, MorpheusQueueFull) for r in outcomes) == 11
    await enqueue(PostgresMorpheusJobs(backend), "bob")
    # Fresh objects model a restarted/multiworker process: state is in DB.
    claims = await asyncio.gather(*(PostgresMorpheusJobs(backend).claim() for _ in range(8)))
    assert sum(r is not None for r in claims) == 1
    assert await PostgresMorpheusJobs(backend).claim() is None


@pytest.mark.asyncio
async def test_queue_limits_and_all_namespace_scope(backend):
    queue = PostgresMorpheusJobs(backend, max_pending=2)
    await enqueue(queue, "a")
    with pytest.raises(MorpheusQueueFull):
        await enqueue(queue, None)
    await enqueue(queue, "b")
    with pytest.raises(MorpheusQueueFull):
        await enqueue(queue, "c")


@pytest.mark.asyncio
async def test_cancelled_execution_is_failed_and_not_reclaimed(backend, monkeypatch):
    run_id = await enqueue(PostgresMorpheusJobs(backend))
    entered = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "execute_existing_run", block)
    task = asyncio.create_task(jobs.run_next_job(backend))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with backend.transactional() as tx:
        assert await tx.conn.fetchval("SELECT status FROM morpheus_runs WHERE id=$1::uuid", run_id) == "failed"
    assert await PostgresMorpheusJobs(backend).claim() is None


@pytest.mark.asyncio
async def test_api_persists_job_without_executing_pipeline(backend, monkeypatch):
    monkeypatch.setattr(routes, "_require_morpheus_installed", lambda: None)
    monkeypatch.setattr(routes, "_require_postgres_backend", lambda: backend)

    async def forbidden(*args, **kwargs):
        raise AssertionError("HTTP request must not execute a dream")

    monkeypatch.setattr(runner, "run_dream", forbidden)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_root] = lambda: SimpleNamespace(role="root")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/morpheus/runs", json={"namespace": "a"})
        assert response.status_code == 202
        assert response.json()["phase"] == "queued"
        assert (await client.post("/admin/morpheus/runs", json={"namespace": "a"})).status_code == 429
    assert (await PostgresMorpheusJobs(backend).claim())[0] == response.json()["id"]


@pytest.mark.asyncio
async def test_legacy_orphan_sweep_does_not_discard_durable_jobs(backend):
    run_id = await enqueue(PostgresMorpheusJobs(backend))
    async with backend.transactional() as tx:
        await tx.conn.execute("UPDATE morpheus_runs SET started_at=NOW()-INTERVAL '3 hours' WHERE id=$1::uuid", run_id)
    await runner.sweep_orphan_runs(backend)
    assert (await PostgresMorpheusJobs(backend).claim())[0] == run_id


@pytest.mark.asyncio
async def test_live_execution_blocks_rollback_and_second_consumer_until_release(backend, monkeypatch):
    from mnemos.persistence.morpheus_jobs import MorpheusExecutionActive

    queue = PostgresMorpheusJobs(backend)
    run_id = await enqueue(queue)
    await enqueue(queue, "bob")
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(pool, active_run_id, **kwargs):
        assert active_run_id == run_id
        entered.set()
        await release.wait()
        await runner.finish_run(backend, active_run_id)

    monkeypatch.setattr(jobs, "execute_existing_run", block)
    task = asyncio.create_task(jobs.run_next_job(backend))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(MorpheusExecutionActive):
            await runner.rollback_run(backend, run_id)
        assert await jobs.run_next_job(backend) is False
        async with backend.transactional() as tx:
            assert await tx.conn.fetchval("SELECT status FROM morpheus_runs WHERE id=$1::uuid", run_id) == "running"
        release.set()
        assert await task is True
        await runner.rollback_run(backend, run_id)
        async with backend.transactional() as tx:
            assert await tx.conn.fetchval("SELECT status FROM morpheus_runs WHERE id=$1::uuid", run_id) == "rolled_back"
        assert await queue.claim() is not None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_single_connection_pool_refuses_admission_without_waiting(backend):
    from mnemos.persistence.morpheus_jobs import MorpheusQueueUnavailable

    small = SimpleNamespace(_pool=SimpleNamespace(get_max_size=lambda: 1))
    with pytest.raises(MorpheusQueueUnavailable, match="at least two"):
        await enqueue(PostgresMorpheusJobs(small))


@pytest.mark.asyncio
async def test_queue_execution_lock_released_by_cancel_for_orphan_rollback(backend, monkeypatch):
    run_id = await enqueue(PostgresMorpheusJobs(backend))
    entered = asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "execute_existing_run", block)
    task = asyncio.create_task(jobs.run_next_job(backend))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runner.rollback_run(backend, run_id)
    async with backend.transactional() as tx:
        assert await tx.conn.fetchval("SELECT status FROM morpheus_runs WHERE id=$1::uuid", run_id) == "rolled_back"


@pytest.mark.asyncio
async def test_api_maps_active_execution_rollback_to_conflict(backend, monkeypatch):
    monkeypatch.setattr(routes, "_require_morpheus_installed", lambda: None)
    monkeypatch.setattr(routes, "_require_postgres_backend", lambda: backend)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_root] = lambda: SimpleNamespace(role="root", user_id="review")
    queue = PostgresMorpheusJobs(backend)
    run_id = await enqueue(queue)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with queue.execution_slot() as acquired:
            assert acquired
            response = await client.delete(f"/admin/morpheus/runs/{run_id}")
            assert response.status_code == 409, response.text
        assert (await client.delete(f"/admin/morpheus/runs/{run_id}")).status_code == 200
