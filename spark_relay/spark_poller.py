"""Spark-side poller: pending/ -> claim -> execute -> results/.

Runs ON the DGX Spark (``spark-0c53``), which is network-isolated from the home
fleet. It imports ONLY :mod:`relay_crypto` and :mod:`relay_client` — never
``bridge_common`` (no hive/MNEMOS reachability). Loop:

    list pending  ->  conditional-claim each  ->  (if won) open  ->  execute on
    host-locked NGC model  ->  seal result  ->  put results/ (or failed/)

The actual agentic work (run the model, edit the repo, commit, push to GitHub)
is delegated to an :class:`Executor`. A reference NGC-chat executor is provided;
swap in the full agentic runtime by passing ``--executor`` or editing
:func:`make_executor`. The relay plumbing is complete and round-trippable with
the stub.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time
from typing import Protocol

from . import relay_crypto
from .relay_client import RelayClient

log = logging.getLogger("spark_relay.poller")


def worker_id() -> str:
    return os.environ.get("SPARK_WORKER_ID") or socket.gethostname()


class Executor(Protocol):
    def execute(self, job: dict) -> dict:
        """Run the job. Return ``{commit_sha, branch, metrics}`` or raise."""
        ...


class OpenAIChatExecutor:
    """Calls any OpenAI-compatible /chat/completions endpoint (local llama.cpp /
    ollama / vLLM, or NGC). The local GB10 coder is the primary; NGC is the
    cloud fallback. Does NOT yet edit/commit/push a repo — that is the
    integration point for the full agentic runtime (see module docstring).
    """

    def __init__(self, base: str, api_key: str, default_model: str, *, label: str, timeout: float):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.label = label
        self.timeout = timeout

    def execute(self, job: dict) -> dict:
        import requests

        model = job.get("model") or self.default_model
        context = "\n\n".join(c["content"] for c in job.get("context", []))
        sys_prompt = (
            f"You are a Spark coding worker. Use the provided MNEMOS context.\n\nCONTEXT:\n{context}"
            if context
            else "You are a Spark coding worker."
        )
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = requests.post(
            f"{self.base}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": job["prompt"]},
                ],
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        out = resp.json()["choices"][0]["message"]["content"]
        # TODO(agentic): apply edits, git commit, git push -> set commit_sha/branch.
        return {
            "commit_sha": None,
            "branch": job.get("branch"),
            "metrics": {"backend": self.label, "model": model, "output_chars": len(out), "output": out},
        }


class FallbackExecutor:
    """Try executors in order; use the first that succeeds. Lets the local GB10
    coder serve by default and fall back to NGC only when local is down."""

    def __init__(self, chain: list[tuple[str, OpenAIChatExecutor]]):
        self.chain = chain

    def execute(self, job: dict) -> dict:
        errors = []
        for name, ex in self.chain:
            try:
                return ex.execute(job)
            except Exception as exc:  # noqa: BLE001 — try the next backend
                log.warning("executor %s failed, trying next: %s", name, exc)
                errors.append(f"{name}: {exc}")
        raise RuntimeError("all executors failed: " + " | ".join(errors))


def _local_executor() -> OpenAIChatExecutor:
    return OpenAIChatExecutor(
        os.environ.get("LLM_BASE", "http://localhost:11434/v1"),
        os.environ.get("LLM_API_KEY", ""),
        os.environ.get("LLM_MODEL", "qwen2.5-coder:32b"),
        label="local",
        timeout=float(os.environ.get("LLM_TIMEOUT", "900")),
    )


def _ngc_executor() -> OpenAIChatExecutor:
    return OpenAIChatExecutor(
        os.environ.get("NGC_BASE", "https://integrate.api.nvidia.com/v1"),
        os.environ.get("NGC_API_KEY", ""),
        os.environ.get("NGC_MODEL", "qwen/qwen3-coder-480b-a35b-instruct"),
        label="ngc",
        timeout=float(os.environ.get("NGC_TIMEOUT", "600")),
    )


def make_executor(name: str) -> Executor:
    if name == "local":
        return _local_executor()
    if name == "ngc":
        return _ngc_executor()
    if name in ("local+ngc", "auto"):  # local primary, NGC fallback
        return FallbackExecutor([("local", _local_executor()), ("ngc", _ngc_executor())])
    raise SystemExit(f"unknown executor {name!r}")


def _seal_terminal(uuid: str, payload: dict, key: bytes) -> bytes:
    return relay_crypto.seal(payload, key, aad=relay_crypto.aad_for("terminal", uuid))


def _write_terminal(relay: RelayClient, uuid: str, payload: dict, key: bytes, *, claimant: str | None = None) -> None:
    # Echo the claimant URN through so the reconciler can PATCH the hive as the
    # job's claimant (the enqueuer that claimed it).
    if claimant and "claimant_urn" not in payload:
        payload = {**payload, "claimant_urn": claimant}
    if not relay.put_terminal(uuid, _seal_terminal(uuid, payload, key)):
        log.warning("terminal for %s already existed — keeping first", uuid)


def run_once(relay: RelayClient, key: bytes, executor: Executor, *, owner: str | None = None) -> int:
    """One sweep of pending/. Returns number of jobs executed this sweep."""
    owner = owner or worker_id()
    done = 0
    for uuid in relay.list_pending():
        if not relay.claim(uuid, owner):
            continue  # another live worker owns it (or lease not yet expired)
        try:
            job = relay_crypto.open_blob(relay.get_pending(uuid), key, aad=relay_crypto.aad_for("pending", uuid))
        except relay_crypto.RelayCryptoError as exc:
            # Don't strand the claim: record a durable terminal failure so the
            # reconciler closes the job out instead of it blocking forever.
            log.exception("undecryptable pending %s — quarantining", uuid)
            _write_terminal(relay, uuid, {"status": "failed", "error": f"undecryptable pending: {exc}"}, key)
            done += 1
            continue
        claimant = job.get("claimant_urn")
        if job.get("job_id") not in (None, uuid):
            log.error("payload job_id %r != object %s — quarantining", job.get("job_id"), uuid)
            _write_terminal(
                relay,
                uuid,
                {"status": "failed", "error": "job_id/object uuid mismatch"},
                key,
                claimant=claimant,
            )
            done += 1
            continue
        try:
            result = executor.execute(job)
            result.setdefault("status", "done")
            _write_terminal(relay, uuid, result, key, claimant=claimant)
            log.info("executed %s sha=%s", uuid, result.get("commit_sha"))
        except Exception as exc:  # noqa: BLE001 — report failure, keep polling
            log.exception("execute %s failed", uuid)
            _write_terminal(relay, uuid, {"status": "failed", "error": str(exc)}, key, claimant=claimant)
        done += 1
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="Spark relay poller (Spark side)")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--executor", default="local+ngc", help="local | ngc | local+ngc (local primary)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument(
        "--worker-id",
        default=None,
        help="claim owner + identity; run several with distinct ids for concurrency",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    key = relay_crypto.load_key()
    relay = RelayClient()
    executor = make_executor(args.executor)
    owner = args.worker_id or worker_id()
    log.info("poller starting worker=%s executor=%s", owner, args.executor)

    if args.once:
        run_once(relay, key, executor, owner=owner)
        return

    while True:
        try:
            run_once(relay, key, executor, owner=owner)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("poller stopped")
            return
        except Exception:  # noqa: BLE001
            log.exception("poller loop error")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
