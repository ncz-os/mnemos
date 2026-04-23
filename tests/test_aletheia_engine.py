"""ALETHEIAEngine — CompressionEngine adapter tests.

Unit-level checks for the ALETHEIA port. We don't exercise the real
GPU endpoint here — the inner ALETHEIA core is mocked so we can pin
success, structured failure (core returned error dict), and raw
exception paths deterministically. Live GPU smoke tests run from
the benchmark harness, not from pytest.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from compression.aletheia import ALETHEIA, ALETHEIAEngine
from compression.base import (
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


def _request(content: str = "hello world " * 30, target_ratio: float = 0.4) -> CompressionRequest:
    return CompressionRequest(memory_id="mem-1", content=content, target_ratio=target_ratio)


def _mock_core(core_result: dict, gpu_url: str = "http://gpu.local:8000") -> MagicMock:
    core = MagicMock(spec=ALETHEIA)
    core.gpu_url = gpu_url
    core.compress = AsyncMock(return_value=core_result)
    core.close = AsyncMock()
    return core


# ---- identity ---------------------------------------------------------------


def test_engine_identity_and_intent():
    engine = ALETHEIAEngine(core=_mock_core({}))
    assert engine.id == "aletheia"
    assert engine.version == "1.0"
    assert engine.gpu_intent is GPUIntent.GPU_REQUIRED
    assert "ALETHEIA" in engine.label


def test_default_core_disables_fallback():
    # When no core is passed, the engine constructs one with
    # disable_fallback=True so a GPU outage doesn't shadow LETHE.
    engine = ALETHEIAEngine(gpu_url="http://example:8000")
    assert engine._core.disable_fallback is True


# ---- success path -----------------------------------------------------------


def test_successful_compress_populates_result_and_marks_gpu_used():
    core = _mock_core({
        "original_tokens": 100,
        "compressed_tokens": 30,
        "compression_ratio": 0.3,
        "compression_percentage": 70.0,
        "compressed_text": "x" * 30,
        "quality_score": 0.95,
        "method": "aletheia",
        "error": None,
    })
    engine = ALETHEIAEngine(core=core)
    res = asyncio.run(engine.compress(_request()))

    assert isinstance(res, CompressionResult)
    assert res.succeeded()
    assert res.engine_id == "aletheia"
    assert res.engine_version == "1.0"
    assert res.gpu_used is True
    assert res.compression_ratio == 0.3
    assert res.quality_score == 0.95
    assert res.compressed_tokens == 30
    assert res.compressed_content == "x" * 30
    assert res.identifier_policy is IdentifierPolicy.OFF
    assert res.manifest.get("method") == "aletheia"
    assert res.manifest.get("gpu_url") == "http://gpu.local:8000"


def test_request_target_ratio_propagated_to_core():
    core = _mock_core({
        "original_tokens": 100, "compressed_tokens": 30,
        "compression_ratio": 0.3, "compression_percentage": 70.0,
        "compressed_text": "x" * 30, "quality_score": 0.95,
        "method": "aletheia", "error": None,
    })
    engine = ALETHEIAEngine(core=core)
    asyncio.run(engine.compress(_request(target_ratio=0.25)))

    call_args = core.compress.call_args
    assert call_args.kwargs.get("target_ratio") == 0.25


# ---- structured failure path (core returned error dict) --------------------


def test_core_error_dict_produces_error_result_not_gpu_used():
    # The existing ALETHEIA returns a dict with error=str on failure
    # (when fallback is disabled). The engine must surface that as an
    # error CompressionResult with gpu_used=False so the contest
    # marks it reject_reason='error' without double-counting.
    core = _mock_core({
        "original_tokens": 100,
        "compressed_tokens": 100,
        "compression_ratio": 1.0,
        "compression_percentage": 0.0,
        "compressed_text": "hello world " * 30,
        "quality_score": 0.5,
        "method": "aletheia",
        "error": "Connection refused",
    })
    engine = ALETHEIAEngine(core=core)
    res = asyncio.run(engine.compress(_request()))

    assert not res.succeeded()
    assert res.error == "Connection refused"
    assert res.engine_id == "aletheia"
    assert res.gpu_used is False
    assert res.compressed_content is None
    assert res.manifest.get("method") == "aletheia"
    assert res.manifest.get("gpu_url") == "http://gpu.local:8000"


# ---- raw exception path ----------------------------------------------------


def test_core_raising_exception_surfaces_as_error_result():
    # If the core raises (e.g., client-level crash) rather than
    # returning an error dict, the engine must still return a
    # CompressionResult and not propagate — run_contest relies on
    # compress() never raising through to the gather.
    core = MagicMock(spec=ALETHEIA)
    core.gpu_url = "http://gpu.local:8000"
    core.compress = AsyncMock(side_effect=RuntimeError("kaboom"))

    engine = ALETHEIAEngine(core=core)
    res = asyncio.run(engine.compress(_request()))

    assert not res.succeeded()
    assert res.error is not None
    assert "kaboom" in res.error
    assert res.gpu_used is False
    assert res.engine_id == "aletheia"


# ---- timing + policy --------------------------------------------------------


def test_elapsed_ms_populated():
    core = _mock_core({
        "original_tokens": 100, "compressed_tokens": 30,
        "compression_ratio": 0.3, "compression_percentage": 70.0,
        "compressed_text": "x" * 30, "quality_score": 0.95,
        "method": "aletheia", "error": None,
    })
    engine = ALETHEIAEngine(core=core)
    res = asyncio.run(engine.compress(_request()))
    assert res.elapsed_ms >= 0


def test_identifier_policy_always_off_even_when_request_wants_strict():
    core = _mock_core({
        "original_tokens": 100, "compressed_tokens": 30,
        "compression_ratio": 0.3, "compression_percentage": 70.0,
        "compressed_text": "x" * 30, "quality_score": 0.95,
        "method": "aletheia", "error": None,
    })
    engine = ALETHEIAEngine(core=core)
    req = CompressionRequest(
        memory_id="m", content="x" * 200, identifier_policy=IdentifierPolicy.STRICT,
    )
    res = asyncio.run(engine.compress(req))
    assert res.identifier_policy is IdentifierPolicy.OFF


def test_close_delegates_to_core():
    core = _mock_core({})
    engine = ALETHEIAEngine(core=core)
    asyncio.run(engine.close())
    core.close.assert_awaited_once()
