#!/usr/bin/env python3
"""
GRAEAE Hive Mind — goose worker daemon.

Self-driving worker: registers as agent, polls /v1/jobs/next, invokes `goose run`,
reports result. One in-flight job per process. Run multiple per host for parallelism.

Env:
  HIVE_URL              http://192.168.207.8:5005 (default)
  GOOSE_BIN             /usr/local/bin/goose (default)
  AGENT_HOST            $(hostname) (default)
  AGENT_CAPABILITIES    comma-sep, default "code-edit,build,test,debug,refactor"
  ELIGIBLE_KINDS_ONLY   if set, only claim jobs explicitly listing 'goose' in eligible_kinds
  POLL_INTERVAL         30 seconds idle wait
  HEARTBEAT_INTERVAL    15 seconds
  GOOSE_TIMEOUT         600 seconds per job
  GOOSE_EXTRA_ARGS      extra goose run args, e.g. "--agent build"
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import random
import signal

HIVE_URL = os.environ.get("HIVE_URL", "http://192.168.207.8:5005")
GOOSE_BIN = os.environ.get("GOOSE_BIN", "/usr/local/bin/goose")
AGENT_HOST = os.environ.get("AGENT_HOST", socket.gethostname())
AGENT_CAPABILITIES = [c.strip() for c in os.environ.get(
    "AGENT_CAPABILITIES",
    "code-edit,build,test,debug,refactor,python,bash,docker,linux"
).split(",") if c.strip()]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
GOOSE_TIMEOUT = int(os.environ.get("GOOSE_TIMEOUT", "600"))
GOOSE_EXTRA_ARGS = os.environ.get("GOOSE_EXTRA_ARGS", "").split()

_urn: str = ""
_last_heartbeat = 0.0
_running = True


def _signal_handler(signum, frame):
    global _running
    print(f"[worker] signal {signum} — shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _http(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict | None]:
    url = f"{HIVE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
            if code == 204 or not raw:
                return code, None
            return code, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        print(f"[worker] http error {method} {path}: {e}", flush=True)
        return 0, None


def register() -> str:
    global _urn, _last_heartbeat
    body = {
        "kind": "goose",
        "host": AGENT_HOST,
        "pid": os.getpid(),
        "capabilities": AGENT_CAPABILITIES,
        "version": _goose_version(),
        "metadata": {
            "daemon": "goose_worker.py",
            "extra_args": GOOSE_EXTRA_ARGS,
            "started_at": time.time(),
        },
    }
    code, resp = _http("POST", "/v1/agents/register", body)
    if code == 200 and resp:
        _urn = resp["urn"]
        _last_heartbeat = time.time()
        print(f"[worker] registered urn={_urn}", flush=True)
        return _urn
    print(f"[worker] register failed code={code} resp={resp}", flush=True)
    sys.exit(1)


def _goose_version() -> str:
    try:
        out = subprocess.run([GOOSE_BIN, "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def heartbeat():
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL:
        return
    _http("POST", "/v1/agents/heartbeat", {"urn": _urn})
    _last_heartbeat = time.time()


def claim_next_job() -> dict | None:
    code, resp = _http("POST", f"/v1/jobs/next?agent_urn={_urn}")
    if code == 200 and resp:
        return resp
    if code != 204 and code != 0:
        print(f"[worker] dequeue unexpected code={code}", flush=True)
    return None


def update_job(job_id: str, status: str, result: dict):
    body = {"status": status, "result": result}
    _http("PATCH", f"/v1/jobs/{job_id}", body)


def run_goose(description: str) -> dict:
    cmd = [GOOSE_BIN, "run", "--text", description, "--no-session"] + GOOSE_EXTRA_ARGS
    print(f"[worker] $ {' '.join(cmd[:6])} … [desc len={len(description)}]", flush=True)
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GOOSE_TIMEOUT)
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-4000:],
            "duration_sec": round(time.time() - start, 1),
            "goose_cmd": " ".join(cmd[:6]),
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "error": f"timeout {GOOSE_TIMEOUT}s exceeded",
            "duration_sec": round(time.time() - start, 1),
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "error": f"{type(e).__name__}: {e}",
        }


def main():
    register()
    backoff = 1.0
    while _running:
        heartbeat()
        job = claim_next_job()
        if not job:
            # exp backoff up to POLL_INTERVAL, plus jitter
            time.sleep(min(backoff, POLL_INTERVAL) + random.uniform(0, 2))
            backoff = min(backoff * 1.5, POLL_INTERVAL)
            continue
        backoff = 1.0  # reset on success
        print(f"[worker] claimed job {job['id'][:8]} kind={job['kind']} priority={job.get('priority')}", flush=True)
        update_job(job["id"], "running", {"started_by": _urn, "started_at": time.time()})
        result = run_goose(job.get("description") or job.get("kind", ""))
        status = "done" if result.get("exit_code") == 0 else "failed"
        result["finished_at"] = time.time()
        update_job(job["id"], status, result)
        print(f"[worker] {status} job {job['id'][:8]} exit={result.get('exit_code')} dur={result.get('duration_sec')}s", flush=True)
    print("[worker] clean shutdown", flush=True)


if __name__ == "__main__":
    main()
