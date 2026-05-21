"""Embedding throughput bench — measures /v1/embeddings + /api/embeddings.

Compares latency and throughput of embedding endpoints across hosts
(CERBERUS RTX 4500 ADA today; TYPHON RTX 5060 once Ollama is
installed). Emits an HMAC-signed JSON artifact under docs/proof/.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import random
import statistics
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

HMAC_KEY = os.environ.get("ORACLE_PROOF_HMAC_KEY", "mnemos-oracle-proof-v1")


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    s = sorted(samples)
    return {
        "n": len(s),
        "min_ms": round(s[0], 3),
        "p50_ms": round(statistics.median(s), 3),
        "p95_ms": round(s[int(0.95 * (len(s) - 1))], 3),
        "p99_ms": round(s[int(0.99 * (len(s) - 1))], 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(statistics.fmean(s), 3),
        "throughput_per_sec": round(1000.0 / statistics.fmean(s), 1) if s else 0,
    }


def _random_text(n_chars: int) -> str:
    rng = random.Random(0)
    return " ".join("".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 9))) for _ in range(n_chars // 6))


async def _bench_endpoint(name: str, base_url: str, model: str, n: int) -> dict[str, Any]:
    texts = [_random_text(s) for s in (256, 512, 1024, 2048)]
    async with httpx.AsyncClient(timeout=30) as client:
        # Warm up
        try:
            await client.post(
                f"{base_url}/api/embeddings",
                json={"model": model, "prompt": texts[0]},
            )
        except Exception:
            pass

        per_size: dict[int, list[float]] = {}
        for text in texts:
            size = len(text)
            samples: list[float] = []
            for _ in range(n):
                t0 = time.perf_counter()
                r = await client.post(
                    f"{base_url}/api/embeddings",
                    json={"model": model, "prompt": text},
                )
                if r.status_code != 200:
                    break
                _ = r.json()
                samples.append((time.perf_counter() - t0) * 1000.0)
            per_size[size] = samples

        return {
            "endpoint": name,
            "base_url": base_url,
            "model": model,
            "by_text_size": {str(sz): _stats(samples) for sz, samples in per_size.items()},
        }


async def main_async(n: int) -> dict[str, Any]:
    endpoints = [
        {
            "name": "cerberus-rtx-4500-ada",
            "base_url": "http://192.168.207.96:11434",
            "model": "nomic-embed-text:latest",
            "hardware": "NVIDIA RTX 4500 ADA, 24 GB",
        },
        {
            "name": "typhon-rtx-5060",
            "base_url": "http://192.168.207.61:11435",
            "model": "nomic-embed-text:latest",
            "hardware": "NVIDIA RTX 5060, 8 GB",
        },
    ]
    extra = os.environ.get("EMBED_BENCH_EXTRA")
    if extra:
        for entry in extra.split(","):
            host, model = entry.split("|", 1) if "|" in entry else (entry, "nomic-embed-text:latest")
            endpoints.append({"name": host, "base_url": host, "model": model, "hardware": "operator-configured"})

    results: list[dict[str, Any]] = []
    for ep in endpoints:
        print(f"[bench] {ep['name']} ({ep['hardware']})")
        try:
            results.append({**ep, **await _bench_endpoint(ep["name"], ep["base_url"], ep["model"], n)})
        except Exception as e:
            results.append({**ep, "error": str(e).splitlines()[0]})

    body = {
        "schema": "mnemos-embed-bench/v1",
        "git_head_sha": _git_head(),
        "run_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "iterations_per_size": n,
        "results": results,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    sig = hmac.new(HMAC_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {
        "evidence": body,
        "hmac_sha256": sig,
        "hmac_key_id": hashlib.sha256(HMAC_KEY.encode()).hexdigest()[:16],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument(
        "--out",
        default=str(
            REPO_ROOT
            / "docs"
            / "proof"
            / f"oracle-embed-bench-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        ),
    )
    args = ap.parse_args()
    artifact = asyncio.run(main_async(args.n))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"\nwrote {out}")
    for r in artifact["evidence"]["results"]:
        if "error" in r:
            print(f"  {r['name']}: ERROR {r['error']}")
            continue
        print(f"  {r['name']} ({r['hardware']}) — model={r['model']}")
        for size, s in r["by_text_size"].items():
            print(
                f"    chars={size:5s}  p50={s['p50_ms']:.1f}ms  "
                f"p95={s['p95_ms']:.1f}ms  ~{s['throughput_per_sec']:.0f}/s"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
