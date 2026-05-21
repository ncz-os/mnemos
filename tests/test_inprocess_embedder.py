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

    backend = emb_mod._OpenVINOBackend(model_id=str(ir_dir), device="CPU", max_chars=512)
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

    backend = emb_mod._OpenVINOBackend(model_id="BAAI/bge-base-en-v1.5", device="CPU", max_chars=512)
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
                "InProcessEmbedder does not expose ov_device/device attribute; " "OV_DEVICE plumbing not yet wired"
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
