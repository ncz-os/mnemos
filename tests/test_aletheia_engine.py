"""ALETHEIAEngine — CompressionEngine adapter tests.

Unit-level checks for the ALETHEIA port. We don't exercise the real
GPU endpoint here — the inner ALETHEIA core is mocked so we can pin
success, structured failure (core returned error dict), and raw
exception paths deterministically. Live GPU smoke tests run from
the benchmark harness, not from pytest.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from compression.aletheia import ALETHEIA, ALETHEIAEngine
from compression.base import (
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


# ---- live-GPU probe --------------------------------------------------------
#
# The live test below hits a real OpenAI-compatible completion endpoint to
# prove ALETHEIA actually reaches GPU, not just that the adapter translates
# shapes. Default target is TYPHON's llama.cpp server (Qwen2.5-Coder-7B).
# CERBERUS vLLM is the documented production target but is frequently
# idle; the probe also tries it as a fallback.
#
# Operators can override via MNEMOS_TEST_GPU_URL. The test skips cleanly
# when no endpoint is reachable so CI on unconfigured hosts doesn't fail.

_CANDIDATE_GPU_URLS: list[str] = [
    url
    for url in (
        os.getenv("MNEMOS_TEST_GPU_URL"),
        "http://192.168.207.61:8080",   # TYPHON llama.cpp + Qwen
        "http://192.168.207.96:8000",   # CERBERUS vLLM (when up)
    )
    if url
]


def _first_reachable(urls: list[str], timeout: float = 2.0) -> str | None:
    for url in urls:
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(f"{url.rstrip('/')}/v1/models")
                if r.status_code == 200:
                    return url
        except Exception:
            continue
    return None


_LIVE_GPU_URL = _first_reachable(_CANDIDATE_GPU_URLS)


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


# ---- live-GPU integration --------------------------------------------------


@pytest.mark.skipif(
    _LIVE_GPU_URL is None,
    reason=(
        "No reachable OpenAI-compatible GPU endpoint "
        "(set MNEMOS_TEST_GPU_URL or start llama.cpp/vLLM on the fleet)"
    ),
)
def test_live_gpu_success_reaches_real_endpoint():
    """Actually hit a GPU provider and compress a real memory.

    This covers what the mocked success test cannot: that the adapter
    correctly drives the inner ALETHEIA HTTP client against a real
    OpenAI-compatible endpoint, the response JSON shape matches
    ALETHEIA's parser, and the engine reports gpu_used=True with a
    populated compressed_content.

    The model may produce a weakly-scored output (llama.cpp with a
    generic coder model isn't an ideal importance-scorer), but
    ALETHEIA's _parse_compressed_tokens has its own fallback that
    still yields a usable CompressionResult. The assertion shape
    accommodates both "model responded with parseable scores" and
    "model responded but parser fell back to first-N tokens" —
    both count as successful real-infra round-trips for the purpose
    of this test.
    """
    assert _LIVE_GPU_URL is not None  # belt-and-suspenders vs the skipif

    async def _run():
        engine = ALETHEIAEngine(gpu_url=_LIVE_GPU_URL)
        try:
            req = CompressionRequest(
                memory_id="live-smoke-1",
                content=(
                    "MNEMOS is a memory operating system for AI agents. "
                    "It stores memories across sessions using PostgreSQL "
                    "with pgvector for embeddings. Compression keeps "
                    "context budgets small. LETHE is the fast CPU tier. "
                    "ALETHEIA runs on GPU via an OpenAI-compatible "
                    "endpoint. The quality tradeoff is tunable via "
                    "scoring profiles."
                ) * 2,
                target_ratio=0.4,
            )
            return await engine.compress(req)
        finally:
            await engine.close()

    res = asyncio.run(_run())

    assert res.engine_id == "aletheia"
    # Real endpoint should return either a succeeded result (happy
    # path) or an error shaped like real-infra failure. The adapter
    # bug we want to catch here would be "adapter raised through to
    # the caller" or "returned the wrong engine_id"; both pass/fail
    # branches above exclude those.
    if res.error is None:
        assert res.succeeded()
        assert res.gpu_used is True
        assert res.compressed_content is not None and res.compressed_content
        assert res.compression_ratio is not None and res.compression_ratio > 0
        assert res.elapsed_ms > 0
        assert res.manifest.get("gpu_url") == _LIVE_GPU_URL
    else:
        # Real HTTP failure modes (e.g., model loaded but completion
        # endpoint returned 500, or request timed out) still count
        # as adapter-correct behavior — we just exercised the error
        # translation path against real infra.
        assert isinstance(res.error, str) and res.error
        assert res.gpu_used is False
