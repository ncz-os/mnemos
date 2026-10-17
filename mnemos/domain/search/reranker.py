"""Cross-encoder reranker HTTP client (v6.2 M-2.2.3).

Talks to a llama.cpp `--rerank` endpoint (e.g. a host running
bge-reranker-v2-m3; see docs/v6.2-nexus-pattern-adoption.md § Reranker
service). There is no default — the previous default pointed at a host
that no longer exists, so the default is now empty. With no URL
configured, rerank() short-circuits to ``[]`` and makes no HTTP call;
see `MNEMOS_RERANKER_URL` below.

Circuit-breaker semantics mirror `mnemos/runtime/embedder.py::_HttpBackend`:
- on 5 consecutive failures, open breaker for 60s
- breaker-open returns ``[]`` so caller falls through to no-rerank
  (the un-reranked order is still a valid result set, never block
  search on a missing reranker — matches v6.2 acceptance criterion
  "failover to FTS-only ranking if the reranker host is down 30s+")
"""

from __future__ import annotations

import logging
import time as _t
from threading import Lock
from typing import Any

from mnemos.core.config import reranker_model_env, reranker_timeout_secs_env, reranker_url_env

logger = logging.getLogger(__name__)


# llama.cpp /v1/rerank shape:
#   {"model": "bge-reranker-v2-m3", "query": "...", "documents": ["...", ...]}
# Response:
#   {"results": [{"index": 0, "relevance_score": 0.87}, ...]}
# No default: the previous default pointed at a host that no longer
# exists, so the default is now empty. Set MNEMOS_RERANKER_URL to a
# trusted llama.cpp --rerank endpoint you configure.
DEFAULT_RERANKER_URL = ""
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
        self.url = url or reranker_url_env()
        self.model = model or reranker_model_env()
        env_timeout = reranker_timeout_secs_env()
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
        self._disabled_logged = False

    @property
    def is_disabled(self) -> bool:
        """True iff the reranker has no URL configured.

        Lets health probes and admin tools query the state without
        triggering ``rerank()``'s INFO-disabled log line. Distinct from a
        breaker-open state: is_disabled is permanent (until
        MNEMOS_RERANKER_URL is set and the process restarts); the breaker
        is a transient circuit-break on transient failures.
        """
        return not self.url

    def _build_client(self) -> None:
        if self._client is not None:
            return
        # Defense in depth: ``rerank()`` already guards ``if not self.url``
        # before calling here, so this should never fire in practice. Belt
        # and braces so a future caller that forgets the outer guard cannot
        # silently build an httpx client with an empty base URL.
        if not self.url:
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

        Short-circuit order (cheapest first):

        1. ``not query or not documents`` → return ``[]`` (no work to do).
        2. ``if not self.url`` (the public-call-site guard; see the
           inline block at the top of this method) → log once at INFO that
           ``MNEMOS_RERANKER_URL`` is unset and return ``[]``.
           Without this guard, every call would build an httpx client
           with an empty base URL and POST to it, generating a
           continuous stream of connect-failure warnings until the
           circuit breaker opened. This short-circuit is the
           **public call site** guard: it fires BEFORE any client
           construction, BEFORE any breaker accounting, and BEFORE any
           network I/O. Verified by
           ``tests/test_reranker_empty_url.py::test_rerank_empty_url_short_circuits_before_httpx_post``.
        3. ``self._breaker_open()`` → return ``[]`` (transient outage).
        """
        if not query or not documents:
            return []
        # Public-call-site empty-URL guard. MUST run before any client
        # construction, before
        # any breaker accounting, and before any network I/O. Without
        # this, the empty URL would build an httpx client and POST to
        # it on every call, spamming connect-failure warnings until
        # the breaker opened. The flag (``_disabled_logged``) makes the
        # INFO line fire exactly once; subsequent calls skip the log.
        if not self.url:
            if not self._disabled_logged:
                logger.info(
                    "[RERANK] rerank disabled: MNEMOS_RERANKER_URL is unset; "
                    "returning empty scores (no HTTP calls will be made)"
                )
                self._disabled_logged = True
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
