"""In-process embedder — pluggable backend (OpenVINO / llama-cpp-python).

Architectural decision (mem_1779334716543_f8ebd4, operator-locked 2026-05-21):
MNEMOS embedding generation is ALWAYS in-process. No HTTP dependency on
Ollama, no external llama-server, no /pantheon/v1/embeddings round-trip.

Backends:
  openvino   optimum-intel + OVModelForFeatureExtraction.
             Picks device from MNEMOS_EMBED_OV_DEVICE in order
             GPU > NPU > CPU when AUTO (default). Phase 1B (2026-05-21,
             mem_1779332109027_8b4402): Intel Xe iGPU 49.84 rec/s, Intel
             CPU 20.86 rec/s — both significantly faster than llama-cpp
             on the same hardware.
  llamacpp   llama-cpp-python with a local GGUF. Works anywhere with a
             gguf file. Phase 1B fleet results: CERBERUS RTX 4500 Ada
             CUDA 523.79 rec/s, TYPHON RTX 5060 CUDA 479.84, PYTHIA CPU
             11.06, PROTEUS CPU 13.65, m66 Cix Sky1 ARM CPU 12.20.

Backend selection:
  MNEMOS_EMBED_BACKEND=auto|openvino|llamacpp (default auto).
  auto tries openvino first (if optimum-intel + transformers importable
  + at least one OpenVINO device available), falls back to llamacpp.

Knobs:
  MNEMOS_EMBED_BACKEND       auto|openvino|llamacpp           default auto
  MNEMOS_EMBED_OV_MODEL_ID   HF model id for openvino path    default nomic-ai/nomic-embed-text-v1.5
  MNEMOS_EMBED_OV_DEVICE     OpenVINO device (AUTO|GPU|CPU|NPU)  default AUTO
  MNEMOS_EMBED_TRUST_REMOTE_CODE allow HF remote code for custom models default false
  MNEMOS_EMBED_MODEL_PATH    .gguf for llamacpp path          default /opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf
  MNEMOS_EMBED_N_CTX         llama_cpp n_ctx                  default 8192
  MNEMOS_EMBED_THREADS       llama_cpp n_threads              default os.cpu_count()
  MNEMOS_EMBED_GPU_LAYERS    llama_cpp n_gpu_layers           default 0
  MNEMOS_EMBED_MAX_CHARS     per-call input truncate          default 8000

The default local backends produce 768-dim vectors. Set
MNEMOS_EMBEDDING_DIM to match alternate embedding endpoints before the
schema is provisioned.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Iterable

from mnemos.core.config import (
    embed_backend_env,
    embed_cix_max_seq_len_env,
    embed_cix_model_path_env,
    embed_cix_tokenizer_id_env,
    embed_gpu_layers_env,
    embed_http_model_env,
    embed_http_timeout_env,
    embed_http_url_env,
    embed_http_url_fallback_env,
    embed_hybrid_env,
    embed_max_chars_env,
    embed_model_path_env,
    embed_n_ctx_env,
    embed_npu_threshold_chars_env,
    embed_ov_device_env,
    embed_ov_model_id_env,
    embed_threads_env,
    embed_trust_remote_code_env,
)

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "auto"
DEFAULT_CIX_MODEL_PATH = "/opt/mnemos/models/bge-small-zh-v1.5_256.cix"
DEFAULT_CIX_TOKENIZER_ID = "BAAI/bge-small-zh-v1.5"
DEFAULT_CIX_MAX_SEQ_LEN = 256
DEFAULT_HYBRID = False
DEFAULT_NPU_THRESHOLD_CHARS = 1000  # ~256 tokens at 4 chars/token
DEFAULT_MODEL_PATH = "/opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf"
# HTTP backend (mem_1779334716543_f8ebd4 EXCEPTION clause 2026-05-23):
# operator-authorized MEDUSA llama.cpp Vulkan endpoint at
# http://192.168.207.64:8090/v1/embeddings. Same memo forbids ollama or
# any other HTTP embed provider; MEDUSA is the sole exception. Must
# circuit-break + fall through to llamacpp on >30s outage.
DEFAULT_HTTP_URL = "http://192.168.207.61:8090/v1/embeddings"  # TYPHON RTX 5060 CUDA
DEFAULT_HTTP_URL_FALLBACK = "http://192.168.207.64:8090/v1/embeddings"  # MEDUSA AMD NAVI14 Vulkan
DEFAULT_HTTP_MODEL = "bge-m3"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_HTTP_CB_THRESHOLD = 5  # consecutive failures before opening breaker
DEFAULT_HTTP_CB_COOLDOWN = 30.0  # seconds breaker stays open before half-open probe
# bge-base-en-v1.5: 768-dim, standard BERT-base, fully OpenVINO-supported.
# Schema provisioning sizes the vector column from MNEMOS_EMBEDDING_DIM.
# Nomic uses a custom nomic_bert arch that optimum-intel's OV exporter
# does not natively support, so we use BGE for the OV path. Operators
# who need to preserve the nomic vector space should set
# MNEMOS_EMBED_BACKEND=llamacpp.
DEFAULT_OV_MODEL_ID = "BAAI/bge-base-en-v1.5"
DEFAULT_OV_DEVICE = "AUTO"
DEFAULT_N_CTX = 8192
DEFAULT_N_GPU_LAYERS = 0
DEFAULT_N_THREADS = max(1, os.cpu_count() or 4)
DEFAULT_MAX_TEXT_CHARS = 8000


class _OpenVINOBackend:
    """OpenVINO + optimum-intel embedding backend.

    Loads BERT-family model on first .embed() call. CLS-pooled
    last_hidden_state is L2-normalized to match the nomic convention.
    """

    def __init__(self, model_id: str, device: str, max_chars: int, trust_remote_code: bool) -> None:
        self.model_id = model_id
        self.device = device
        self.max_chars = max_chars
        self.trust_remote_code = trust_remote_code
        self._model = None
        self._tokenizer = None
        self._embed_dim: int | None = None
        self._resolved_device: str | None = None
        self._numpy = None  # type: ignore[assignment]

    def _load_sync(self) -> None:
        if self._model is not None:
            return
        import openvino as ov  # noqa: F401  (validate import early)
        from optimum.intel import OVModelForFeatureExtraction
        from transformers import AutoTokenizer
        import numpy as np

        core = ov.Core()
        available = core.available_devices
        if self.device == "AUTO":
            # Pick fastest available: GPU > NPU > CPU
            for cand in ("GPU", "NPU", "CPU"):
                if cand in available:
                    resolved = cand
                    break
            else:
                raise RuntimeError(f"No OpenVINO device available; saw {available}")
        else:
            if self.device not in available:
                raise RuntimeError(f"OpenVINO device {self.device!r} not in available {available}")
            resolved = self.device
        self._resolved_device = resolved
        logger.info(
            "[EMBED][ov] loading model_id=%s device=%s (available=%s)",
            self.model_id,
            resolved,
            available,
        )
        if self.trust_remote_code:
            logger.warning("[EMBED][ov] trusting remote model code for model_id=%s", self.model_id)
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id,
            trust_remote_code=self.trust_remote_code,
        )
        # Detect pre-exported local OpenVINO IR directory (export=False),
        # otherwise fall back to HF export path (export=True).
        _is_local_ir = os.path.isdir(self.model_id) and os.path.isfile(
            os.path.join(self.model_id, "openvino_model.xml")
        )
        self._model = OVModelForFeatureExtraction.from_pretrained(
            self.model_id,
            export=not _is_local_ir,
            device=resolved,
            trust_remote_code=self.trust_remote_code,
        )
        self._numpy = np
        # Warmup + dim probe
        warm = self._embed_sync("warmup")
        self._embed_dim = len(warm) if warm else None
        logger.info("[EMBED][ov] loaded; embed_dim=%s", self._embed_dim)

    def _embed_sync(self, text: str) -> list[float]:
        truncated = (text or "")[: self.max_chars]
        if not truncated.strip():
            return []
        np = self._numpy
        inputs = self._tokenizer(truncated, return_tensors="pt", truncation=True, max_length=512, padding=True)
        out = self._model(**inputs)
        last = out.last_hidden_state.detach().cpu().numpy()
        vec = last[0, 0, :]  # CLS pooling
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def embed_dim(self) -> int | None:
        return self._embed_dim

    @property
    def resolved_device(self) -> str | None:
        return self._resolved_device


class _LlamaCppBackend:
    """llama-cpp-python embedding backend (GGUF, CPU/CUDA)."""

    def __init__(
        self,
        model_path: str,
        n_ctx: int,
        n_threads: int,
        n_gpu_layers: int,
        max_chars: int,
    ) -> None:
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self.max_chars = max_chars
        self._llm = None
        self._embed_dim: int | None = None

    def _load_sync(self) -> None:
        if self._llm is not None:
            return
        path = Path(self.model_path)
        if not path.exists():
            raise FileNotFoundError(
                f"GGUF model not found at {self.model_path}. Set "
                "MNEMOS_EMBED_MODEL_PATH or bake the model into the image."
            )
        from llama_cpp import Llama

        logger.info(
            "[EMBED][llamacpp] loading model=%s n_ctx=%d threads=%d gpu_layers=%d",
            self.model_path,
            self.n_ctx,
            self.n_threads,
            self.n_gpu_layers,
        )
        self._llm = Llama(
            model_path=str(path),
            n_ctx=self.n_ctx,
            n_threads=self.n_threads,
            embedding=True,
            n_gpu_layers=self.n_gpu_layers,
            verbose=False,
        )
        warmup = self._llm.create_embedding("warmup")
        self._embed_dim = len(warmup["data"][0]["embedding"])
        logger.info("[EMBED][llamacpp] loaded; embed_dim=%d", self._embed_dim)

    def _embed_sync(self, text: str) -> list[float]:
        truncated = (text or "")[: self.max_chars]
        if not truncated.strip():
            return []
        r = self._llm.create_embedding(truncated)
        vec = list(r["data"][0]["embedding"])
        # L2-normalize to match the nomic/bge convention (see
        # _OpenVINOBackend / _CixNpuBackend, which already do this).
        # Without it, raw llama.cpp embeddings have arbitrary magnitude,
        # so a Euclidean distance computed against them is nowhere near
        # the [0, 2] range score_to_similarity()'s euclidean_unit metric
        # assumes for unit vectors -- every result silently clamps to a
        # similarity of 0.0 and gets floor-filtered, with no exception
        # anywhere in the stack (found 2026-07-10 debugging PEGASUS/
        # ACHILLES semantic search returning 0 rows despite embeddings
        # being generated and stored correctly).
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    @property
    def loaded(self) -> bool:
        return self._llm is not None

    @property
    def embed_dim(self) -> int | None:
        return self._embed_dim


class _CixNpuBackend:
    """Cix Sky1 Zhouyi V3 NPU backend via libnoe.

    Loads a precompiled .cix model on first .embed() call. The .cix file is
    a Compass NN Compiler IR + weights blob built from an ONNX source via
    the Arm-China Compass_MiniPkg toolchain (proprietary, x86 Linux only —
    see https://aijishu.com/a/1060000000215443). Available only on hosts
    that present /dev/aipu + libnoe Python wheel (cixmini Sky1).

    Model conventions follow Arm-China Compass Whisper / ai_model_hub:
    fixed input shape, INT8-quantized weights, tokenizer matches the ONNX
    source. Max sequence length is baked at compile time (default 256
    tokens for BGE-style embedders) — call sites that exceed this should
    fall back to the OpenVINO/llama-cpp backend (see hybrid routing).
    """

    def __init__(
        self,
        model_path: str,
        tokenizer_id: str,
        max_seq_len: int,
        max_chars: int,
        trust_remote_code: bool,
    ) -> None:
        self.model_path = model_path
        self.tokenizer_id = tokenizer_id
        self.max_seq_len = max_seq_len
        self.max_chars = max_chars
        self.trust_remote_code = trust_remote_code
        self._engine = None
        self._tokenizer = None
        self._embed_dim: int | None = None

    def _load_sync(self) -> None:
        if self._engine is not None:
            return
        path = Path(self.model_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Cix NPU .cix model not found at {self.model_path}. "
                "Compile via Compass_MiniPkg (Arm-China NN Compiler, x86 Linux) "
                "or set MNEMOS_EMBED_CIX_MODEL_PATH to a prebuilt .cix file."
            )
        if not Path("/dev/aipu").exists():
            raise RuntimeError(
                "Cix NPU device /dev/aipu not present. Driver not loaded or "
                "wrong kernel (LTS 6.18.26-cix-sky1-lts known good; NEXT 7.0.9 "
                "blocked by missing fwnode genpd patch — see "
                "mem_1779346076558_e5672e)."
            )
        from libnoe import NPU  # type: ignore[import-not-found]
        from transformers import AutoTokenizer

        logger.info(
            "[EMBED][cix-npu] loading model=%s tokenizer=%s max_seq=%d",
            self.model_path,
            self.tokenizer_id,
            self.max_seq_len,
        )
        if self.trust_remote_code:
            logger.warning("[EMBED][cix-npu] trusting remote tokenizer code for tokenizer=%s", self.tokenizer_id)
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.tokenizer_id,
            trust_remote_code=self.trust_remote_code,
        )
        self._engine = NPU()
        self._engine.load_graph(str(path))
        # The .cix advertises its output dim via libnoe descriptors; warmup
        # exercises the path + captures the dim for sanity checks.
        warm = self._embed_sync("warmup")
        self._embed_dim = len(warm) if warm else None
        logger.info("[EMBED][cix-npu] loaded; embed_dim=%s", self._embed_dim)

    def _embed_sync(self, text: str) -> list[float]:
        truncated = (text or "")[: self.max_chars]
        if not truncated.strip():
            return []
        import numpy as np

        enc = self._tokenizer(
            truncated,
            return_tensors="np",
            padding="max_length",
            truncation=True,
            max_length=self.max_seq_len,
        )
        inputs = [
            enc["input_ids"].astype(np.int32),
            enc["attention_mask"].astype(np.int32),
            enc["token_type_ids"].astype(np.int32),
        ]
        # libnoe returns a list of output tensors; convention from Arm-China
        # ai_model_hub inference_npu.py: the second tensor [1] is the
        # pooled embedding for BGE-family models.
        outputs = self._engine.forward(inputs)
        if len(outputs) > 1:
            vec = np.asarray(outputs[1]).reshape(-1)
        else:
            vec = np.asarray(outputs[0]).reshape(-1)
        # L2-normalize to match nomic/bge convention
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        return [float(x) for x in vec.tolist()]

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def embed_dim(self) -> int | None:
        return self._embed_dim


class _HttpBackend:
    """OpenAI-compatible /v1/embeddings HTTP backend.

    Operator-locked decision (mem_1779334716543_f8ebd4 EXCEPTION clause
    2026-05-23) restricts this backend to the MEDUSA llama.cpp Vulkan
    endpoint at http://192.168.207.64:8090/v1/embeddings (or another
    operator-vetted local-LAN llama-server). ollama at any host remains
    forbidden — the 2026-05-21 90-day silent-stall incident was caused
    by a stale podman-compose service alias and this backend MUST guard
    against the same failure mode.

    Defenses:
      * Per-call timeout (DEFAULT_HTTP_TIMEOUT, 30s).
      * Consecutive-failure circuit breaker (DEFAULT_HTTP_CB_THRESHOLD,
        5 failures → open for DEFAULT_HTTP_CB_COOLDOWN, 30s).
      * Caller-side fall-through: HybridHttpEmbedder wraps this with an
        in-process llama-cpp fallback so embedder.embed_text never
        returns silently-stale.
      * Per-call latency + status logging so /v1/admin/embed_stats can
        surface degradation within minutes (vs the original 90 days).
    """

    def __init__(
        self,
        url: str,
        model: str,
        timeout: float,
        max_chars: int,
        cb_threshold: int = DEFAULT_HTTP_CB_THRESHOLD,
        cb_cooldown: float = DEFAULT_HTTP_CB_COOLDOWN,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self.max_chars = max_chars
        self._cb_threshold = cb_threshold
        self._cb_cooldown = cb_cooldown
        self._consecutive_failures = 0
        self._breaker_opened_at: float | None = None
        self._embed_dim: int | None = None
        self._client = None  # lazy httpx.AsyncClient

    def _build_client_sync(self) -> None:
        if self._client is not None:
            return
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout)
        logger.info(
            "[EMBED][http] backend ready: url=%s model=%s timeout=%.1fs",
            self.url,
            self.model,
            self.timeout,
        )

    def _load_sync(self) -> None:
        # No model file to load; just instantiate the client. Dim is
        # discovered on the first successful embed call.
        self._build_client_sync()

    @property
    def loaded(self) -> bool:
        return self._client is not None

    @property
    def embed_dim(self) -> int | None:
        return self._embed_dim

    def _breaker_open(self) -> bool:
        """Return True iff breaker is currently open (block calls)."""
        if self._breaker_opened_at is None:
            return False
        import time as _t

        if _t.monotonic() - self._breaker_opened_at >= self._cb_cooldown:
            # Cooldown elapsed; half-open: allow next call to probe.
            self._breaker_opened_at = None
            self._consecutive_failures = 0
            logger.info("[EMBED][http] circuit breaker half-open; probing %s", self.url)
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_opened_at = None

    def _record_failure(self) -> None:
        import time as _t

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._cb_threshold and self._breaker_opened_at is None:
            self._breaker_opened_at = _t.monotonic()
            logger.warning(
                "[EMBED][http] circuit breaker OPEN after %d consecutive failures; cooling down %.1fs (url=%s)",
                self._consecutive_failures,
                self._cb_cooldown,
                self.url,
            )

    async def embed_async(self, text: str) -> list[float]:
        """Async embed; respects breaker. Returns [] on failure (caller falls
        through to the wrapping HybridHttpEmbedder's local backend)."""
        if not text or not text.strip():
            return []
        if self._client is None:
            self._build_client_sync()
        if self._breaker_open():
            return []
        truncated = text[: self.max_chars]
        import time as _t

        t0 = _t.monotonic()
        try:
            r = await self._client.post(
                self.url,
                json={"model": self.model, "input": truncated},
            )
            dt_ms = (_t.monotonic() - t0) * 1000.0
            if r.status_code != 200:
                logger.warning(
                    "[EMBED][http] status=%d latency_ms=%.1f url=%s",
                    r.status_code,
                    dt_ms,
                    self.url,
                )
                self._record_failure()
                return []
            data = r.json()
            vec = data["data"][0]["embedding"]
            if self._embed_dim is None:
                self._embed_dim = len(vec)
                logger.info("[EMBED][http] embed_dim=%d (discovered)", self._embed_dim)
            self._record_success()
            logger.debug("[EMBED][http] ok latency_ms=%.1f dim=%d", dt_ms, len(vec))
            return list(vec)
        except Exception as exc:
            dt_ms = (_t.monotonic() - t0) * 1000.0
            logger.warning(
                "[EMBED][http] exception=%s latency_ms=%.1f url=%s",
                type(exc).__name__,
                dt_ms,
                self.url,
            )
            self._record_failure()
            return []

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Batch embed; one POST per call with `input` as a list. Returns
        per-text [] on partial failure modes (length mismatch)."""
        cleaned = [(t or "")[: self.max_chars] for t in texts]
        nonempty = [c for c in cleaned if c.strip()]
        if not nonempty:
            return [[] for _ in texts]
        if self._client is None:
            self._build_client_sync()
        if self._breaker_open():
            return [[] for _ in texts]
        import time as _t

        t0 = _t.monotonic()
        try:
            r = await self._client.post(
                self.url,
                json={"model": self.model, "input": cleaned},
            )
            dt_ms = (_t.monotonic() - t0) * 1000.0
            if r.status_code != 200:
                logger.warning(
                    "[EMBED][http] batch status=%d count=%d latency_ms=%.1f",
                    r.status_code,
                    len(cleaned),
                    dt_ms,
                )
                self._record_failure()
                return [[] for _ in texts]
            data = r.json()
            vecs = [list(d["embedding"]) for d in data["data"]]
            if self._embed_dim is None and vecs:
                self._embed_dim = len(vecs[0])
                logger.info("[EMBED][http] embed_dim=%d (discovered, batch)", self._embed_dim)
            self._record_success()
            logger.info(
                "[EMBED][http] batch ok count=%d latency_ms=%.1f rate=%.1f emb/s",
                len(vecs),
                dt_ms,
                len(vecs) / max(0.001, dt_ms / 1000.0),
            )
            return vecs
        except Exception as exc:
            dt_ms = (_t.monotonic() - t0) * 1000.0
            logger.warning(
                "[EMBED][http] batch exception=%s count=%d latency_ms=%.1f",
                type(exc).__name__,
                len(cleaned),
                dt_ms,
            )
            self._record_failure()
            return [[] for _ in texts]


def _cix_npu_available() -> bool:
    """Return True if Cix NPU is usable on this host (device + library + model)."""
    if not Path("/dev/aipu").exists():
        return False
    try:
        import libnoe  # noqa: F401
    except Exception:
        return False
    model_path = embed_cix_model_path_env()
    if not Path(model_path).exists():
        return False
    return True


def _select_backend(requested: str) -> tuple[str, str]:
    """Resolve backend choice. Returns (backend_name, reason).

    auto-detect priority (highest accelerator wins, with fallback):
      1. cix-npu  — /dev/aipu + libnoe + .cix model present (Sky1 only)
      2. openvino — optimum-intel + transformers + any OV device (Intel CPU/iGPU/NPU)
      3. llamacpp — GGUF + llama-cpp-python (portable CPU/CUDA)

    When MNEMOS_EMBED_HYBRID=true, cix-npu + (openvino|llamacpp) wrap into a
    HybridEmbedder that routes short inputs (<= MNEMOS_EMBED_NPU_THRESHOLD_CHARS,
    default 1000) to the NPU and longer ones to the fallback backend, so the NPU's
    fixed max-seq-len doesn't silently truncate.
    """
    if requested != "auto":
        return requested, f"explicit MNEMOS_EMBED_BACKEND={requested}"

    if _cix_npu_available():
        return "cix-npu", "/dev/aipu + libnoe + .cix model present"

    try:
        import openvino as ov  # noqa: F401
        from optimum.intel import OVModelForFeatureExtraction  # noqa: F401
        from transformers import AutoTokenizer  # noqa: F401

        core = ov.Core()
        if core.available_devices:
            return "openvino", f"openvino devices available: {core.available_devices}"
    except Exception as exc:
        logger.debug("[EMBED] openvino unavailable (%s); falling through", exc)
    return "llamacpp", "openvino + cix-npu unavailable; using llama-cpp-python"


class InProcessEmbedder:
    """Process-resident embedder with pluggable backend (openvino|llamacpp).

    Lazy-loads on first .embed() call so the import is cheap. The
    underlying backend is NOT reentrant in either implementation
    (llama_cpp.Llama nor optimum-intel forward), so all calls serialize
    through an asyncio.Lock and run on the default executor.

    The ``backend`` argument (or MNEMOS_EMBED_BACKEND env) picks the
    runtime: ``openvino`` for optimum-intel, ``llamacpp`` for GGUF, or
    ``auto`` to try openvino first and fall back to llamacpp.
    """

    def __init__(
        self,
        backend: str | None = None,
        # openvino knobs
        ov_model_id: str | None = None,
        ov_device: str | None = None,
        # llamacpp knobs
        model_path: str | None = None,
        n_ctx: int | None = None,
        n_threads: int | None = None,
        n_gpu_layers: int | None = None,
        # cix-npu knobs
        cix_model_path: str | None = None,
        cix_tokenizer_id: str | None = None,
        cix_max_seq_len: int | None = None,
        # http knobs (mem_1779334716543_f8ebd4 EXCEPTION clause 2026-05-23)
        http_url: str | None = None,
        http_url_fallback: str | None = None,
        http_model: str | None = None,
        http_timeout: float | None = None,
        # hybrid knobs
        hybrid: bool | None = None,
        npu_threshold_chars: int | None = None,
        # shared
        max_text_chars: int | None = None,
        trust_remote_code: bool | None = None,
    ) -> None:
        requested = backend or embed_backend_env()
        self.backend_choice = requested

        self.ov_model_id = ov_model_id or embed_ov_model_id_env()
        self.ov_device = (ov_device or embed_ov_device_env()).upper()
        self.trust_remote_code = trust_remote_code if trust_remote_code is not None else embed_trust_remote_code_env()

        self.model_path = model_path or embed_model_path_env()
        self.cix_model_path = cix_model_path or embed_cix_model_path_env()
        self.cix_tokenizer_id = cix_tokenizer_id or embed_cix_tokenizer_id_env()
        self.cix_max_seq_len = int(cix_max_seq_len if cix_max_seq_len is not None else embed_cix_max_seq_len_env())
        hybrid_env = embed_hybrid_env()
        self.hybrid = hybrid if hybrid is not None else hybrid_env.lower() in ("1", "true", "yes")
        self.npu_threshold_chars = int(
            npu_threshold_chars if npu_threshold_chars is not None else embed_npu_threshold_chars_env()
        )
        self.n_ctx = int(n_ctx if n_ctx is not None else embed_n_ctx_env())
        self.n_threads = int(n_threads if n_threads is not None else embed_threads_env())
        self.n_gpu_layers = int(n_gpu_layers if n_gpu_layers is not None else embed_gpu_layers_env())
        self.http_url = http_url or embed_http_url_env()
        # Fallback URL: empty string disables; default = MEDUSA so the primary
        # TYPHON outage path falls to MEDUSA before in-process llamacpp.
        self.http_url_fallback = http_url_fallback if http_url_fallback is not None else embed_http_url_fallback_env()
        self.http_model = http_model or embed_http_model_env()
        self.http_timeout = float(http_timeout if http_timeout is not None else embed_http_timeout_env())
        self.max_text_chars = int(max_text_chars if max_text_chars is not None else embed_max_chars_env())
        self._backend = None  # type: ignore[assignment]
        self._backend_name: str | None = None
        # Hybrid sidecar: when self.hybrid + cix-npu available, route short
        # texts to NPU and long to the primary backend.
        self._npu_sidecar: _CixNpuBackend | None = None
        self._lock = asyncio.Lock()

    def _make_backend(self, name: str):
        if name == "openvino":
            return _OpenVINOBackend(
                model_id=self.ov_model_id,
                device=self.ov_device,
                max_chars=self.max_text_chars,
                trust_remote_code=self.trust_remote_code,
            )
        if name == "cix-npu":
            return _CixNpuBackend(
                model_path=self.cix_model_path,
                tokenizer_id=self.cix_tokenizer_id,
                max_seq_len=self.cix_max_seq_len,
                max_chars=self.max_text_chars,
                trust_remote_code=self.trust_remote_code,
            )
        if name == "http":
            return _HttpBackend(
                url=self.http_url,
                model=self.http_model,
                timeout=self.http_timeout,
                max_chars=self.max_text_chars,
            )
        return _LlamaCppBackend(
            model_path=self.model_path,
            n_ctx=self.n_ctx,
            n_threads=self.n_threads,
            n_gpu_layers=self.n_gpu_layers,
            max_chars=self.max_text_chars,
        )

    def _build_backend(self) -> None:
        if self._backend is not None:
            return
        resolved, reason = _select_backend(self.backend_choice)
        logger.info("[EMBED] backend=%s (%s)", resolved, reason)
        self._backend = self._make_backend(resolved)
        self._backend_name = resolved
        # Hybrid: if primary != cix-npu but NPU is available, attach a sidecar
        # for short-input routing. If primary IS cix-npu, no sidecar — caller
        # explicitly asked for NPU-only.
        if self.hybrid and resolved != "cix-npu" and _cix_npu_available():
            logger.info(
                "[EMBED] hybrid mode: NPU sidecar active, threshold=%d chars",
                self.npu_threshold_chars,
            )
            self._npu_sidecar = _CixNpuBackend(
                model_path=self.cix_model_path,
                tokenizer_id=self.cix_tokenizer_id,
                max_seq_len=self.cix_max_seq_len,
                max_chars=self.max_text_chars,
                trust_remote_code=self.trust_remote_code,
            )
        # HTTP fallback chain (mem_1779334716543_f8ebd4 EXCEPTION clause):
        # the http backend falls through PRIMARY -> REMOTE-SECONDARY -> in-
        # process llamacpp when the remote endpoint is unreachable. Each
        # fallback is constructed lazily so missing GGUF on hosts that
        # intentionally have only http available doesn't fail startup.
        self._http_fallback_remote: _HttpBackend | None = None
        self._http_fallback: _LlamaCppBackend | None = None
        if resolved == "http":
            if self.http_url_fallback and self.http_url_fallback != self.http_url:
                self._http_fallback_remote = _HttpBackend(
                    url=self.http_url_fallback,
                    model=self.http_model,
                    timeout=self.http_timeout,
                    max_chars=self.max_text_chars,
                )
                logger.info(
                    "[EMBED] http remote-fallback prepared: %s",
                    self.http_url_fallback,
                )
            try:
                if Path(self.model_path).exists():
                    self._http_fallback = _LlamaCppBackend(
                        model_path=self.model_path,
                        n_ctx=self.n_ctx,
                        n_threads=self.n_threads,
                        n_gpu_layers=self.n_gpu_layers,
                        max_chars=self.max_text_chars,
                    )
                    logger.info("[EMBED] http local-fallback prepared: %s", self.model_path)
                else:
                    logger.warning(
                        "[EMBED] http backend has NO local fallback (%s missing); "
                        "remote-fallback %s will be tried, then [] embeddings",
                        self.model_path,
                        self.http_url_fallback or "(none configured)",
                    )
            except Exception:
                logger.exception("[EMBED] http backend local-fallback prep failed")

    def _load_sync(self) -> None:
        self._build_backend()
        self._backend._load_sync()

    @property
    def loaded(self) -> bool:
        return self._backend is not None and self._backend.loaded

    @property
    def embed_dim(self) -> int | None:
        return self._backend.embed_dim if self._backend else None

    @property
    def backend_name(self) -> str | None:
        return self._backend_name

    async def _ensure_loaded(self) -> None:
        if not (self._backend and self._backend.loaded):
            await asyncio.get_running_loop().run_in_executor(None, self._load_sync)
        if self._npu_sidecar and not self._npu_sidecar.loaded:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._npu_sidecar._load_sync)
            except Exception:
                logger.warning("[EMBED] hybrid NPU sidecar failed to load; falling back to primary backend")
                self._npu_sidecar = None

    async def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns [] on error or empty input.

        In hybrid mode with a working NPU sidecar, inputs shorter than
        npu_threshold_chars are routed to the NPU (Cix Sky1 fixed-shape
        graph at max_seq_len=256 tokens, low-latency). Longer inputs go
        to the primary backend (OV / llama-cpp) which has flexible
        max_length. This avoids silent truncation on the NPU's fixed shape.

        For the http backend, the call is async (httpx) — no executor
        thread. On empty result the optional local fallback is invoked
        per mem_1779334716543_f8ebd4 EXCEPTION clause.
        """
        truncated = (text or "")[: self.max_text_chars]
        if not truncated.strip():
            return []
        async with self._lock:
            try:
                await self._ensure_loaded()
                if isinstance(self._backend, _HttpBackend):
                    vec = await self._backend.embed_async(truncated)
                    if vec:
                        return vec
                    # Empty -> breaker open or remote failed; try remote-fallback first.
                    if self._http_fallback_remote is not None:
                        logger.warning(
                            "[EMBED][http] primary failed -> remote-fallback %s",
                            self._http_fallback_remote.url,
                        )
                        vec = await self._http_fallback_remote.embed_async(truncated)
                        if vec:
                            return vec
                    # Then in-process local llamacpp.
                    if self._http_fallback is not None:
                        if not self._http_fallback.loaded:
                            await asyncio.get_running_loop().run_in_executor(None, self._http_fallback._load_sync)
                        logger.warning("[EMBED][http] remote+fallback failed -> llamacpp local")
                        return await asyncio.get_running_loop().run_in_executor(
                            None, self._http_fallback._embed_sync, truncated
                        )
                    return []
                use_npu = (
                    self._npu_sidecar is not None
                    and self._npu_sidecar.loaded
                    and len(truncated) <= self.npu_threshold_chars
                )
                backend = self._npu_sidecar if use_npu else self._backend
                return await asyncio.get_running_loop().run_in_executor(None, backend._embed_sync, truncated)
            except Exception:
                logger.exception("[EMBED] failed to embed text len=%d", len(truncated))
                # On hybrid NPU failure, retry on primary backend
                if self._npu_sidecar and self._backend:
                    try:
                        return await asyncio.get_running_loop().run_in_executor(
                            None, self._backend._embed_sync, truncated
                        )
                    except Exception:
                        logger.exception("[EMBED] primary backend retry also failed")
                return []

    async def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed multiple strings. For the http backend, sends one POST
        with input=list[str] (MEDUSA llama.cpp handles batch). For
        local backends, falls back to per-text sequential calls since
        neither in-process backend is reentrant.
        """
        texts_list = list(texts)
        if isinstance(self._backend, _HttpBackend) or self.backend_choice == "http":
            async with self._lock:
                try:
                    await self._ensure_loaded()
                    if isinstance(self._backend, _HttpBackend):
                        vecs = await self._backend.embed_batch_async(texts_list)
                        # Remote-fallback first for failed rows (MEDUSA :8090
                        # when TYPHON primary failed). Batch only the failed
                        # subset to keep wire payload tight.
                        if any(not v for v in vecs) and self._http_fallback_remote is not None:
                            miss_idx = [i for i, v in enumerate(vecs) if not v]
                            miss_texts = [texts_list[i] for i in miss_idx]
                            logger.warning(
                                "[EMBED][http] batch primary missed %d/%d; remote-fallback %s",
                                len(miss_idx),
                                len(vecs),
                                self._http_fallback_remote.url,
                            )
                            remote_vecs = await self._http_fallback_remote.embed_batch_async(miss_texts)
                            for j, i in enumerate(miss_idx):
                                if remote_vecs[j]:
                                    vecs[i] = remote_vecs[j]
                        # Local fallback for any rows still empty.
                        if any(not v for v in vecs) and self._http_fallback is not None:
                            if not self._http_fallback.loaded:
                                await asyncio.get_running_loop().run_in_executor(None, self._http_fallback._load_sync)
                            for i, (v, t) in enumerate(zip(vecs, texts_list)):
                                if not v and t and t.strip():
                                    logger.warning("[EMBED][http] batch local-fallback idx=%d", i)
                                    vecs[i] = await asyncio.get_running_loop().run_in_executor(
                                        None, self._http_fallback._embed_sync, t[: self.max_text_chars]
                                    )
                        return vecs
                except Exception:
                    logger.exception("[EMBED] http batch failed; per-text fallback")
        return [await self.embed(t) for t in texts_list]


_embedder: InProcessEmbedder | None = None


def get_embedder() -> InProcessEmbedder:
    """Return the process-wide embedder singleton. Lazy-constructs on first call."""
    global _embedder
    if _embedder is None:
        _embedder = InProcessEmbedder()
    return _embedder


async def embed_text(text: str) -> list[float]:
    """Convenience: embed via the process-wide singleton."""
    return await get_embedder().embed(text)


def reset_embedder() -> None:
    """Drop the process-wide singleton. Test-helper; not for production use."""
    global _embedder
    _embedder = None
