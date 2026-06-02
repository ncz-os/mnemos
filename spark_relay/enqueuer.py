"""PYTHIA-side enqueuer: hive nvidia-job -> context-prepackage -> seal -> pending/.

Runs as a loop on PYTHIA (or any home-fleet host that reaches both the hive bus
and MNEMOS). For each nvidia-eligible job it claims on the hive, it pulls
relevant MNEMOS context (the Spark is stateless / network-isolated), seals the
combined payload with the shared E2EE key, and drops it in the relay bucket's
``pending/`` prefix. The Spark poller takes it from there.

Idempotency: the hive job id IS the relay uuid, and the Spark claim is a
conditional create, so re-enqueuing the same job is harmless.
"""

from __future__ import annotations

import argparse
import logging
import time

from . import relay_crypto
from .bridge_common import HiveClient, backoff_sleep, mnemos_search
from .relay_client import RelayClient

log = logging.getLogger("spark_relay.enqueuer")

# The bridge registers AS the Spark host so the hive offers it jobs submitted
# with eligible_hosts=["spark-0c53"]. (The hive has no nvidia kind; routing is
# by host parsed from the URN.)
SPARK_HOST = "spark-0c53"
CONTEXT_LIMIT = 6


def build_payload(job: dict) -> dict:
    """Shape the sealed job the Spark will execute. Pre-packages MNEMOS context."""
    prompt = job.get("prompt") or job.get("task") or job.get("description", "")
    context = mnemos_search(prompt, limit=CONTEXT_LIMIT)
    return {
        "job_id": job["id"],
        "prompt": prompt,
        "model": job.get("claimed_model") or job.get("model") or "qwen/qwen3-coder-480b-a35b-instruct",
        "repo": job.get("repo"),
        "branch": job.get("branch"),
        "context": context,
        "meta": {"submitter": job.get("submitter_urn"), "priority": job.get("priority")},
    }


def run_once(hive: HiveClient, relay: RelayClient, key: bytes) -> int:
    """Drain the hive of eligible jobs into the bucket. Returns count enqueued."""
    enqueued = 0
    while True:
        job = hive.claim_next()
        if job is None:
            break
        job_id = job["id"]
        try:
            payload = build_payload(job)
            # Embed the claimant URN so the Spark echoes it back in the terminal
            # object; the reconciler needs it to PATCH as the job's claimant (the
            # hive has no GET-single-job endpoint to look it up).
            payload["claimant_urn"] = hive.urn
            sealed = relay_crypto.seal(payload, key, aad=relay_crypto.aad_for("pending", job_id))
            relay.put_pending(job_id, sealed)
            hive.patch_status(job_id, "running", result={"note": "offloaded to spark relay bucket"})
            log.info("enqueued %s (context=%d)", job_id, len(payload["context"]))
            enqueued += 1
        except Exception as exc:  # noqa: BLE001 — surface to hive, keep draining
            log.exception("enqueue failed for %s", job_id)
            hive.patch_status(job_id, "failed", result={"error": f"enqueue: {exc}"})
    return enqueued


def main() -> None:
    ap = argparse.ArgumentParser(description="Spark relay enqueuer (PYTHIA side)")
    ap.add_argument("--interval", type=float, default=15.0, help="poll seconds")
    ap.add_argument("--once", action="store_true", help="single drain then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    key = relay_crypto.load_key()
    hive = HiveClient(
        urn=f"urn:agent:system:{SPARK_HOST}:spark-relay-enqueuer",
        runtime="system",
        kind="system",
        host=SPARK_HOST,
        capabilities=["*"],
        provider="nvidia-ngc",
        model="qwen/qwen3-coder-480b-a35b-instruct",
    )
    relay = RelayClient()
    hive.register()

    if args.once:
        run_once(hive, relay, key)
        return

    attempt = 0
    while True:
        try:
            run_once(hive, relay, key)
            attempt = 0
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("enqueuer stopped")
            return
        except Exception:  # noqa: BLE001 — never die on transient errors
            attempt += 1
            log.exception("enqueuer loop error (attempt %d)", attempt)
            backoff_sleep(attempt)


if __name__ == "__main__":
    main()
