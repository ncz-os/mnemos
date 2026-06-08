"""PYTHIA-side reconciler: terminal/ -> open -> land patch -> hive status + purge.

Polls the relay bucket's single ``terminal/`` prefix, decrypts each sealed
object with the shared E2EE key, branches on its ``status`` (done/failed/
needs-review), LANDS any needs-review patch as a ``hive/spark-<jobid>`` review
branch on the canonical repository (the 2026-06-07 orphan incident: 73 patches
were trapped inside done-marked job results because no landing step existed),
reports the job's terminal status back to the hive, then purges the job's
objects. Decoupled from the Spark; runs whenever.
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from . import relay_crypto
from .bridge_common import HiveClient, backoff_sleep
from .lander import PatchLander, PermanentLandingError, TransientLandingError
from .relay_client import RelayClient

log = logging.getLogger("spark_relay.reconciler")

MAX_RESULT_BYTES = 256 * 1024
TERMINAL_STATUSES = {"done", "failed", "needs-review"}


def _json_size_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _shrink_result(result: dict) -> dict:
    """Drop the largest payload fields until the result fits MAX_RESULT_BYTES.

    Pre-lander this path replaced the WHOLE result with ``payload_too_large``,
    destroying the patch. Now the patch (a) usually already landed as a branch
    and (b) is dropped field-by-field, keeping the routing/review metadata.
    """
    for field in ("patch", "suggestion", "metrics", "files_changed"):
        if _json_size_bytes(result) <= MAX_RESULT_BYTES:
            return result
        if field in result:
            result[f"{field}_dropped"] = "payload_too_large"
            del result[field]
    if _json_size_bytes(result) > MAX_RESULT_BYTES:
        keep = {
            k: result.get(k)
            for k in (
                "needs_review", "commit_sha", "spark_commit_sha", "commits", "branch",
                "landed_branch", "landed_repo", "landed_sha",
                "landing", "landing_error", "patch_saved",
            )
            if k in result
        }
        keep["error"] = "payload_too_large"
        return keep
    return result


def _reconcile_one(
    hive: HiveClient, relay: RelayClient, key: bytes, uuid: str, lander: PatchLander
) -> bool:
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
        # survive the round-trip. Strip only the routing field.
        result = {k: v for k, v in payload.items() if k != "claimant_urn"}
        result["needs_review"] = (status == "needs-review") or bool(payload.get("needs_review"))

        # LANDER (2026-06-08): a needs-review payload with a patch must land as
        # a review branch BEFORE the job is closed out — a patch that exists
        # only inside a job result is an orphan (the 2026-06-07 incident).
        if result["needs_review"] and payload.get("patch"):
            try:
                landed = lander.land(payload, uuid)
                result.update(landed)
                # Surface the LANDED commit (git am re-hashes; the Spark-side
                # sha never exists on the canonical remote) where the bus
                # work-contract guard looks, so commit-mandatory jobs read as
                # truthfully committed. Keep the Spark sha for traceability.
                result["spark_commit_sha"] = payload.get("commit_sha")
                result["commit_sha"] = landed["landed_sha"]
                result["commits"] = [landed["landed_sha"]]
            except TransientLandingError as exc:
                log.warning("transient landing failure for %s — will retry: %s", uuid, exc)
                return False
            except PermanentLandingError as exc:
                # Close out, but never lose the patch: keep it in the result
                # AND save it durably on disk (the result copy may be dropped
                # by the size cap below).
                result["landing"] = "failed"
                result["landing_error"] = str(exc)[:500]
                saved = lander.save_orphan_patch(payload, uuid)
                if saved:
                    result["patch_saved"] = saved
                log.error("permanent landing failure for %s: %s", uuid, exc)

        result = _shrink_result(result)
        # Prefer the hive's first-class needs-review status (reviewer timer
        # watches it). Fall back to done + the needs_review result flag ONLY
        # on an explicit schema-validation rejection (400/422 = a bus that
        # doesn't know the enum). Everything else — 401/403 (auth/claimant),
        # 408/409/423/429 (transient or state conflicts), 5xx, network — must
        # retry on the next sweep, never double-PATCH a review job to done.
        if result["needs_review"]:
            code = hive.patch_status_code(uuid, "needs-review", result=result, claimed_by=claimant)
            if code is not None and 200 <= code < 300:
                ok = True
            elif code in (400, 422):
                ok = hive.patch_status(uuid, "done", result=result, claimed_by=claimant)
            else:
                ok = False
        else:
            ok = hive.patch_status(uuid, "done", result=result, claimed_by=claimant)
    if not ok:
        log.error("hive PATCH for %s failed — leaving bucket objects for retry", uuid)
        return False
    log.info("reconciled %s %s", "FAILED" if failed else "done", uuid)
    relay.purge(uuid)  # safe now: hive durably holds terminal state
    return True


def run_once(hive: HiveClient, relay: RelayClient, key: bytes, lander: PatchLander) -> int:
    count = 0
    for uuid in relay.list_terminal():
        try:
            if _reconcile_one(hive, relay, key, uuid, lander):
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
    lander = PatchLander()
    hive.register()

    if args.once:
        run_once(hive, relay, key, lander)
        return

    attempt = 0
    while True:
        try:
            hive.heartbeat()  # stay 'online'
            run_once(hive, relay, key, lander)
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
