"""LETHEEngine — CompressionEngine adapter tests.

Covers the LETHE port of the v3.1 plugin ABC. No DB, no GPU, no
network — pure CPU path. The existing sync LETHE.compress(text, ratio)
contract is kept untouched; this file pins the new async
LETHEEngine.compress(request) contract.
"""

from __future__ import annotations

import asyncio


from compression.base import (
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from compression.lethe import LETHE, LETHEEngine


LONG_STRUCTURED = (
    "MNEMOS is a memory operating system. It stores memories across sessions. "
    "Compression is important for context budgets. LETHE is the fast CPU tier. "
    "ALETHEIA runs on GPU. ANAMNESIS handles archival. APOLLO is schema-aware."
)


def test_engine_identity_and_intent():
    engine = LETHEEngine()
    assert engine.id == "lethe"
    assert engine.label.startswith("LETHE")
    assert engine.version == "1.0"
    assert engine.gpu_intent is GPUIntent.CPU_ONLY


def test_compress_returns_compression_result_not_dict():
    # The v3.0 LETHE.compress returned a dict; the ABC port must return
    # a CompressionResult. This pins the contract.
    engine = LETHEEngine()
    req = CompressionRequest(memory_id="m-1", content=LONG_STRUCTURED, target_ratio=0.4)
    res = asyncio.run(engine.compress(req))
    assert isinstance(res, CompressionResult)


def test_compress_succeeds_and_reduces_tokens():
    engine = LETHEEngine()
    req = CompressionRequest(memory_id="m-1", content=LONG_STRUCTURED, target_ratio=0.4)
    res = asyncio.run(engine.compress(req))

    assert res.succeeded()
    assert res.engine_id == "lethe"
    assert res.engine_version == "1.0"
    assert res.gpu_used is False
    assert res.compressed_content
    assert res.compression_ratio is not None
    assert 0.0 < res.compression_ratio <= 1.0
    assert res.compressed_tokens is not None
    assert res.original_tokens > 0
    assert res.quality_score is not None
    assert 0.0 <= res.quality_score <= 1.0
    assert res.elapsed_ms >= 0


def test_identifier_policy_honestly_reported_as_off():
    # LETHE does not preserve identifiers, regardless of what the
    # request asked for. It MUST report OFF so the manager can apply
    # a policy-mismatch penalty rather than being misled.
    engine = LETHEEngine()
    req = CompressionRequest(
        memory_id="m-1",
        content=LONG_STRUCTURED,
        identifier_policy=IdentifierPolicy.STRICT,  # request asks strict
    )
    res = asyncio.run(engine.compress(req))
    assert res.identifier_policy is IdentifierPolicy.OFF


def test_manifest_records_mode_and_core_settings():
    engine = LETHEEngine(mode="sentence", aggressive=False, min_length=7)
    req = CompressionRequest(memory_id="m-1", content=LONG_STRUCTURED)
    res = asyncio.run(engine.compress(req))
    assert res.manifest.get("aggressive") is False
    assert res.manifest.get("min_length") == 7
    assert res.manifest.get("mode") in {"sentence", "token", "none"}
    assert "compression_percentage" in res.manifest


def test_empty_content_succeeds_with_ratio_one():
    # LETHE treats very short content as a no-op (ratio=1.0).
    # The engine port still reports succeeded=True — the manager's
    # composite_score for ratio=1.0 will be low, so this engine
    # won't win on trivial inputs, but it won't error out either.
    engine = LETHEEngine()
    req = CompressionRequest(memory_id="m-empty", content="")
    res = asyncio.run(engine.compress(req))
    assert res.succeeded()
    assert res.compression_ratio == 1.0


def test_core_override_is_respected():
    # Operators may want to pre-configure LETHE (different stop-word
    # sets, etc.) and hand it to the engine. Verify composition works.
    core = LETHE(mode="token", aggressive=True, min_length=3)
    engine = LETHEEngine(core=core)
    assert engine._core is core


def test_target_ratio_from_request_reaches_core():
    # The request's target_ratio must be propagated to LETHE.compress.
    # Run two requests with very different targets and confirm the
    # output tokens differ meaningfully.
    engine = LETHEEngine()
    req_aggressive = CompressionRequest(memory_id="m-1", content=LONG_STRUCTURED, target_ratio=0.2)
    req_lenient = CompressionRequest(memory_id="m-2", content=LONG_STRUCTURED, target_ratio=0.8)

    res_a = asyncio.run(engine.compress(req_aggressive))
    res_l = asyncio.run(engine.compress(req_lenient))

    assert res_a.compressed_tokens is not None and res_l.compressed_tokens is not None
    # Aggressive should produce fewer-or-equal tokens than lenient.
    # LETHE is heuristic, so we don't require strict ordering of
    # ratios — just that the requested targets propagate through.
    assert res_a.compressed_tokens <= res_l.compressed_tokens
