"""ANAMNESISEngine — CompressionEngine adapter tests.

Mock-based checks for the ANAMNESIS port of the v3.1 plugin ABC. The
inner core's extract_facts call is mocked so we can pin every branch
(full extraction / summary-only / error dict / empty / raw exception)
deterministically. A live-GPU success test runs against TYPHON
llama.cpp (skipped when unreachable), mirroring the
tests/test_aletheia_engine.py pattern.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from compression.anamnesis import (
    ANAMNESIS,
    ANAMNESISEngine,
    _render_extraction,
    _self_score,
)
from compression.base import (
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


_LONG_CONTENT = (
    "MNEMOS is a memory operating system for AI agents. It stores "
    "memories across sessions using PostgreSQL with pgvector. "
    "Compression runs through LETHE, ALETHEIA, and ANAMNESIS tiers."
) * 2


def _mock_core(core_output: dict, gpu_url: str = "http://gpu.local:8000") -> MagicMock:
    core = MagicMock(spec=ANAMNESIS)
    core.gpu_url = gpu_url
    core.extract_facts = AsyncMock(return_value=core_output)
    core.close = AsyncMock()
    return core


def _extraction(
    *,
    facts: list[str] | None = None,
    entities: list[str] | None = None,
    concepts: list[str] | None = None,
    summary: str = "MNEMOS is a memory OS.",
    error: str | None = None,
) -> dict:
    return {
        "facts": facts if facts is not None else ["Memories stored in PostgreSQL.", "Three-tier compression."],
        "entities": entities if entities is not None else ["MNEMOS", "PostgreSQL"],
        "concepts": concepts if concepts is not None else ["memory", "compression"],
        "summary": summary,
        "extraction_method": "anamnesis",
        "error": error,
    }


# ---- identity --------------------------------------------------------------


def test_engine_identity_and_intent():
    engine = ANAMNESISEngine(core=_mock_core(_extraction()))
    assert engine.id == "anamnesis"
    assert engine.version == "1.0"
    assert engine.gpu_intent is GPUIntent.GPU_REQUIRED
    assert "ANAMNESIS" in engine.label


# ---- success path ----------------------------------------------------------


def test_full_extraction_produces_rendered_content_and_manifest():
    engine = ANAMNESISEngine(core=_mock_core(_extraction()))
    req = CompressionRequest(memory_id="m-1", content=_LONG_CONTENT, task_type="solutions")
    res = asyncio.run(engine.compress(req))

    assert isinstance(res, CompressionResult)
    assert res.succeeded()
    assert res.engine_id == "anamnesis"
    assert res.gpu_used is True
    assert res.compression_ratio is not None
    assert res.quality_score == 0.85
    assert res.identifier_policy is IdentifierPolicy.OFF
    assert res.elapsed_ms >= 0

    # Rendered content is human-readable: summary line + bulleted facts
    assert "MNEMOS is a memory OS." in res.compressed_content
    assert "- Memories stored in PostgreSQL." in res.compressed_content
    assert "- Three-tier compression." in res.compressed_content

    # Manifest carries the structured extraction for indexing/audit
    assert res.manifest["category"] == "solutions"
    assert res.manifest["facts"] == _extraction()["facts"]
    assert res.manifest["entities"] == _extraction()["entities"]
    assert res.manifest["concepts"] == _extraction()["concepts"]
    assert res.manifest["gpu_url"] == "http://gpu.local:8000"


def test_summary_only_extraction_is_weak_win():
    # GPU replied but produced just a summary with no facts. succeeded()
    # is True (content is present) but quality is marked lower so the
    # contest correctly weighs this against richer outputs.
    engine = ANAMNESISEngine(core=_mock_core(_extraction(
        facts=[], entities=[], concepts=[], summary="Brief summary only."
    )))
    res = asyncio.run(engine.compress(CompressionRequest(memory_id="m", content=_LONG_CONTENT)))
    assert res.succeeded()
    assert res.quality_score == 0.50
    assert res.compressed_content == "Brief summary only."


# ---- category resolution ---------------------------------------------------


@pytest.mark.parametrize(
    "task_type, metadata, expected_category",
    [
        ("solutions", None, "solutions"),
        ("decisions", None, "decisions"),
        ("infrastructure", None, "infrastructure"),
        ("reasoning", None, "facts"),                                # unknown task_type -> default
        (None, {"category": "patterns"}, "patterns"),               # metadata fallback
        (None, {"category": "bogus"}, "facts"),                     # invalid metadata -> default
        (None, None, "facts"),                                       # nothing set -> default
        ("reasoning", {"category": "decisions"}, "decisions"),      # metadata wins when task_type invalid
    ],
)
def test_category_resolution(task_type, metadata, expected_category):
    core = _mock_core(_extraction())
    engine = ANAMNESISEngine(core=core)
    req = CompressionRequest(
        memory_id="m", content=_LONG_CONTENT,
        task_type=task_type, metadata=metadata or {},
    )
    asyncio.run(engine.compress(req))
    assert core.extract_facts.call_args.kwargs["category"] == expected_category


# ---- failure paths ---------------------------------------------------------


def test_core_error_dict_produces_error_result():
    engine = ANAMNESISEngine(core=_mock_core(_extraction(
        facts=[], entities=[], concepts=[], summary="trunc",
        error="Connection refused",
    )))
    res = asyncio.run(engine.compress(CompressionRequest(memory_id="m", content=_LONG_CONTENT)))
    assert not res.succeeded()
    assert res.error == "Connection refused"
    assert res.gpu_used is False
    assert res.engine_id == "anamnesis"
    # Manifest still carries the (empty) structured fields so the audit log shows the shape
    assert "category" in res.manifest
    assert "gpu_url" in res.manifest


def test_empty_extraction_demoted_to_error():
    # GPU responded 200 but parser recovered nothing. Without this
    # check the engine would report succeeded() with compressed_content=""
    # — a degenerate win that the ratio_term floor would reject, but
    # demoting to error at the adapter is cleaner and more honest.
    engine = ANAMNESISEngine(core=_mock_core(_extraction(
        facts=[], entities=[], concepts=[], summary="",
    )))
    res = asyncio.run(engine.compress(CompressionRequest(memory_id="m", content=_LONG_CONTENT)))
    assert not res.succeeded()
    assert res.error is not None
    assert "empty extraction" in res.error
    assert res.gpu_used is True  # we did reach GPU; it just produced nothing useful


def test_core_raising_exception_surfaces_as_error_result():
    core = MagicMock(spec=ANAMNESIS)
    core.gpu_url = "http://gpu:8000"
    core.extract_facts = AsyncMock(side_effect=RuntimeError("kaboom"))
    engine = ANAMNESISEngine(core=core)
    res = asyncio.run(engine.compress(CompressionRequest(memory_id="m", content=_LONG_CONTENT)))
    assert not res.succeeded()
    assert res.error is not None and "kaboom" in res.error
    assert res.gpu_used is False


# ---- pure-function helpers -------------------------------------------------


@pytest.mark.parametrize(
    "facts, entities, concepts, summary, expected",
    [
        (["f"], ["e"], ["c"], "s", 0.85),
        (["f"], ["e"], [], "s", 0.85),                                 # entities OR concepts sufficient
        (["f"], [], ["c"], "s", 0.85),
        (["f"], [], [], "s", 0.70),                                     # summary + facts, no meta
        ([], [], [], "s", 0.50),                                        # summary only
        ([], [], [], "", 0.30),                                         # degenerate
        (["  "], [""], [""], "s", 0.50),                               # whitespace treated as absent
    ],
)
def test_self_score(facts, entities, concepts, summary, expected):
    assert _self_score(facts, entities, concepts, summary) == expected


@pytest.mark.parametrize(
    "facts, summary, expected",
    [
        (["fact1", "fact2"], "Summary.", "Summary.\n\n- fact1\n- fact2"),
        ([], "Summary.", "Summary."),
        (["f"], "", "- f"),
        ([], "", ""),
        (["  spaced  "], "S", "S\n\n- spaced"),                       # trim facts
    ],
)
def test_render_extraction(facts, summary, expected):
    assert _render_extraction(facts, summary) == expected


# ---- live-GPU integration (skipped when unreachable) -----------------------


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


@pytest.mark.skipif(
    _LIVE_GPU_URL is None,
    reason="No reachable OpenAI-compatible GPU endpoint for live ANAMNESIS test",
)
def test_live_gpu_extraction_reaches_real_endpoint():
    """Hit a real GPU provider and run ANAMNESIS end-to-end.

    The model may or may not produce valid JSON — ANAMNESIS's parser
    has its own fallback. Both "succeeded" and "real-infra error"
    branches count as valid outcomes for this test; what matters is
    that the adapter correctly drives the HTTP client against real
    infra without raising.
    """
    assert _LIVE_GPU_URL is not None

    async def _run():
        engine = ANAMNESISEngine(gpu_url=_LIVE_GPU_URL)
        try:
            req = CompressionRequest(
                memory_id="anam-live-1",
                content=_LONG_CONTENT,
                task_type="solutions",
            )
            return await engine.compress(req)
        finally:
            await engine.close()

    res = asyncio.run(_run())
    assert res.engine_id == "anamnesis"
    if res.error is None:
        assert res.succeeded()
        assert res.gpu_used is True
        assert res.compressed_content
        assert res.elapsed_ms > 0
    else:
        assert isinstance(res.error, str) and res.error
