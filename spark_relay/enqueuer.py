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
from .spark_poller import NONCOMMIT_PREFIXES, repo_url_for_kind

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
        "kind": job.get("kind"),  # poller KIND_WORKSPACE_MAP needs it (was missing -> no repo mapping, 2026-06-06)
        "prompt": prompt,
        # No hardcoded model: the Spark executor picks (local GB10 coder primary,
        # NGC fallback). A submitter MAY pin one via the job's model field.
        "model": job.get("claimed_model") or job.get("model"),
        "repo": job.get("repo"),
        "branch": job.get("branch"),
        "context": context,
        "meta": {"submitter": job.get("submitter_urn"), "priority": job.get("priority")},
    }


def _spark_should_offload(job: dict) -> bool:
    """The Spark only takes work it can actually complete.

    Offload when the job is (a) explicitly host-targeted to the Spark, (b) a
    no-commit kind (research/analysis/triage/etc. — answered via chat, no repo
    needed), or (c) a kind the Spark poller can map to a repo. Everything else
    (notably a general ``build:<repo>`` the relay can't map) is left for the
    home fleet, which has the full ``zc_native_build`` repo map. This prevents
    the Spark from claiming build jobs it cannot finish — they would otherwise
    zombie in 'running' or degrade to a useless chat suggestion.
    """
    kind = str(job.get("kind") or "")
    eligible_hosts = job.get("eligible_hosts") or []
    if any(str(h).strip().lower() == SPARK_HOST for h in eligible_hosts):
        return True
    if kind.startswith(NONCOMMIT_PREFIXES):
        return True
    return repo_url_for_kind(kind) is not None


def run_once(hive: HiveClient, relay: RelayClient, key: bytes) -> int:
    """Drain the hive of eligible jobs into the bucket. Returns count enqueued."""
    enqueued = 0
    released_this_sweep: set[str] = set()
    while True:
        job = hive.claim_next()
        if job is None:
            break
        job_id = job["id"]
        if not _spark_should_offload(job):
            # Release back to the queue (we are the claimant, so patch_status
            # defaults claimed_by to our URN) for a home-fleet worker to take.
            hive.patch_status(
                job_id,
                "queued",
                result={"note": "released by spark enqueuer: not spark-offloadable (no repo mapping)"},
            )
            log.info("released %s (kind=%s) — not spark-offloadable", job_id, job.get("kind"))
            if job_id in released_this_sweep:
                # Re-claimed our own release before a home worker did; stop to
                # avoid a hot release/claim loop. Next sweep retries.
                break
            released_this_sweep.add(job_id)
            continue
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


GPU_STATUS_NAME = SPARK_HOST + "-gpu"


def read_gpu(relay: RelayClient, key: bytes) -> dict:
    """Read the remote worker's GPU snapshot off the bucket (fail-soft)."""
    try:
        raw = relay.get_status(GPU_STATUS_NAME)
        if not raw:
            return {}
        return relay_crypto.open_blob(raw, key, aad=relay_crypto.aad_for("status", GPU_STATUS_NAME))
    except Exception as exc:  # noqa: BLE001
        log.warning("read_gpu failed: %s", exc)
        return {}


def gpu_metadata(relay: RelayClient, key: bytes) -> dict:
    """Build agent metadata so /v1/hosts surfaces the remote GPU in the dashboard."""
    snap = read_gpu(relay, key)
    return {
        "specs": {"gpus": snap.get("specs_gpus", []), "arch": "aarch64", "has_npu": False},
        "load": {"gpus_runtime": snap.get("gpus_runtime", [])},
    }


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
    hive.register(metadata=gpu_metadata(relay, key))

    if args.once:
        run_once(hive, relay, key)
        return

    attempt = 0
    while True:
        try:
            # heartbeat carries fresh remote GPU telemetry -> dashboard GPU panel
            hive.heartbeat(metadata=gpu_metadata(relay, key))
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
