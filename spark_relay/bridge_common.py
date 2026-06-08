"""Shared HTTP clients + helpers for the relay's PYTHIA-side processes.

Talks to the GRAEAE Hive Mind bus (:5005) and MNEMOS (:5002) over HTTP so the
relay stays decoupled from the in-process repository layer. The Spark side does
NOT import this module (it never reaches the home fleet).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger("spark_relay")

HIVE_BASE = os.environ.get("HIVE_BASE", "http://192.168.207.67:5005")
MNEMOS_BASE = os.environ.get("MNEMOS_BASE", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get("MNEMOS_TOKEN", "")
_TIMEOUT = float(os.environ.get("RELAY_HTTP_TIMEOUT", "30"))


class HiveClient:
    """Minimal client for the hive endpoints the bridge needs.

    Spark routing is by HOST, not kind: the hive has no ``nvidia`` kind, and
    dequeue eligibility filters a job's ``eligible_hosts`` against the agent host
    parsed from its URN (``urn:agent:<kind>:<host>:<sid>``). So the enqueuer
    registers AS the Spark — ``urn:agent:system:spark-0c53:…`` — and drains jobs
    submitted with ``eligible_hosts=["spark-0c53"]``. ``capabilities=["*"]``
    bypasses capability/workspace claim gates. The reconciler only PATCHes.
    """

    def __init__(
        self,
        urn: str,
        *,
        runtime: str,
        kind: str,
        host: str,
        capabilities: list[str] | None = None,
        provider: str = "unknown",
        model: str = "unknown",
        base: str = HIVE_BASE,
    ):
        self.base = base.rstrip("/")
        self.urn = urn
        self.runtime = runtime
        self.kind = kind
        self.host = host
        self.capabilities = capabilities
        self.provider = provider
        self.model = model
        self._session = requests.Session()

    def register(self, metadata: dict[str, Any] | None = None) -> None:
        """Register and ADOPT the server-assigned URN. The hive replaces the
        session segment with its own uuid; subsequent claim/patch must use the
        returned URN or the agent reads as 'not registered'. ``metadata`` is
        merged into the role tag (e.g. specs.gpus so /v1/hosts surfaces it)."""
        meta = {"role": "spark-relay-bridge", **(metadata or {})}
        try:
            resp = self._session.post(
                f"{self.base}/v1/agents/register",
                json={
                    "urn": self.urn,
                    "runtime": self.runtime,
                    "kind": self.kind,
                    "host": self.host,
                    "provider": self.provider,
                    "model": self.model,
                    "capabilities": self.capabilities,
                    "autonomy_level": "autonomous",
                    "metadata": meta,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            assigned = (resp.json() or {}).get("urn")
            if assigned:
                self.urn = assigned
        except (requests.RequestException, ValueError) as exc:
            log.warning("hive register failed (non-fatal): %s", exc)

    def heartbeat(self, status: str = "online", metadata: dict[str, Any] | None = None) -> None:
        """Keep the agent active. The hive reaps agents to 'stale' after ~90s
        without a heartbeat, after which dequeue stops offering work — so the
        loops must call this every iteration (interval < the stale window).
        ``metadata`` (e.g. {"load": {"gpus_runtime": [...]}}) is merged
        server-side so /v1/hosts shows fresh GPU telemetry."""
        body: dict[str, Any] = {"urn": self.urn, "status": status}
        if metadata:
            body["metadata"] = metadata
        try:
            self._session.post(
                f"{self.base}/v1/agents/heartbeat",
                json=body,
                timeout=_TIMEOUT,
            ).raise_for_status()
        except requests.RequestException as exc:
            log.warning("heartbeat failed (non-fatal): %s", exc)

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically dequeue+claim the next job this agent is eligible for, or
        None if the queue is dry. Eligibility = job.eligible_hosts covers this
        agent's host (parsed from the URN at registration).

        Never raises: any non-2xx (204 empty, 409 claim race, 403 stale, 5xx) is
        treated as 'no claim this round' so the drain loop keeps running."""
        try:
            resp = self._session.post(
                f"{self.base}/v1/jobs/next",
                params={"agent_urn": self.urn},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("claim_next failed: %s", exc)
            return None
        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            log.warning("claim_next %s: %s", resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json() or None
        except ValueError:
            return None

    def patch_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        claimed_by: str | None = None,
    ) -> bool:
        """PATCH a job to ``status`` per the JobUpdate contract. Non-status data
        (commit_sha, branch, metrics, error) goes inside ``result``.

        ``claimed_by`` MUST equal the job's current claimant or the hive returns
        403. The reconciler (a different agent than the enqueuer that claimed the
        job) passes the job's actual claimant; the enqueuer omits it and defaults
        to its own URN (it is the claimant)."""
        code = self.patch_status_code(job_id, status, result=result, claimed_by=claimed_by)
        return code is not None and 200 <= code < 300

    def patch_status_code(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        claimed_by: str | None = None,
    ) -> int | None:
        """Like :meth:`patch_status` but returns the HTTP status code (or None
        on a network-level failure) so callers can distinguish validation
        rejections (4xx — e.g. a bus that doesn't know a status enum) from
        transient transport errors that should be retried, not worked around."""
        body: dict[str, Any] = {"status": status, "claimed_by": claimed_by or self.urn}
        if result is not None:
            body["result"] = result
        try:
            resp = self._session.patch(f"{self.base}/v1/jobs/{job_id}", json=body, timeout=_TIMEOUT)
            if resp.status_code >= 400:
                log.error(
                    "patch_status %s -> %s rejected %s: %s",
                    job_id, status, resp.status_code, resp.text[:200],
                )
            return resp.status_code
        except requests.RequestException as exc:
            log.error("patch_status %s -> %s failed: %s", job_id, status, exc)
            return None


def mnemos_search(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Retrieve relevant MNEMOS context for context-prepackaging (fail-soft).

    Returns a list of ``{"id", "content"}`` dicts. On any failure returns ``[]``
    rather than blocking the job — the Spark prompt degrades gracefully to no
    injected context. Endpoint/shape per the MNEMOS HTTP API (Bearer auth).
    """
    if not query.strip():
        return []
    headers = {"Authorization": f"Bearer {MNEMOS_TOKEN}"} if MNEMOS_TOKEN else {}
    try:
        resp = requests.post(
            f"{MNEMOS_BASE}/v1/memories/search",
            json={"query": query, "limit": limit, "semantic": True},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("mnemos_search failed (degrading to no context): %s", exc)
        return []
    rows = data.get("memories", data) if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if isinstance(r, dict) and r.get("content"):
            out.append({"id": r.get("id"), "content": r["content"]})
    return out


def backoff_sleep(attempt: int, base: float = 2.0, cap: float = 60.0) -> None:
    """Deterministic exponential backoff (no jitter — single bridge process)."""
    time.sleep(min(cap, base * (2 ** max(0, attempt - 1))))
