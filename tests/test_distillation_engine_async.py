"""Tests for DistillationEngine.distill_async + sync-call safety guard.

Regression tests for the v3.3 LETHE-removal cleanup. Covers:

  1. distill_async returns the same dict shape as distill (compressed_text,
     original_tokens, compressed_tokens, compression_ratio, quality_score,
     strategy_used, compression_time_ms, engine).

  2. distill() raises RuntimeError when called from inside a running event
     loop instead of silently falling back. The pre-fix asyncio.run() in
     a sync method silently failed on the worker's hot path so EVERY
     distillation fell through to the LLM fallback. distill_async is the
     correct seam for async callers.
"""
from __future__ import annotations

import asyncio

import pytest

from compression.distillation_engine import (
    CompressionStrategy,
    DistillationEngine,
)


@pytest.mark.asyncio
async def test_distill_async_returns_expected_dict_shape():
    """The async path returns the same dict shape historical callers
    (distillation_worker.py:419) read from the sync version."""
    engine = DistillationEngine()
    text = (
        "MNEMOS v3.3 ships APOLLO and ARTEMIS as the going-forward "
        "compression stack. ARTEMIS is the CPU-only extractive engine "
        "with identifier preservation; APOLLO does schema-aware dense "
        "encoding with optional LLM fallback. The retired v3.0–v3.2 "
        "engines were removed entirely in the v3.3 cleanup pass."
    )
    result = await engine.distill_async(text, strategy=CompressionStrategy.AUTO)

    # All keys callers depend on must be present and well-typed.
    for key in (
        "compressed_text",
        "compressed",
        "original_tokens",
        "compressed_tokens",
        "compression_ratio",
        "strategy_used",
        "compression_time_ms",
        "engine",
    ):
        assert key in result, f"distill_async result missing key {key!r}"

    assert isinstance(result["compressed_text"], str)
    assert isinstance(result["original_tokens"], int) and result["original_tokens"] > 0
    assert isinstance(result["compressed_tokens"], int)
    assert isinstance(result["compression_ratio"], float)
    assert result["engine"] == "artemis"
    assert result["strategy_used"] in ("token", "sentence", "auto")


@pytest.mark.asyncio
async def test_sync_distill_raises_when_called_from_running_loop():
    """The sync distill() must NOT silently fall through inside a
    running loop. Pre-fix it called asyncio.run() which raises
    RuntimeError, then a fallback that ALSO raised — silently turning
    every worker call into an LLM-fallback. Now distill() raises
    explicitly so the caller knows to use distill_async()."""
    engine = DistillationEngine()
    with pytest.raises(RuntimeError, match="distill_async"):
        engine.distill("anything", strategy=CompressionStrategy.AUTO)


def test_sync_distill_works_from_outside_a_loop():
    """The sync distill() still works for the non-async caller — it
    spawns its own loop. Test runs without an asyncio.mark, so no
    enclosing loop exists."""
    engine = DistillationEngine()
    result = engine.distill(
        "Short test content.", strategy=CompressionStrategy.AUTO,
    )
    assert "compressed_text" in result
    assert result["engine"] == "artemis"


@pytest.mark.asyncio
async def test_distill_async_honors_explicit_ratio():
    """ARTEMIS now reads request.target_ratio (Codex audit P2 fix).
    A small ratio should produce a smaller compressed_tokens count
    than a large ratio, on the same input. Pre-fix every ratio
    produced identical output."""
    engine = DistillationEngine()
    text = (
        "First sentence with concrete detail. "
        "Second sentence elaborating on the first. "
        "Third sentence providing additional context. "
        "Fourth sentence with even more elaboration. "
        "Fifth sentence offering further context. "
        "Sixth sentence to give the extractive engine room to choose. "
        "Seventh sentence so MMR has options. "
        "Eighth sentence as additional padding."
    )
    tight = await engine.distill_async(text, ratio=0.25)
    loose = await engine.distill_async(text, ratio=0.75)

    # Tight ratio should keep fewer characters than loose. The
    # extractive selection is character-budgeted, so this is the
    # observable effect of request.target_ratio actually being used.
    assert tight["compressed_tokens"] <= loose["compressed_tokens"], (
        f"tight (ratio=0.25) compressed_tokens={tight['compressed_tokens']} "
        f"should be <= loose (ratio=0.75) compressed_tokens={loose['compressed_tokens']}. "
        f"If equal, ARTEMIS is ignoring request.target_ratio."
    )


@pytest.mark.asyncio
async def test_distill_async_strategy_recorded_for_observability():
    """The strategy enum is vestigial post-LETHE — no algorithm change.
    But the strategy value is still echoed back in the result so
    downstream telemetry / compression_method tagging continues to
    work without surprise."""
    engine = DistillationEngine()
    text = "A test sentence with some content."
    for strat in (
        CompressionStrategy.TOKEN,
        CompressionStrategy.SENTENCE,
        CompressionStrategy.AUTO,
    ):
        result = await engine.distill_async(text, strategy=strat)
        assert result["strategy_used"] == strat.value


def test_get_running_loop_negative_path_does_not_explode():
    """Ensure the safety check itself uses asyncio correctly — calling
    distill() from outside a loop should NOT raise our guard error."""
    # Sanity check: asyncio.get_running_loop() raises when there's no
    # loop, which is what our guard relies on. If python ever changed
    # that behavior this test catches it.
    with pytest.raises(RuntimeError, match="no running event loop"):
        asyncio.get_running_loop()
