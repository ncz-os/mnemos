"""Cross-encoder reranker HTTP client (v6.2 M-2.2.3).

Talks to llama.cpp `--rerank` endpoint (default: MEDUSA :8091 running
bge-reranker-v2-m3 on AMD NAVI14 Vulkan; see
docs/v6.2-nexus-pattern-adoption.md § Reranker service).

Circuit-breaker semantics mirror `mnemos/runtime/embedder.py::_HttpBackend`:
- on 5 consecutive failures, open breaker for 60s
- breaker-open returns ``[]`` so caller falls through to no-rerank
  (the un-reranked order is still a valid result set, never block
  search on a missing reranker — matches v6.2 acceptance criterion
  "failover to FTS-only ranking if MEDUSA :8091 down 30s+")
"""

from __future__ import annotations

import logging
import time as _t
from threading import Lock
from typing import Any

from mnemos.core.config import get_settings

logger = logging.getLogger(__name__)


# llama.cpp /v1/rerank shape:
#   {"model": "bge-reranker-v2-m3", "query": "...", "documents": ["...", ...]}
# Response:
#   {"results": [{"index": 0, "relevance_score": 0.87}, ...]}
DEFAULT_RERANKER_URL = "http://192.168.207.64:8091/v1/rerank"
DEFAULT_RERANKER_MODEL = "bge-reranker-v2-m3"
DEFAULT_RERANKER_TIMEOUT = 5.0
DEFAULT_CB_THRESHOLD = 5
DEFAULT_CB_COOLDOWN = 60.0


class Reranker:
    """HTTP client for cross-encoder reranker with breaker."""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        cb_threshold: int = DEFAULT_CB_THRESHOLD,
        cb_cooldown: float = DEFAULT_CB_COOLDOWN,
    ) -> None:
        settings = get_settings().providers
        self.url = url or settings.reranker_url
        self.model = model or settings.reranker_model
        env_timeout = settings.reranker_timeout_secs
        if timeout is not None:
            self.timeout = timeout
        elif env_timeout:
            try:
                self.timeout = float(env_timeout)
            except ValueError:
                self.timeout = DEFAULT_RERANKER_TIMEOUT
        else:
            self.timeout = DEFAULT_RERANKER_TIMEOUT
        self._cb_threshold = cb_threshold
        self._cb_cooldown = cb_cooldown
        self._consecutive_failures = 0
        self._breaker_opened_at: float | None = None
        self._client: Any = None

    def _build_client(self) -> None:
        if self._client is not None:
            return
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout)
        logger.info(
            "[RERANK] backend ready url=%s model=%s timeout=%.1fs",
            self.url,
            self.model,
            self.timeout,
        )

    def _breaker_open(self) -> bool:
        if self._breaker_opened_at is None:
            return False
        if _t.monotonic() - self._breaker_opened_at >= self._cb_cooldown:
            self._breaker_opened_at = None
            self._consecutive_failures = 0
            logger.info("[RERANK] breaker half-open; probing %s", self.url)
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_opened_at = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._cb_threshold and self._breaker_opened_at is None:
            self._breaker_opened_at = _t.monotonic()
            logger.warning(
                "[RERANK] breaker OPEN after %d consecutive failures; cooling down %.1fs url=%s",
                self._consecutive_failures,
                self._cb_cooldown,
                self.url,
            )

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[float]:
        """Return relevance scores aligned with ``documents`` order.

        On any error or open breaker, returns ``[]`` — caller MUST treat
        empty return as "rerank unavailable, keep original order".
        """
        if not query or not documents:
            return []
        if self._client is None:
            self._build_client()
        if self._breaker_open():
            return []
        t0 = _t.monotonic()
        try:
            r = await self._client.post(
                self.url,
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                },
            )
            dt_ms = (_t.monotonic() - t0) * 1000.0
            if r.status_code != 200:
                logger.warning(
                    "[RERANK] status=%d latency_ms=%.1f url=%s",
                    r.status_code,
                    dt_ms,
                    self.url,
                )
                self._record_failure()
                return []
            body = r.json()
            # llama.cpp returns results=[{"index": i, "relevance_score": s}, ...]
            # ordered by index (per server impl); we sort defensively
            results = body.get("results", [])
            scores = [0.0] * len(documents)
            for r_item in results:
                idx = r_item.get("index")
                score = r_item.get("relevance_score")
                if isinstance(idx, int) and 0 <= idx < len(documents) and score is not None:
                    scores[idx] = float(score)
            self._record_success()
            logger.debug("[RERANK] OK n=%d latency_ms=%.1f", len(documents), dt_ms)
            return scores
        except Exception as exc:
            dt_ms = (_t.monotonic() - t0) * 1000.0
            logger.warning(
                "[RERANK] error type=%s msg=%s latency_ms=%.1f url=%s",
                type(exc).__name__,
                exc,
                dt_ms,
                self.url,
            )
            self._record_failure()
            return []


_singleton: Reranker | None = None
_singleton_lock = Lock()


def get_reranker() -> Reranker:
    """Process-wide cached client."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Reranker()
    return _singleton
