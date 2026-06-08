"""PYTHIA-side reconciler: terminal/ -> open -> hive terminal status + purge.

Polls the relay bucket's single ``terminal/`` prefix, decrypts each sealed
object with the shared E2EE key, branches on its ``status`` (done/failed),
reports terminal status back to the hive (with ``commit_sha`` so ARGONAS can fan
out by sha), then purges the job's objects. Decoupled from the Spark; runs
whenever.
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from . import relay_crypto
from .bridge_common import HiveClient, backoff_sleep
from .relay_client import RelayClient

log = logging.getLogger("spark_relay.reconciler")

MAX_RESULT_BYTES = 256 * 1024
TERMINAL_STATUSES = {"done", "failed", "needs-review"}


def _json_size_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _reconcile_one(hive: HiveClient, relay: RelayClient, key: bytes, uuid: str) -> bool:
    """Report one terminal job to the hive and purge ONLY on a durable PATCH.

    Returns True if reconciled (and purged); False if the hive PATCH did not
    succeed, in which case the bucket objects are left in place for the next
    sweep to retry — never delete evidence the hive hasn't acknowledged.
    """
    payload = relay_crypto.open_blob(
        relay.get_terminal(uuid), key, aad=relay_crypto.aad_for("terminal", uuid)
    )
    status = payload.get("status")
    if status not in TERMINAL_STATUSES:
        log.error("unknown terminal status for %s: %r — leaving for inspection", uuid, status)
        return False

    # The hive only lets a job's current claimant update it; the enqueuer claimed
    # it under a different URN than this reconciler and echoed that URN through
    # the terminal payload, so PATCH as that claimant.
    claimant = payload.get("claimant_urn")
    failed = status == "failed"
    if failed:
        result = {"error": payload.get("error", "spark failure")}
        if _json_size_bytes(result) > MAX_RESULT_BYTES:
            result = {"error": "payload_too_large"}
        ok = hive.patch_status(uuid, "failed", result=result, claimed_by=claimant)
    else:
        # Pass the FULL Spark terminal payload through to the hive result so the
        # agentic executor's patch / suggestion / needs_review / files_changed
        # survive the round-trip (the hive has no needs-review status, so flag it
        # in the result). Strip only the routing field.
        result = {k: v for k, v in payload.items() if k != "claimant_urn"}
        result["needs_review"] = (status == "needs-review") or bool(payload.get("needs_review"))
        if _json_size_bytes(result) > MAX_RESULT_BYTES:
            result = {"error": "payload_too_large"}
            ok = hive.patch_status(uuid, "failed", result=result, claimed_by=claimant)
        else:
            ok = hive.patch_status(
                uuid,
                "done",
                result=result,
                claimed_by=claimant,
            )
    if not ok:
        log.error("hive PATCH for %s failed — leaving bucket objects for retry", uuid)
        return False
    log.info("reconciled %s %s", "FAILED" if failed else "done", uuid)
    relay.purge(uuid)  # safe now: hive durably holds terminal state
    return True


def run_once(hive: HiveClient, relay: RelayClient, key: bytes) -> int:
    count = 0
    for uuid in relay.list_terminal():
        try:
            if _reconcile_one(hive, relay, key, uuid):
                count += 1
        except relay_crypto.RelayCryptoError:
            log.exception("undecryptable %s — leaving for inspection", uuid)
        except Exception:  # noqa: BLE001
            log.exception("reconcile %s failed", uuid)
    return count


def main() -> None:
    ap = argparse.ArgumentParser(description="Spark relay reconciler (PYTHIA side)")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    key = relay_crypto.load_key()
    hive = HiveClient(
        urn="urn:agent:mnemos:pythia:spark-relay-reconciler",
        runtime="mnemos",
        kind="mnemos",
        host="pythia",
        capabilities=["*"],
    )
    relay = RelayClient()
    hive.register()

    if args.once:
        run_once(hive, relay, key)
        return

    attempt = 0
    while True:
        try:
            hive.heartbeat()  # stay 'online'
            run_once(hive, relay, key)
            attempt = 0
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("reconciler stopped")
            return
        except Exception:  # noqa: BLE001
            attempt += 1
            log.exception("reconciler loop error (attempt %d)", attempt)
            backoff_sleep(attempt)


if __name__ == "__main__":
    main()
