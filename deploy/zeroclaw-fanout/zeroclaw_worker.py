#!/usr/bin/env python3
"""
GRAEAE Hive Mind — zeroclaw worker daemon.

Self-driving worker: registers as agent (kind=zeroclaw), polls /v1/jobs/next,
invokes `zeroclaw agent -a hive -m <description>`, reports result.
One in-flight job per process. Run multiple per host for parallelism.

Env:
  HIVE_URL              http://192.168.207.67:5005 (default)
  ZEROCLAW_BIN          zeroclaw (default, resolved via PATH)
  ZEROCLAW_AGENT        hive (default agent alias in ~/.zeroclaw/config.toml)
  AGENT_HOST            $(hostname) (default)
  AGENT_CAPABILITIES    comma-sep, default "code-edit,multi-agent,delegate,python,bash"
  POLL_INTERVAL         30 seconds idle wait
  HEARTBEAT_INTERVAL    15 seconds
  ZEROCLAW_TIMEOUT      600 seconds per job
  ORCHESTRATION_TIMEOUT 3600 seconds for orchestration/meta jobs
  AGENT_MODEL           model name reported to hive
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
import re as _re
from typing import Optional

HIVE_URL = os.environ.get("HIVE_URL", "http://192.168.207.67:5005")
ZEROCLAW_BIN = os.environ.get("ZEROCLAW_BIN", "zeroclaw")
ZEROCLAW_AGENT = os.environ.get("ZEROCLAW_AGENT", "hive")
AGENT_HOST = os.environ.get("AGENT_HOST", socket.gethostname())
AGENT_CAPABILITIES = [
    c.strip()
    for c in os.environ.get(
        "AGENT_CAPABILITIES", "code-edit,multi-agent,delegate,python,bash,linux,orchestration"
    ).split(",")
    if c.strip()
]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
ZEROCLAW_TIMEOUT = int(os.environ.get("ZEROCLAW_TIMEOUT", "600"))
ORCHESTRATION_TIMEOUT = int(os.environ.get("ORCHESTRATION_TIMEOUT", "3600"))
WORKDIR = os.environ.get("HIVE_WORKDIR", os.getcwd())
AGENT_MODEL = os.environ.get("AGENT_MODEL", "groq/qwen3-32b")

JOB_HEARTBEAT_INTERVAL = int(os.environ.get("JOB_HEARTBEAT_INTERVAL", "300"))


def timeout_for_kind(kind: str) -> int:
    if (kind or "").lower() in ("orchestration", "orchestrate", "fan-out", "meta"):
        return ORCHESTRATION_TIMEOUT
    return ZEROCLAW_TIMEOUT


_urn: str = ""
_last_heartbeat = 0.0
_running = True


def _signal_handler(signum, frame):
    global _running
    print(f"[zc-worker] signal {signum} — shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _http(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict | None]:
    url = f"{HIVE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
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
        print(f"[zc-worker] http error {method} {path}: {e}", flush=True)
        return 0, None


def _zeroclaw_version() -> str:
    try:
        out = subprocess.run([ZEROCLAW_BIN, "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _probe_cores() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def _probe_ram_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _instance_id() -> str:
    """Return systemd instance id (1, 2, ...) or 'solo' if not under template."""
    return os.environ.get("INSTANCE", os.environ.get("ZEROCLAW_INSTANCE_ID", "solo"))


def register() -> str:
    global _urn, _last_heartbeat
    body = {
        "kind": "zeroclaw",
        "host": AGENT_HOST,
        "pid": os.getpid(),
        "capabilities": AGENT_CAPABILITIES,
        "provider": "groq",
        "model": AGENT_MODEL,
        "version": _zeroclaw_version(),
        "metadata": {
            "daemon": "zeroclaw_worker.py",
            "agent_alias": ZEROCLAW_AGENT,
            "started_at": time.time(),
            "cores": _probe_cores(),
            "ram_mb": _probe_ram_mb(),
            "instance_id": _instance_id(),
        },
    }
    code, resp = _http("POST", "/v1/agents/register", body)
    if code == 200 and resp:
        _urn = resp["urn"]
        _last_heartbeat = time.time()
        print(f"[zc-worker] registered urn={_urn}", flush=True)
        return _urn
    print(f"[zc-worker] register failed code={code} resp={resp}", flush=True)
    sys.exit(1)


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
        print(f"[zc-worker] dequeue unexpected code={code} resp={resp}", flush=True)
    return None


def update_job(job_id: str, status: str, result: dict):
    body = {"status": status, "result": result, "claimed_by": _urn}
    if isinstance(result, dict):
        t_in = result.get("tokens_in")
        t_out = result.get("tokens_out")
        if t_in is not None:
            body["tokens_in"] = int(t_in)
        if t_out is not None:
            body["tokens_out"] = int(t_out)
    _http("PATCH", f"/v1/jobs/{job_id}", body)


ERR_PATTERNS = [
    ("rate_limit", "rate limit exceeded"),
    ("auth_error", "authentication error"),
    ("auth_failed", "authentication failed"),
    ("context_overflow", "context length"),
    ("model_not_found", "model not found"),
    ("upstream_500", "internalservererror"),
]


def detect_error(stdout: str) -> Optional[str]:
    if not stdout:
        return None
    s = stdout.lower()
    for tag, needle in ERR_PATTERNS:
        if needle in s:
            return tag
    return None


_TOKEN_PAT = _re.compile(r"\[tokens?:\s*(\d+)(?:\s*[/+]\s*(\d+))?", _re.I)
_USAGE_PAT = _re.compile(
    r"(?:prompt[_ ]?tokens|input[_ ]?tokens)[:\s=]+(\d+).*?" r"(?:completion[_ ]?tokens|output[_ ]?tokens)[:\s=]+(\d+)",
    _re.I | _re.S,
)


def parse_tokens(stdout: str, stderr: str = "") -> tuple[int, int]:
    text = (stdout or "") + "\n" + (stderr or "")
    m = _TOKEN_PAT.search(text)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else 0
        return (a, b) if b else (0, a)
    m = _USAGE_PAT.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def run_zeroclaw(description: str, kind: str = "", job_heartbeat_fn=None) -> dict:
    # zeroclaw agent -a <alias> -m "<description>" --no-session
    cmd = [ZEROCLAW_BIN, "agent", "-a", ZEROCLAW_AGENT, "-m", description]
    print(f"[zc-worker] $ {' '.join(cmd[:5])} … [desc len={len(description)}]", flush=True)
    start = time.time()
    timeout = timeout_for_kind(kind)
    try:
        proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        last_job_hb = time.time()
        while True:
            try:
                proc.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                if elapsed >= timeout:
                    proc.kill()
                    proc.wait()
                    return {
                        "exit_code": -1,
                        "error": f"timeout {timeout}s exceeded",
                        "duration_sec": round(elapsed, 1),
                        "workdir": WORKDIR,
                    }
                if job_heartbeat_fn and (time.time() - last_job_hb) >= JOB_HEARTBEAT_INTERVAL:
                    try:
                        job_heartbeat_fn(round(elapsed, 1))
                    except Exception:
                        pass
                    last_job_hb = time.time()

        stdout, stderr = proc.stdout.read(), proc.stderr.read()
        err_tag = detect_error(stdout)
        t_in, t_out = parse_tokens(stdout, stderr)
        if t_in == 0 and t_out == 0:
            t_in = max(1, len(description) // 4)
            t_out = max(1, len(stdout) // 4)
        result = {
            "exit_code": proc.returncode,
            "stdout": stdout[-12000:],
            "stderr": stderr[-4000:],
            "duration_sec": round(time.time() - start, 1),
            "zeroclaw_cmd": " ".join(cmd[:5]),
            "agent_alias": ZEROCLAW_AGENT,
            "tokens_in": t_in,
            "tokens_out": t_out,
            "workdir": WORKDIR,
        }
        if err_tag:
            result["exit_code"] = 1
            result["worker_error"] = err_tag
        return result
    except Exception as e:
        return {
            "exit_code": -1,
            "error": f"{type(e).__name__}: {e}",
            "workdir": WORKDIR,
        }


def main():
    register()
    backoff = 1.0
    while _running:
        heartbeat()
        job = claim_next_job()
        if not job:
            time.sleep(min(backoff, POLL_INTERVAL) + random.uniform(0, 2))
            backoff = min(backoff * 1.5, POLL_INTERVAL)
            continue
        backoff = 1.0
        print(f"[zc-worker] claimed job {job['id'][:8]} kind={job['kind']} priority={job.get('priority')}", flush=True)
        update_job(job["id"], "running", {"started_by": _urn, "started_at": time.time()})
        job_id = job["id"]

        def _job_hb(elapsed):
            update_job(job_id, "running", {"heartbeat_at": time.time(), "elapsed_sec": elapsed})

        result = run_zeroclaw(
            job.get("description") or job.get("kind", ""), job.get("kind", ""), job_heartbeat_fn=_job_hb
        )
        status = "done" if result.get("exit_code") == 0 else "failed"
        result["finished_at"] = time.time()
        update_job(job["id"], status, result)
        print(
            f"[zc-worker] {status} job {job['id'][:8]} exit={result.get('exit_code')} dur={result.get('duration_sec')}s",
            flush=True,
        )
    print("[zc-worker] clean shutdown", flush=True)


if __name__ == "__main__":
    main()
