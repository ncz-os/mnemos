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
    """Minimal client for the hive endpoints the bridge needs."""

    def __init__(self, base: str = HIVE_BASE, urn: str = "mnemos:pythia:spark-bridge"):
        self.base = base.rstrip("/")
        self.urn = urn
        self._session = requests.Session()

    def register(self, kind: str = "mnemos", host: str = "pythia") -> None:
        try:
            self._session.post(
                f"{self.base}/v1/agents/register",
                json={"urn": self.urn, "kind": kind, "host": host},
                timeout=_TIMEOUT,
            ).raise_for_status()
        except requests.RequestException as exc:
            log.warning("hive register failed (non-fatal): %s", exc)

    def claim_next(self, eligible_kinds: list[str]) -> dict[str, Any] | None:
        """Atomically claim the next eligible job, or None if the queue is dry."""
        try:
            resp = self._session.post(
                f"{self.base}/v1/jobs/next",
                params={"agent_urn": self.urn},
                json={"eligible_kinds": eligible_kinds},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("claim_next failed: %s", exc)
            return None
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json() or None

    def patch_status(self, job_id: str, status: str, **fields: Any) -> bool:
        body = {"status": status, "claimed_by": self.urn, **fields}
        try:
            resp = self._session.patch(f"{self.base}/v1/jobs/{job_id}", json=body, timeout=_TIMEOUT)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("patch_status %s -> %s failed: %s", job_id, status, exc)
            return False


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
