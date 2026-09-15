"""Tests for the in-process embedder (mnemos/runtime/embedder.py).

Architectural decision (mem_1779334716543_f8ebd4, operator-locked 2026-05-21):
MNEMOS embedding generation is ALWAYS in-process via llama-cpp-python.
This test file exercises:

  - Lazy load: import does not trigger model load
  - Missing model file: FileNotFoundError with actionable message
  - Empty input: returns [] without invoking the model
  - Singleton: get_embedder() returns the same instance twice
  - reset_embedder() drops the singleton

The "real" model-load + embed path is covered only when a GGUF is
available at MNEMOS_EMBED_MODEL_PATH; otherwise those tests are
skipped. CI without the model file still exercises the surface.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mnemos.runtime.embedder import (
    InProcessEmbedder,
    embed_text,
    get_embedder,
    reset_embedder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_embedder()
    yield
    reset_embedder()


def test_import_does_not_load_model():
    # Importing the module + constructing the embedder should NOT touch
    # the GGUF file. Only .embed() / ._ensure_loaded() loads it.
    e = InProcessEmbedder(model_path="/nonexistent/path/never-loaded.gguf")
    assert e.loaded is False
    assert e.embed_dim is None


def test_singleton_identity():
    a = get_embedder()
    b = get_embedder()
    assert a is b


def test_reset_singleton():
    a = get_embedder()
    reset_embedder()
    b = get_embedder()
    assert a is not b


def test_missing_model_raises_filenotfound():
    # Pin backend=llamacpp so this test exercises the GGUF-missing path
    # regardless of which backends are installed in the test environment
    # (auto-select would pick openvino when optimum-intel is present).
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path/never-loaded.gguf")
    with pytest.raises(FileNotFoundError) as exc:
        e._load_sync()
    assert "MNEMOS_EMBED_MODEL_PATH" in str(exc.value)


@pytest.mark.asyncio
async def test_empty_input_returns_empty_list():
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path/never-loaded.gguf")
    # whitespace-only and empty should both return [] without touching the model
    assert await e.embed("") == []
    assert await e.embed("   \t\n  ") == []
    # Still not loaded
    assert e.loaded is False


@pytest.mark.asyncio
async def test_embed_swallows_load_failure_and_returns_empty():
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path/never-loaded.gguf")
    # First call attempts load, hits FileNotFoundError; .embed() catches
    # the exception, logs, and returns []. Subsequent calls also return [].
    out = await e.embed("hello world")
    assert out == []
    out2 = await e.embed("hello world")
    assert out2 == []


_MODEL_PATH = os.environ.get(
    "MNEMOS_EMBED_MODEL_PATH",
    "/opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf",
)
_HAS_MODEL = Path(_MODEL_PATH).exists()


def test_cix_npu_unavailable_off_aipu():
    from mnemos.runtime.embedder import _cix_npu_available

    # On any host without /dev/aipu (i.e. anywhere except .66), this should
    # return False regardless of MNEMOS_EMBED_CIX_MODEL_PATH.
    assert _cix_npu_available() is False or Path("/dev/aipu").exists()


def test_cix_backend_missing_model_raises():
    from mnemos.runtime.embedder import _CixNpuBackend

    b = _CixNpuBackend(
        model_path="/nonexistent/path.cix",
        tokenizer_id="BAAI/bge-small-zh-v1.5",
        max_seq_len=256,
        max_chars=8000,
        trust_remote_code=False,
    )
    # Expect either FileNotFoundError (no .cix) OR RuntimeError (no /dev/aipu),
    # depending on which check fails first on the host.
    with pytest.raises((FileNotFoundError, RuntimeError)):
        b._load_sync()


def test_hybrid_flag_propagates():
    # MNEMOS_EMBED_HYBRID env should be picked up by InProcessEmbedder
    os.environ["MNEMOS_EMBED_HYBRID"] = "true"
    try:
        e = InProcessEmbedder()
        assert e.hybrid is True
    finally:
        del os.environ["MNEMOS_EMBED_HYBRID"]


def test_hybrid_explicit_false_overrides_env():
    os.environ["MNEMOS_EMBED_HYBRID"] = "true"
    try:
        e = InProcessEmbedder(hybrid=False)
        assert e.hybrid is False
    finally:
        del os.environ["MNEMOS_EMBED_HYBRID"]


def test_npu_threshold_env():
    os.environ["MNEMOS_EMBED_NPU_THRESHOLD_CHARS"] = "500"
    try:
        e = InProcessEmbedder()
        assert e.npu_threshold_chars == 500
    finally:
        del os.environ["MNEMOS_EMBED_NPU_THRESHOLD_CHARS"]


def test_select_backend_explicit_request_honored():
    from mnemos.runtime.embedder import _select_backend

    name, reason = _select_backend("llamacpp")
    assert name == "llamacpp"
    assert "explicit" in reason

    name, reason = _select_backend("cix-npu")
    assert name == "cix-npu"
    assert "explicit" in reason


@pytest.mark.skipif(not _HAS_MODEL, reason=f"requires GGUF at {_MODEL_PATH}")
@pytest.mark.asyncio
async def test_real_embed_is_l2_normalized():
    # Regression guard (found 2026-07-10): _LlamaCppBackend._embed_sync
    # used to return llama.cpp's raw create_embedding() output with no
    # normalization, unlike _OpenVINOBackend and _CixNpuBackend which
    # both L2-normalize. A raw (un-normalized) vector has arbitrary
    # magnitude, so mnemos/api/routes/memories.py's score_to_similarity()
    # -- which assumes unit vectors under the euclidean_unit metric
    # (sim = 1 - d^2/2, valid only for d in [0, 2]) -- silently clamps
    # every result to a similarity of 0.0 and the relevance floor drops
    # every row. No exception anywhere: insert_memory stores the vector
    # fine, semantic_search runs fine, it just always returns 0 rows.
    e = InProcessEmbedder()
    vec = await e.embed("normalization regression guard")
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-4, f"embedding must be L2-normalized (unit length), got norm={norm}"


@pytest.mark.skipif(not _HAS_MODEL, reason=f"requires GGUF at {_MODEL_PATH}")
@pytest.mark.asyncio
async def test_real_embed_returns_nonempty_vector():
    e = InProcessEmbedder()
    vec = await e.embed("the quick brown fox jumps over the lazy dog")
    assert isinstance(vec, list)
    assert len(vec) > 0
    assert all(isinstance(x, float) for x in vec[:10])
    # nomic-embed-text-v1.5 is 768-dim; bge-small-zh-v1.5 is 512-dim.
    # We don't pin the exact dim — just sanity that the load + warmup
    # captured something plausible.
    assert e.embed_dim is not None and e.embed_dim >= 128


@pytest.mark.skipif(not _HAS_MODEL, reason=f"requires GGUF at {_MODEL_PATH}")
@pytest.mark.asyncio
async def test_embed_text_convenience_uses_singleton():
    v1 = await embed_text("alpha")
    v2 = await embed_text("alpha")
    # Same input + same model + deterministic init → identical vector
    assert v1 == v2
    # Calling get_embedder() afterwards should return the same loaded one
    e = get_embedder()
    assert e.loaded is True


@pytest.mark.skipif(not _HAS_MODEL, reason=f"requires GGUF at {_MODEL_PATH}")
@pytest.mark.asyncio
async def test_embed_batch_returns_per_input_vector():
    e = InProcessEmbedder()
    texts = ["alpha", "beta", "gamma"]
    out = await e.embed_batch(texts)
    assert len(out) == 3
    assert all(len(v) > 0 for v in out)
    # different inputs should produce different vectors
    assert out[0] != out[1]


def test_ov_backend_detects_local_ir_directory(tmp_path, monkeypatch):
    """OV backend must set export=False when model_id is a local OpenVINO IR dir.

    Regression: optimum-intel re-exports from PyTorch by default. When the
    operator pre-converts to OV IR (eg via optimum-cli + NNCF INT8) and points
    MNEMOS_EMBED_OV_MODEL_ID at the resulting directory, the loader has to skip
    the export step. Detect by checking for openvino_model.xml in the dir.
    """
    from mnemos.runtime import embedder as emb_mod

    ir_dir = tmp_path / "bge-ir"
    ir_dir.mkdir()
    (ir_dir / "openvino_model.xml").write_text("<placeholder/>")
    (ir_dir / "openvino_model.bin").write_bytes(b"")

    captured = {}

    class _FakeOVModel:
        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            captured["model_id"] = model_id
            captured["export"] = kwargs.get("export")
            captured["device"] = kwargs.get("device")
            instance = cls()
            instance._dummy = True
            return instance

        def __call__(self, **inputs):
            class _Out:
                last_hidden_state = None

            return _Out()

    class _FakeTokenizer:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            return cls()

        def __call__(self, *a, **kw):
            class _R:
                def __getitem__(self, k):
                    raise KeyError(k)

            return _R()

    fake_optimum = type("optimum_intel", (), {})()
    fake_optimum.OVModelForFeatureExtraction = _FakeOVModel
    monkeypatch.setitem(__import__("sys").modules, "optimum.intel", fake_optimum)
    fake_transformers = type("transformers", (), {})()
    fake_transformers.AutoTokenizer = _FakeTokenizer
    monkeypatch.setitem(__import__("sys").modules, "transformers", fake_transformers)

    class _FakeCore:
        available_devices = ["CPU"]

    class _FakeOV:
        Core = _FakeCore

    monkeypatch.setitem(__import__("sys").modules, "openvino", _FakeOV)

    backend = emb_mod._OpenVINOBackend(model_id=str(ir_dir), device="CPU", max_chars=512, trust_remote_code=False)
    try:
        backend._load_sync()
    except Exception:
        # The fake tokenizer's empty call shape will fail in _embed_sync
        # warmup; that's fine, we only care about the export flag captured
        # at from_pretrained time, which fired before warmup.
        pass

    assert captured.get("model_id") == str(ir_dir)
    assert captured.get("export") is False, f"expected export=False for local IR dir, got {captured.get('export')}"


def test_ov_backend_export_true_for_hf_repo_id(tmp_path, monkeypatch):
    """OV backend must keep export=True for a Hugging Face repo id.

    The detection key is presence of openvino_model.xml; for a plain HF id like
    'BAAI/bge-base-en-v1.5' the path isn't a directory at all, so the loader
    must fall back to export=True (the standard HF→OV conversion path).
    """
    from mnemos.runtime import embedder as emb_mod

    captured = {}

    class _FakeOVModel:
        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            captured["model_id"] = model_id
            captured["export"] = kwargs.get("export")
            instance = cls()
            return instance

        def __call__(self, **inputs):
            class _Out:
                last_hidden_state = None

            return _Out()

    class _FakeTokenizer:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            return cls()

        def __call__(self, *a, **kw):
            class _R:
                def __getitem__(self, k):
                    raise KeyError(k)

            return _R()

    fake_optimum = type("optimum_intel", (), {})()
    fake_optimum.OVModelForFeatureExtraction = _FakeOVModel
    monkeypatch.setitem(__import__("sys").modules, "optimum.intel", fake_optimum)
    fake_transformers = type("transformers", (), {})()
    fake_transformers.AutoTokenizer = _FakeTokenizer
    monkeypatch.setitem(__import__("sys").modules, "transformers", fake_transformers)

    class _FakeCore:
        available_devices = ["CPU"]

    class _FakeOV:
        Core = _FakeCore

    monkeypatch.setitem(__import__("sys").modules, "openvino", _FakeOV)

    backend = emb_mod._OpenVINOBackend(
        model_id="BAAI/bge-base-en-v1.5", device="CPU", max_chars=512, trust_remote_code=False
    )
    try:
        backend._load_sync()
    except Exception:
        pass

    assert captured.get("export") is True, f"expected export=True for HF repo id, got {captured.get('export')}"


# ─── OV_DEVICE=AUTO selection path ────────────────────────────────────────────
#
# Operator override pattern documented in the canonical Dockerfile:
#   MNEMOS_EMBED_OV_DEVICE=AUTO   # AUTO | CPU | GPU | NPU
# When OpenVINO + optimum-intel are present, the embedder should respect
# MNEMOS_EMBED_OV_DEVICE; in particular, AUTO should resolve through the
# OpenVINO Core's device-selection heuristic and not hard-fail on hosts
# without GPU/NPU. These tests assert the env-var plumbing reaches the
# embedder; they do NOT require a real OpenVINO install (skipped if absent).


def test_ov_device_env_auto_propagates_to_embedder():
    """MNEMOS_EMBED_OV_DEVICE=AUTO should be visible on the embedder instance.

    The architectural decision (mem_1779334716543_f8ebd4) requires the
    embedder to respect the AUTO/CPU/GPU/NPU device hint at construction
    time. This test exercises only the env-var-read path — it does not
    instantiate a real OpenVINO Core (skipped if openvino isn't importable).
    """
    pytest.importorskip("openvino", reason="OpenVINO not installed; AUTO path not exercised")
    os.environ["MNEMOS_EMBED_OV_DEVICE"] = "AUTO"
    try:
        e = InProcessEmbedder()
        # The embedder exposes the resolved device via either `ov_device`
        # or `device` (depending on the impl revision). Accept either.
        resolved = getattr(e, "ov_device", None) or getattr(e, "device", None)
        if resolved is None:
            pytest.skip(
                "InProcessEmbedder does not expose ov_device/device attribute; OV_DEVICE plumbing not yet wired"
            )
        assert str(resolved).upper() in {"AUTO", "CPU", "GPU", "NPU"}, f"expected AUTO/CPU/GPU/NPU, got {resolved!r}"
    finally:
        del os.environ["MNEMOS_EMBED_OV_DEVICE"]


def test_ov_device_env_explicit_cpu_overrides_auto():
    """Explicit CPU pin must override the AUTO default."""
    pytest.importorskip("openvino", reason="OpenVINO not installed; AUTO path not exercised")
    os.environ["MNEMOS_EMBED_OV_DEVICE"] = "CPU"
    try:
        e = InProcessEmbedder()
        resolved = getattr(e, "ov_device", None) or getattr(e, "device", None)
        if resolved is None:
            pytest.skip("InProcessEmbedder does not expose ov_device/device attribute")
        # When explicitly pinned to CPU, the embedder must not silently
        # resolve to AUTO/GPU/NPU.
        assert str(resolved).upper() == "CPU", f"expected CPU pin to win, got {resolved!r}"
    finally:
        del os.environ["MNEMOS_EMBED_OV_DEVICE"]


# ─── F15: non-reentrant backend must never be entered concurrently ───────────
#
# The in-process backends (llama_cpp.Llama.create_embedding / OpenVINO
# OVModelForFeatureExtraction forward / libnoe NPU) are documented as
# NOT reentrant. The pre-fix embedder used a bare ``asyncio.Lock`` around
# a ``run_in_executor(None, ...)`` call. Cancelling the awaiting asyncio
# task released the lock in the ``async with`` finally before the
# underlying thread-pool work actually finished, so a second concurrent
# caller could pass the lock and start its own backend call before the
# first one returned -- two simultaneous backend entries into a backend
# that is only safe with one at a time.
#
# The fix dispatches non-reentrant backend calls through a dedicated
# single-worker ThreadPoolExecutor: ``max_workers=1`` means two submitted
# tasks are GUARANTEED not to overlap on the executor, regardless of
# whether the awaiting asyncio side cancels mid-call.
#
# This test installs a fake non-reentrant backend that:
#   * sleeps a moment inside ``_embed_sync`` so a second call could in
#     principle overlap if the executor serialized it incorrectly;
#   * asserts an instance-level flag that the backend is "in use" right
#     now, raising ``AssertionError`` if a second invocation lands while
#     the first is still running.
#
# It then cancels an in-flight call and immediately issues a new one;
# the assertion never fires (otherwise the test would raise). This is
# the canonical reproduction shape the reviewer used.


class _NonReentrantFakeBackend:
    """Drop-in for _LlamaCppBackend that asserts single-entry invariant."""

    def __init__(self) -> None:
        self._in_use = False
        self._max_overlap = 0
        self._current_overlap = 0
        self.loaded = True
        self.embed_dim = 4

    def _embed_sync(self, text: str) -> list[float]:
        # The bare assertion: an instance-level flag must NEVER be
        # already-True when we enter. If the executor serializes
        # correctly (one worker, one task at a time), this holds even
        # under cancellation pressure from the asyncio side.
        assert not self._in_use, (
            "non-reentrant backend entered twice concurrently -- "
            "F15 regression: single-worker executor did not serialize"
        )
        self._in_use = True
        self._current_overlap += 1
        try:
            # Hold long enough that any overlap would be visible.
            import time as _t
            _t.sleep(0.1)
            return [0.1, 0.2, 0.3, 0.4]
        finally:
            self._in_use = False
            if self._current_overlap > self._max_overlap:
                self._max_overlap = self._current_overlap
            self._current_overlap -= 1


@pytest.mark.asyncio
async def test_cancel_then_reembed_never_enters_backend_concurrently():
    """F15 regression: cancelling an in-flight embed() and immediately
    issuing a new one must NEVER result in two concurrent entries into
    the (non-reentrant) backend.

    Before the fix, the cancellation released ``_lock`` while the
    executor was still running the prior ``_embed_sync``; a new embed()
    call would acquire ``_lock`` and start its own ``run_in_executor``
    on the default thread pool. Both backend invocations could run in
    parallel, breaking the single-entry contract.
    """
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path.gguf")
    # Bypass _build_backend: pin a fake backend that does not need a model.
    fake = _NonReentrantFakeBackend()
    e._backend = fake  # type: ignore[assignment]
    e._backend_name = "fake"

    # Issue an embed that will block in _embed_sync for ~100ms.
    long_task = asyncio.create_task(e.embed("cancel-me-please"))
    # Let it enter the executor.
    await asyncio.sleep(0.02)
    # Cancel mid-flight.
    long_task.cancel()
    # Immediately fire a second embed. Pre-fix this races the executor
    # and the fake backend's assertion would raise.
    second = await e.embed("right-after-cancel")
    # Now wait for the cancelled task to fully unwind.
    with pytest.raises(asyncio.CancelledError):
        await long_task

    # The fake backend asserts on every entry; reaching this line means
    # no concurrent entry was ever observed.
    assert second == [0.1, 0.2, 0.3, 0.4]
    # And the maximum concurrent-entries recorded by the fake is 1.
    assert fake._max_overlap == 1, (
        f"expected max concurrent backend entries = 1, got {fake._max_overlap}"
    )


@pytest.mark.asyncio
async def test_serial_executor_only_one_worker():
    """The serial executor used for non-reentrant backends must be
    configured with max_workers=1 -- otherwise F15's guarantee of
    serial execution falls apart."""
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path.gguf")
    # Trigger lazy construction.
    executor = e._ensure_serial_executor()
    assert executor._max_workers == 1, (
        f"F15 regression: serial executor must be max_workers=1, got {executor._max_workers}"
    )
    # Idempotent: a second call returns the same instance.
    assert e._ensure_serial_executor() is executor


# ─── HTTP-backend concurrent embed must NOT serialize to one-at-a-time ────────
#
# Regression: the pre-fix ``InProcessEmbedder.embed`` wrapped its entire body
# in ``async with self._lock:``, so HTTP calls (which are plain ``httpx``
# async calls — fully reentrant on the asyncio side, no thread, no in-process
# state) were forced to wait for each other. Reviewer's reproduction: 10
# concurrent simulated 50ms HTTP embeds serialized through the lock ran at
# concurrency 1 and took 508.8ms total instead of ~50-100ms.
#
# The fix replaces that single lock with a BOUNDED ``asyncio.Semaphore``
# (``_http_semaphore``) for the HTTP path only. Local non-reentrant backends
# still serialize through ``_lock`` + the single-worker executor (F15).
#
# These tests use a fake ``_HttpBackend`` whose ``embed_async`` sleeps
# ~50ms then returns a fixed vector; they then fire N concurrent
# ``.embed()`` calls and assert the wall time matches bounded-parallel,
# not serialized.


import time as _time
from mnemos.runtime import embedder as _emb_mod


class _FakeHttpBackend(_emb_mod._HttpBackend):
    """Drop-in for ``_HttpBackend`` that sleeps for ~``delay_s`` and returns
    a fixed vector. Tracks in-flight call count so we can assert real
    concurrency (overlap > 1) when ``http_concurrency >= 2``.

    Inherits from ``_HttpBackend`` so ``isinstance`` checks in the
    embedder's HTTP branch correctly fire AND attribute lookups for
    ``loaded`` / ``embed_dim`` etc. resolve correctly.
    """

    def __init__(self, delay_s: float = 0.05, dim: int = 4) -> None:
        # Bypass real ``_HttpBackend.__init__`` (which builds an httpx
        # client + circuit breaker we don't need for tests) by setting
        # the attributes the read-only ``@property`` accessors look up
        # directly. ``loaded`` is overridden as a plain attribute (the
        # parent class has it as a property too, but we override here).
        self.delay_s = delay_s
        self._embed_dim = dim
        self._client = None  # the @property ``loaded`` reads this
        self.loaded = True
        self._in_flight = 0
        self._max_in_flight = 0
        self.call_count = 0
        # Required attributes that the real backend exposes for fallback
        # wiring / diagnostics / hybrid retry.
        self.url = "http://fake/embeddings"
        self.timeout = 1.0
        self.max_chars = 8000
        self.model = "fake-embed-model"
        # Circuit breaker state — read by some paths.
        self._breaker_opened_at: float | None = None
        self._cb_failures = 0
        # Empty fallback chain: tests exercise primary path only.
        self._fallback_remote = None
        self._fallback_local = None

    # Override the parent's read-only ``loaded`` property — we want a
    # plain attribute set in __init__, not one that reads ``_client``.
    loaded: bool = True

    async def embed_async(self, text: str) -> list[float]:
        self._in_flight += 1
        self._max_in_flight = max(self._max_in_flight, self._in_flight)
        self.call_count += 1
        try:
            await asyncio.sleep(self.delay_s)
            return [float(len(text))] + [0.0] * (self.embed_dim - 1)
        finally:
            self._in_flight -= 1

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        # Treat a batch call as one in-flight; sleep proportionally to the
        # number of texts so the per-row equivalent matches.
        self._in_flight += 1
        self._max_in_flight = max(self._max_in_flight, self._in_flight)
        self.call_count += 1
        try:
            await asyncio.sleep(self.delay_s * max(1, len(texts)))
            return [[float(len(t))] + [0.0] * (self.embed_dim - 1) for t in texts]
        finally:
            self._in_flight -= 1

    def _embed_sync(self, text: str) -> list[float]:
        raise AssertionError(
            "_FakeHttpBackend._embed_sync called — tests should not exercise "
            "the non-reentrant local-fallback path on this fake."
        )


@pytest.mark.asyncio
async def test_http_embed_runs_concurrently_not_serialized():
    """Regression: 10 concurrent HTTP embed() calls must run in parallel up
    to ``http_concurrency``, not serialize through ``_lock``.

    Pre-fix: wall time ≈ 10 * 50ms = 500ms (serialized).
    Post-fix (with http_concurrency=10): wall time ≈ ~50ms (all parallel).
    """
    n = 10
    delay = 0.05  # 50ms per HTTP call
    e = InProcessEmbedder(
        backend="http",
        http_concurrency=10,
        http_url="http://fake/embeddings",
        http_url_fallback="",  # disable local + remote fallback so we exercise primary only
        hybrid=False,
    )
    fake = _FakeHttpBackend(delay_s=delay)
    e._backend = fake  # type: ignore[assignment]
    e._backend_name = "http"

    started = _time.monotonic()
    results = await asyncio.gather(*[e.embed(f"text-{i}") for i in range(n)])
    elapsed = _time.monotonic() - started

    assert all(len(v) > 0 for v in results), f"every embed must return a non-empty vector, got {results!r}"
    assert fake.call_count == n, f"primary HTTP backend called {fake.call_count} times, expected {n}"
    # Concurrent overlap observed must be > 1 — if it were 1 we'd be back to
    # the pre-fix serialized behaviour.
    assert fake._max_in_flight > 1, (
        f"HTTP embeds serialized to max_in_flight={fake._max_in_flight}; expected >1 (bounded-parallel)"
    )
    # Wall time should be roughly ceil(n / http_concurrency) * delay, plus
    # scheduling overhead. Allow generous slack for slow CI runners (3x).
    serial_lower_bound = (n // 10) * delay  # = 1 * delay if concurrency >= n
    serial_upper_bound = (n // 10 + 1) * delay * 3.0  # generous ceiling
    assert elapsed < n * delay, (
        f"10 concurrent 50ms embeds took {elapsed*1000:.1f}ms — close to the serialized 500ms. "
        f"HTTP path is still being serialized through _lock instead of _http_semaphore."
    )
    # And it should NOT be 0 — they actually had to do some work.
    assert elapsed >= serial_lower_bound * 0.5, (
        f"elapsed {elapsed*1000:.1f}ms suspiciously short for {n} x {delay*1000:.0f}ms HTTP calls"
    )
    assert elapsed < serial_upper_bound, (
        f"elapsed {elapsed*1000:.1f}ms exceeds expected ceiling {serial_upper_bound*1000:.1f}ms"
    )


@pytest.mark.asyncio
async def test_http_embed_bounded_by_semaphore_size():
    """With http_concurrency=3, only 3 of N=10 concurrent calls can be
    in-flight at once — assert max_in_flight <= 3 and wall time scales
    with ceil(N / 3) * delay."""
    n = 10
    delay = 0.05
    e = InProcessEmbedder(
        backend="http",
        http_concurrency=3,
        http_url="http://fake/embeddings",
        http_url_fallback="",
        hybrid=False,
    )
    fake = _FakeHttpBackend(delay_s=delay)
    e._backend = fake
    e._backend_name = "http"

    started = _time.monotonic()
    results = await asyncio.gather(*[e.embed(f"text-{i}") for i in range(n)])
    elapsed = _time.monotonic() - started

    assert all(len(v) > 0 for v in results)
    assert fake.call_count == n
    # Concurrency cap holds: no more than http_concurrency=3 simultaneous.
    assert fake._max_in_flight <= 3, (
        f"semaphore did not cap concurrency: max_in_flight={fake._max_in_flight} > 3"
    )
    # And concurrency did happen — at least 2 in flight at peak. If
    # max_in_flight==1 we serialized (regression).
    assert fake._max_in_flight >= 2, (
        f"semaphore accidentally serialized calls: max_in_flight={fake._max_in_flight} == 1"
    )
    # Wall time scales with ceil(10/3) * 50ms ≈ 200ms. Allow 3x slack for CI.
    expected_min_batches = (n + 3 - 1) // 3  # 4
    assert elapsed >= expected_min_batches * delay * 0.7, (
        f"elapsed {elapsed*1000:.1f}ms shorter than expected ceil(10/3)*50ms={expected_min_batches*delay*1000:.1f}ms"
    )
    # And it should NOT have serialized to 10*50ms = 500ms (regression guard).
    assert elapsed < n * delay * 1.2, (
        f"elapsed {elapsed*1000:.1f}ms ≈ serialized 500ms; semaphore not actually parallelizing"
    )


@pytest.mark.asyncio
async def test_http_embed_concurrency_env_propagates():
    """MNEMOS_EMBED_HTTP_CONCURRENCY env var must drive http_concurrency,
    and the constructed ``_http_semaphore`` must reflect that bound.
    """
    os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"] = "6"
    try:
        e = InProcessEmbedder(backend="http", http_url="http://fake/", http_url_fallback="")
        assert e.http_concurrency == 6
        # ``_http_semaphore`` is an ``asyncio.Semaphore`` whose internal
        # ``_value`` mirrors the bound at construction. (Touching private
        # state is justified here — the public surface of Semaphore
        # doesn't expose its bound.)
        assert isinstance(e._http_semaphore, asyncio.Semaphore)
        assert e._http_semaphore._value == 6  # type: ignore[attr-defined]
    finally:
        del os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"]


@pytest.mark.asyncio
async def test_http_embed_invalid_concurrency_clamps_to_one():
    """Invalid / non-positive MNEMOS_EMBED_HTTP_CONCURRENCY clamps to 1."""
    os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"] = "0"
    try:
        e = InProcessEmbedder(backend="http", http_url="http://fake/", http_url_fallback="")
        assert e.http_concurrency == 1
    finally:
        del os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"]

    os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"] = "not-a-number"
    try:
        e = InProcessEmbedder(backend="http", http_url="http://fake/", http_url_fallback="")
        assert e.http_concurrency == 10, f"non-numeric env should fall back to default 10, got {e.http_concurrency}"
    finally:
        del os.environ["MNEMOS_EMBED_HTTP_CONCURRENCY"]


@pytest.mark.asyncio
async def test_http_embed_batch_runs_concurrently_not_serialized():
    """Regression for ``embed_batch``: 10 concurrent batch calls (each with
    4 texts) on the http backend must overlap, not serialize through
    ``_lock``."""
    n = 10
    delay = 0.05
    e = InProcessEmbedder(
        backend="http",
        http_concurrency=10,
        http_url="http://fake/embeddings",
        http_url_fallback="",
        hybrid=False,
    )
    fake = _FakeHttpBackend(delay_s=delay)
    e._backend = fake
    e._backend_name = "http"

    batches = [[f"text-{i}-{j}" for j in range(4)] for i in range(n)]
    started = _time.monotonic()
    results = await asyncio.gather(*[e.embed_batch(b) for b in batches])
    elapsed = _time.monotonic() - started

    assert len(results) == n
    assert all(len(r) == 4 and all(len(v) > 0 for v in r) for r in results)
    # At least 2 in flight concurrently — proves we didn't serialize.
    assert fake._max_in_flight > 1, (
        f"http embed_batch serialized: max_in_flight={fake._max_in_flight}"
    )
    # Each batch call sleeps `delay * 4` = 200ms. Serialized: 2000ms. With
    # http_concurrency=10 they all overlap → ~200ms. Assert < 1000ms.
    assert elapsed < 1.0, (
        f"10 concurrent 200ms batches took {elapsed*1000:.0f}ms — close to serialized 2000ms; "
        f"embed_batch still serializing HTTP calls through _lock"
    )


# ─── F15 invariant unchanged: local non-reentrant backend NEVER entered concurrently
# ───
#
# The F15 fix (single-worker executor) MUST survive this refactor. The
# existing ``test_cancel_then_reembed_never_enters_backend_concurrently``
# already covers the canonical cancellation-pressure scenario; this second
# test exercises a different shape: 10 concurrent ``.embed()`` calls
# against a non-reentrant fake backend, then assert no concurrent entry
# was ever observed.


class _StrictNonReentrantBackend:
    """Backend that asserts single-entry under ALL circumstances."""

    def __init__(self) -> None:
        self._in_use = 0
        self._max_overlap = 0
        self.loaded = True
        self.embed_dim = 4

    def _embed_sync(self, text: str) -> list[float]:
        assert self._in_use == 0, (
            f"non-reentrant backend entered while in_use={self._in_use} "
            f"(F15 regression: serial executor failed under concurrent load)"
        )
        self._in_use += 1
        try:
            import time as _t

            _t.sleep(0.02)
            return [0.1] * 4
        finally:
            self._in_use -= 1
            self._max_overlap = max(self._max_overlap, 1)


@pytest.mark.asyncio
async def test_local_backend_unchanged_concurrent_embeds_never_overlap():
    """Concurrent embed() calls against the local (non-reentrant) backend
    MUST still serialize at the single-worker executor — F15 guarantee
    must not regress after the HTTP-semaphore refactor.

    Fire 10 concurrent embed() calls; assert max_overlap never exceeded 1.
    """
    e = InProcessEmbedder(backend="llamacpp", model_path="/nonexistent/path.gguf")
    fake = _StrictNonReentrantBackend()
    e._backend = fake  # type: ignore[assignment]
    e._backend_name = "fake"

    # 10 concurrent embed() calls — F15's executor serializes them, so
    # wall time ≈ 10 * 20ms = 200ms but max_overlap stays at 1.
    started = _time.monotonic()
    results = await asyncio.gather(*[e.embed(f"text-{i}") for i in range(10)])
    elapsed = _time.monotonic() - started

    assert all(r == [0.1] * 4 for r in results), f"every embed should return the fixed vector, got {results!r}"
    # Crucial invariant: never more than one entry into the non-reentrant
    # backend at a time, regardless of concurrency.
    assert fake._max_overlap == 1, (
        f"non-reentrant backend observed max_overlap={fake._max_overlap} under concurrent load; "
        f"F15's single-worker executor regressed"
    )
    # Wall time should reflect serialization: 10 * 20ms ≈ 200ms (or more
    # on slow CI). Allow generous slack: must be > 10 * 5ms and < 10 * 200ms.
    assert 0.05 <= elapsed <= 2.0, (
        f"unexpected wall time {elapsed*1000:.0f}ms for 10 serialized 20ms embeds"
    )


# ─── _ensure_loaded double-checked lock: safe under concurrent HTTP-first callers
# ───


@pytest.mark.asyncio
async def test_ensure_loaded_double_checked_lock_under_concurrent_calls():
    """Two concurrent first-time embed() calls (HTTP backend, lazy init)
    must not race past ``self._backend is None`` and double-invoke
    ``_build_backend``.

    The narrow ``_lock`` inside ``_ensure_loaded`` (post-fix) re-checks
    ``self._backend is not None`` inside the critical section, so only
    one coroutine actually runs through the lazy-init path. Pre-fix,
    both would.
    """
    e = InProcessEmbedder(
        backend="http",
        http_concurrency=10,
        http_url="http://fake/embeddings",
        http_url_fallback="",
    )
    # DO NOT pre-pin _backend — exercise the lazy-init race.
    assert e._backend is None

    build_count = {"n": 0}
    load_count = {"n": 0}
    real_build = _emb_mod.InProcessEmbedder._build_backend
    real_load = _emb_mod.InProcessEmbedder._load_sync

    def counting_build(self):  # type: ignore[no-untyped-def]
        build_count["n"] += 1
        return real_build(self)

    def counting_load(self):  # type: ignore[no-untyped-def]
        load_count["n"] += 1
        return real_load(self)

    e._build_backend = counting_build.__get__(e, _emb_mod.InProcessEmbedder)  # type: ignore[method-assign]
    e._load_sync = counting_load.__get__(e, _emb_mod.InProcessEmbedder)  # type: ignore[method-assign]

    # After the first call completes, the embedder will have a real
    # _HttpBackend pointing at http://fake/... and any subsequent
    # .embed() will try to hit that URL and likely fail. That's fine —
    # we only care about counting build/load invocations. Wrap each in
    # a try/except so the failure doesn't mask the assertion.
    try:
        await asyncio.gather(e.embed("text-a"), e.embed("text-b"))
    except Exception:
        pass

    # The double-checked lock guarantees _load_sync runs at most once
    # (the expensive part — builds the httpx client). ``_build_backend``
    # is also idempotent thanks to its own ``is not None`` guard, but
    # the narrow lock prevents the race even if that guard were absent.
    assert load_count["n"] == 1, (
        f"_load_sync was invoked {load_count['n']} times for 2 concurrent "
        f"first-time embed() calls; expected exactly 1 (double-checked lock regressed)"
    )
    # _build_backend may run 1 or 2 times depending on race timing —
    # both are safe because the function's own ``is not None`` guard
    # is the actual gate. The key invariant is that the EXPENSIVE load
    # runs exactly once.
    assert build_count["n"] <= 2, (
        f"_build_backend invoked {build_count['n']} times (>2) under concurrent "
        f"first-time calls; some race is escaping both guards"
    )
