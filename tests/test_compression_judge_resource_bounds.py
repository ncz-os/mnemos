import asyncio
import threading

import pytest

from mnemos.domain.compression import judge as module
from mnemos.runtime.bounded_cpu import BoundedExecutor


async def _score(judge, original="hello", candidate="hello"):
    return await judge.score(
        original=original, candidate_encoded=candidate, candidate_narrated=candidate, candidate_engine_id="test"
    )


@pytest.mark.asyncio
async def test_exact_scoring_is_preserved_and_oversize_is_not_truncated():
    judge = module.DeterministicJudge(max_edit_cells=100)
    result = await _score(judge)
    assert result.fidelity == module._judge_deterministic_score("hello", "hello")["composite"]
    assert await _score(judge, "a" * 11, "a" * 11) is None


@pytest.mark.asyncio
async def test_cancelled_cpu_job_retains_admission_until_worker_finishes(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    executor = BoundedExecutor(workers=1, capacity=1)

    def slow_score(*_):
        entered.set()
        assert release.wait(5)
        return dict(composite=1.0, bigram_overlap=1.0, edit_distance_ratio=1.0, length_ratio=1.0)

    monkeypatch.setattr(module, "_judge_deterministic_score", slow_score)
    judge = module.DeterministicJudge(executor=executor)
    task = asyncio.create_task(_score(judge))
    try:
        # This loop would time out if score blocked the asyncio thread.
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _score(judge) is None
        release.set()
        # The permit is returned by the underlying future's completion.
        async with asyncio.timeout(2):
            while await _score(judge) is None:
                await asyncio.sleep(0.005)
    finally:
        release.set()
        executor.close()


def test_executor_accepts_calls_from_successive_event_loops():
    executor = BoundedExecutor(workers=1, capacity=1)
    try:
        judge = module.DeterministicJudge(executor=executor)
        assert asyncio.run(_score(judge)).fidelity == 1.0
        assert asyncio.run(_score(judge)).fidelity == 1.0
    finally:
        executor.close()
