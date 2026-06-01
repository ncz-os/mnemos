#!/usr/bin/env python3
"""Zeroclaw Hive triage daemon.

Polls queued Hive jobs, asks MNEMOS/KNEMON for cost- and usage-aware model
selection, then writes model affinity back to Hive before workers claim jobs.
"""

from __future__ import annotations

import json
import os
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mnemos.core.config import mcp_mnemos_token_env, mcp_mnemos_url_env, system_hive_url_env

HIVE_URL = os.environ.get("HIVE_URL") or system_hive_url_env()
MNEMOS_URL = os.environ.get("MNEMOS_URL") or mcp_mnemos_url_env()
MNEMOS_TOKEN = os.environ.get("MNEMOS_API_KEY") or os.environ.get("MNEMOS_TOKEN") or mcp_mnemos_token_env()
POLL_INTERVAL = float(os.environ.get("ZEROCLAW_TRIAGE_POLL_INTERVAL", "10"))
BATCH_LIMIT = int(os.environ.get("ZEROCLAW_TRIAGE_BATCH_LIMIT", "25"))
DEFAULT_EST_TOKENS_IN = int(os.environ.get("ZEROCLAW_TRIAGE_EST_TOKENS_IN", "10000"))
DEFAULT_EST_TOKENS_OUT = int(os.environ.get("ZEROCLAW_TRIAGE_EST_TOKENS_OUT", "2000"))
COST_TIER_ORDER = ["A", "B", "C"]

_running = True


def _signal(signum: int, _frame: Any) -> None:
    global _running
    print(f"[zc-triage] signal {signum}; shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _signal)
signal.signal(signal.SIGINT, _signal)


def _http_json(
    method: str,
    base_url: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    bearer: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | None]:
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}{qs}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, None
    except Exception as exc:
        print(f"[zc-triage] http error {method} {path}: {exc}", flush=True)
        return 0, None


def _estimate_tokens(job: dict[str, Any]) -> tuple[int, int]:
    description = str(job.get("description") or "")
    estimated_in = max(DEFAULT_EST_TOKENS_IN, int(len(description.split()) * 1.3))
    return estimated_in, DEFAULT_EST_TOKENS_OUT


def _needs_routing(job: dict[str, Any]) -> bool:
    if job.get("status") != "queued":
        return False
    if job.get("routed_at"):
        return False
    return not (job.get("preferred_models") and job.get("preferred_providers"))


def _submitter_max_cost_tier_explicit(job: dict[str, Any]) -> bool:
    metadata = job.get("routing_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return bool(isinstance(metadata, dict) and metadata.get("submitter_max_cost_tier_explicit"))


def _widen_default_cap(job: dict[str, Any], decision: dict[str, Any]) -> str:
    current = str(job.get("max_cost_tier") or "A").upper()
    if _submitter_max_cost_tier_explicit(job):
        return current
    selected = str(decision.get("dispatch_cost_tier") or current).upper()
    try:
        return COST_TIER_ORDER[max(COST_TIER_ORDER.index(current), COST_TIER_ORDER.index(selected))]
    except ValueError:
        return current


def routing_patch_for_decision(job: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    existing_caps = list(job.get("required_capabilities") or [])
    decision_caps = list(decision.get("dispatch_required_capabilities") or [])
    required_capabilities: list[str] = []
    for cap in [*existing_caps, *decision_caps]:
        if cap and cap not in required_capabilities:
            required_capabilities.append(cap)
    return {
        "required_capabilities": required_capabilities,
        "eligible_kinds": [decision.get("dispatch_kind") or "zeroclaw"],
        "preferred_providers": list(decision.get("dispatch_preferred_providers") or [decision.get("provider")]),
        "preferred_models": list(decision.get("dispatch_preferred_models") or [decision.get("model_id")]),
        "max_cost_tier": _widen_default_cap(job, decision),
        "routing_metadata": {
            "router": "knemon",
            "caller_subsystem": "zeroclaw",
            "decision": decision,
            "estimated_cost_usd": decision.get("estimated_cost_usd"),
            "submitter_max_cost_tier_explicit": _submitter_max_cost_tier_explicit(job),
        },
    }


def route_job(job: dict[str, Any]) -> dict[str, Any] | None:
    est_in, est_out = _estimate_tokens(job)
    query = {
        "task_kind": job.get("kind") or "code-fix",
        "priority": int(job.get("priority") or 0),
        "est_tokens_in": est_in,
        "est_tokens_out": est_out,
        "caller_subsystem": "zeroclaw",
        "require_capability": ",".join(job.get("required_capabilities") or []),
    }
    if _submitter_max_cost_tier_explicit(job):
        query["max_cost_tier"] = str(job.get("max_cost_tier") or "A").upper()
    code, decision = _http_json(
        "GET",
        MNEMOS_URL,
        "/v1/knemon/route",
        query=query,
        bearer=MNEMOS_TOKEN,
    )
    if code != 200 or not decision:
        print(f"[zc-triage] route failed job={job.get('id')} code={code} resp={decision}", flush=True)
        return None
    return routing_patch_for_decision(job, decision)


def run_once() -> int:
    code, payload = _http_json("GET", HIVE_URL, "/v1/jobs", query={"status": "queued", "limit": BATCH_LIMIT})
    if code != 200 or not payload:
        print(f"[zc-triage] hive queue poll failed code={code} resp={payload}", flush=True)
        return 0
    routed = 0
    for job in payload.get("jobs") or []:
        if not _needs_routing(job):
            continue
        patch = route_job(job)
        if not patch:
            continue
        patch_code, patch_resp = _http_json("PATCH", HIVE_URL, f"/v1/jobs/{job['id']}/routing", body=patch)
        if patch_code == 200:
            routed += 1
            print(
                f"[zc-triage] routed job={job['id'][:8]} model={patch['preferred_providers'][0]}/{patch['preferred_models'][0]}",
                flush=True,
            )
        else:
            print(f"[zc-triage] route patch failed job={job['id']} code={patch_code} resp={patch_resp}", flush=True)
    return routed


def main() -> None:
    print(f"[zc-triage] starting HIVE_URL={HIVE_URL} MNEMOS_URL={MNEMOS_URL}", flush=True)
    while _running:
        run_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
