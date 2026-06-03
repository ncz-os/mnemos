"""Tests for the PANTHEON fallback + retry orchestrator.

Sync tests drive the coroutine with ``asyncio.run`` so no pytest-asyncio config
is needed. The executor and clock are fakes: no network, no real sleeping.
"""

from __future__ import annotations

import asyncio

import pytest

from mnemos.domain.pantheon.errors import RetryAction, normalize_error
from mnemos.domain.pantheon.fallback import (
    AllDeploymentsFailed,
    execute_with_fallbacks,
)


class FakeHTTPError(Exception):
    def __init__(self, status_code=None, body=None, retry_after=None):
        super().__init__(body or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


def _classify(exc):
    return normalize_error(status_code=getattr(exc, "status_code", None), body=getattr(exc, "body", None))


def _scripted_call(script):
    """script: dict[deployment] -> list of outcomes consumed per call.
    An outcome is either an Exception (raised) or a value (returned)."""
    state = {k: list(v) for k, v in script.items()}
    calls = []

    async def call(deployment):
        calls.append(deployment)
        outcome = state[deployment].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return call, calls


def _recording_sleep():
    slept = []

    async def sleep(d):
        slept.append(d)

    return sleep, slept


def _run(chain, call, **kw):
    kw.setdefault("classify", _classify)
    kw.setdefault("rng", lambda: 0.0)
    return asyncio.run(execute_with_fallbacks(chain, call, **kw))


def test_success_on_first_deployment():
    call, calls = _scripted_call({"a": ["ok"]})
    res = _run(["a", "b"], call)
    assert res.result == "ok"
    assert res.deployment == "a"
    assert res.attempts == ()
    assert calls == ["a"]


def test_400_falls_over_to_next_no_retry():
    call, calls = _scripted_call({"a": [FakeHTTPError(400, "bad")], "b": ["ok"]})
    sleep, slept = _recording_sleep()
    res = _run(["a", "b"], call, sleep=sleep)
    assert res.result == "ok"
    assert res.deployment == "b"
    assert [a.action for a in res.attempts] == [RetryAction.FALLOVER]
    assert calls == ["a", "b"]
    assert slept == []  # non-retryable never sleeps


def test_the_outage_400_missing_input_falls_over_instantly():
    call, calls = _scripted_call(
        {
            "lead": [FakeHTTPError(400, "OpenAI Codex API error (400): missing_required_parameter: input")],
            "deepseek": ["done"],
        }
    )
    sleep, slept = _recording_sleep()
    res = _run(["lead", "deepseek"], call, sleep=sleep)
    assert res.result == "done"
    assert res.deployment == "deepseek"
    assert slept == []
    assert res.attempts[0].action is RetryAction.FALLOVER


def test_transient_500_retries_same_then_falls_over():
    # a: 500 three times (initial + 2 retries all fail) -> fall over; b ok
    call, calls = _scripted_call({"a": [FakeHTTPError(500), FakeHTTPError(500), FakeHTTPError(500)], "b": ["ok"]})
    sleep, slept = _recording_sleep()
    res = _run(["a", "b"], call, num_retries=2, sleep=sleep)
    assert res.result == "ok"
    assert calls == ["a", "a", "a", "b"]  # 1 initial + 2 retries on a, then b
    assert len(slept) == 2  # two backoff sleeps between the three a-attempts
    # 'a' had siblings -> backoff is instant (0.0)
    assert slept == [0.0, 0.0]


def test_transient_retry_succeeds_on_same_deployment():
    call, calls = _scripted_call({"a": [FakeHTTPError(503), "recovered"]})
    sleep, slept = _recording_sleep()
    res = _run(["a"], call, num_retries=2, sleep=sleep)
    assert res.result == "recovered"
    assert calls == ["a", "a"]
    # 'a' is the only deployment (no siblings) -> real backoff applied once
    assert slept == [pytest.approx(0.5)]  # attempt 0, rng=0 -> 0.5 + 0


def test_all_non_retryable_raises_all_failed():
    call, calls = _scripted_call({"a": [FakeHTTPError(400)], "b": [FakeHTTPError(404)]})
    with pytest.raises(AllDeploymentsFailed) as ei:
        _run(["a", "b"], call)
    assert len(ei.value.attempts) == 2
    assert calls == ["a", "b"]


def test_terminal_raise_when_last_deployment_non_retryable():
    # single deployment, auth error, no siblings -> RAISE
    call, calls = _scripted_call({"a": [FakeHTTPError(401)]})
    with pytest.raises(AllDeploymentsFailed):
        _run(["a"], call)
    assert calls == ["a"]


def test_max_fallbacks_bounds_attempts():
    chain = ["a", "b", "c", "d", "e"]
    call, calls = _scripted_call({k: [FakeHTTPError(400)] for k in chain})
    with pytest.raises(AllDeploymentsFailed) as ei:
        _run(chain, call, max_fallbacks=3)
    assert len(ei.value.attempts) == 3
    assert calls == ["a", "b", "c"]


def test_retry_after_header_used_for_backoff_on_last_deployment():
    call, calls = _scripted_call({"a": [FakeHTTPError(429, retry_after=12), "ok"]})
    sleep, slept = _recording_sleep()
    res = _run(["a"], call, num_retries=2, sleep=sleep, rng=lambda: 0.0)
    assert res.result == "ok"
    assert slept == [pytest.approx(12.0)]  # honored Retry-After, no siblings


def test_empty_chain_raises_value_error():
    call, _ = _scripted_call({})
    with pytest.raises(ValueError):
        _run([], call)
