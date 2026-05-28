from __future__ import annotations

import asyncio
import inspect
import os
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Query, Response

from mnemos.hive_mind.oracle_repository import OracleHiveMindRepository
from mnemos.hive_mind.repository import SqliteHiveMindRepository
from mnemos.persistence.oracle import _parse_oracle_dsn

try:
    from mnemos.hive_mind.db2_repository import Db2HiveMindRepository
except ImportError:  # pragma: no cover - guard for future Db2 hive port.
    Db2HiveMindRepository = None  # type: ignore[assignment,misc]


N_JOBS = 1000
N_WORKERS = 8
WORKER_KIND = "concurrency-parity-worker"
ORACLE_DSN = os.environ.get("ORACLE_DSN", "").strip()
DB2_DSN = os.environ.get("DB2_DSN", "").strip()


def _repo_params() -> list[Any]:
    params: list[Any] = [
        pytest.param(SqliteHiveMindRepository, id="sqlite"),
    ]
    params.append(
        pytest.param(
            OracleHiveMindRepository,
            id="oracle",
            marks=pytest.mark.skipif(not ORACLE_DSN, reason="ORACLE_DSN not set; live Oracle parity arm skipped"),
        )
    )
    params.append(
        pytest.param(
            Db2HiveMindRepository,
            id="db2",
            marks=pytest.mark.skipif(
                Db2HiveMindRepository is None or not DB2_DSN,
                reason="Db2HiveMindRepository or DB2_DSN not available; Db2 parity arm skipped",
            ),
        )
    )
    return params


def _make_oracle_repo() -> OracleHiveMindRepository:
    pytest.importorskip("oracledb", reason="oracledb driver not installed")
    kwargs = _parse_oracle_dsn(ORACLE_DSN)
    user = kwargs.pop("user", os.environ.get("ORACLE_USER", "")).strip()
    password = kwargs.pop("password", os.environ.get("ORACLE_PASSWORD", "")).strip()
    if not user or not password:
        pytest.skip("ORACLE_DSN must include user/password or ORACLE_USER/ORACLE_PASSWORD must be set")
    return OracleHiveMindRepository(
        user=user,
        password=password,
        dsn=kwargs["dsn"],
        min_pool=1,
        max_pool=N_WORKERS,
    )


@pytest_asyncio.fixture(params=_repo_params())
async def repo_case(request: pytest.FixtureRequest, tmp_path) -> AsyncIterator[tuple[str, Any, str]]:
    repo_cls = request.param
    project = f"concurrency-parity-{request.node.name}-{uuid.uuid4().hex[:12]}"

    if repo_cls is SqliteHiveMindRepository:
        repo = SqliteHiveMindRepository(str(tmp_path / "hive.sqlite3"))
        await repo.init()
        yield "sqlite", repo, project
        await repo.close()
        return

    if repo_cls is OracleHiveMindRepository:
        repo = _make_oracle_repo()
        await _cleanup_jobs(repo, project)
        try:
            yield "oracle", repo, project
        finally:
            await _cleanup_jobs(repo, project)
            await repo.close()
        return

    pytest.skip("Db2HiveMindRepository is not implemented yet")


async def _cleanup_jobs(repo: Any, project: str) -> None:
    if isinstance(repo, SqliteHiveMindRepository):
        await asyncio.to_thread(_cleanup_sqlite_jobs, repo.db_path, project)
        return

    if isinstance(repo, OracleHiveMindRepository):
        await repo._run(_cleanup_oracle_jobs, repo, project)
        return

    if hasattr(repo, "delete_test_jobs"):
        await repo.delete_test_jobs(project=project)


def _cleanup_sqlite_jobs(db_path: str, project: str) -> None:
    with sqlite3.connect(db_path, timeout=30.0) as db:
        db.execute("DELETE FROM memory_jobs WHERE project=?", (project,))
        db.commit()


def _cleanup_oracle_jobs(repo: OracleHiveMindRepository, project: str) -> None:
    with repo._pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM memory_jobs WHERE project = :project", {"project": project})
        conn.commit()


async def _seed_jobs(repo: Any, *, project: str, job_ids: list[str]) -> None:
    base_ts = time.time()
    for offset in range(0, len(job_ids), 50):
        batch = job_ids[offset : offset + 50]
        await asyncio.gather(
            *(
                repo.insert_job(
                    job_id=job_id,
                    submitter_urn="urn:agent:test:concurrency-parity",
                    parent_job_id=None,
                    kind="concurrency-parity",
                    description="Concurrency parity claim probe",
                    priority=10,
                    eligible_kinds=[WORKER_KIND],
                    project=project,
                    tags={
                        "source": "tests/hive_mind/test_concurrency_parity.py",
                        "hive_job": "019e6d13-03e4",
                    },
                    max_retries=0,
                    created_at=base_ts + offset / 1_000_000,
                )
                for job_id in batch
            )
        )


def _make_app(repo: Any) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/jobs/next")
    async def claim_next_job(
        response: Response,
        agent_urn: str = Query(...),
    ) -> dict[str, Any] | None:
        claimed = await repo.claim_next_job(agent_urn=agent_urn, agent_kind=WORKER_KIND)
        if claimed is None:
            response.status_code = 204
            return None
        return claimed

    return app


async def _poll_until_empty(client: httpx.AsyncClient, worker_idx: int) -> list[str]:
    claimed: list[str] = []
    agent_urn = f"urn:agent:test:worker_{worker_idx}"
    while True:
        response = await client.post("/v1/jobs/next", params={"agent_urn": agent_urn})
        if response.status_code == 204:
            return claimed
        assert response.status_code == 200, response.text
        body = response.json()
        claimed.append(body.get("claim_id", body["id"]))
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_next_job_claims_are_concurrency_safe(repo_case: tuple[str, Any, str]) -> None:
    backend_name, repo, project = repo_case
    seeded_ids = [str(uuid.uuid4()) for _ in range(N_JOBS)]
    await _seed_jobs(repo, project=project, job_ids=seeded_ids)

    app = _make_app(repo)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        worker_results = await asyncio.gather(*(_poll_until_empty(client, idx) for idx in range(N_WORKERS)))

    claim_ids = [claim_id for worker_claims in worker_results for claim_id in worker_claims]
    duplicates = {claim_id: count for claim_id, count in Counter(claim_ids).items() if count > 1}
    missed = set(seeded_ids) - set(claim_ids)
    foreign = set(claim_ids) - set(seeded_ids)
    queued = await repo.list_jobs(status="queued", project=project, limit=N_JOBS + 1)

    assert len(claim_ids) == N_JOBS, (backend_name, len(claim_ids), len(missed), len(foreign))
    assert duplicates == {}
    assert missed == set()
    assert foreign == set()
    assert queued == []


def test_oracle_claim_path_uses_skip_locked_and_rowcount_guard() -> None:
    source = inspect.getsource(OracleHiveMindRepository._claim_next_job_sync)
    compact = " ".join(source.upper().split())

    assert "FOR UPDATE SKIP LOCKED" in compact
    assert "UPDATE MEMORY_JOBS" in compact
    assert "AND STATUS = 'QUEUED'" in compact
    assert "ROWCOUNT != 1" in compact
    # Oracle's row-tuple compare claim path depends on the deployed P0-1
    # rowcount fix: exactly one updated row is required before a claim is
    # returned. Without it, SKIP LOCKED/isolation drift can hide duplicates.
