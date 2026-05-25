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


# ────────────────────────────────────────────────────────────
# Workspace + git lifecycle (code-executing mode)
# ────────────────────────────────────────────────────────────

GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "Jason Perlow")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "jperlow@gmail.com")
HIVE_WORK_ROOT = os.path.expanduser(os.environ.get("HIVE_WORK_ROOT", "~/hive-work"))

# Directive format embedded at start of job description:
#   [repo:<URL> branch:<branch> base:<base-ref>]
# Branch + base optional. base defaults to "main"; branch defaults to
# `hive/<job-id-short>` (auto-derived per job).
_REPO_DIRECTIVE_RE = _re.compile(
    r"^\s*\[repo:(?P<url>\S+?)(?:\s+branch:(?P<branch>\S+?))?(?:\s+base:(?P<base>\S+?))?\s*\]\s*",
    _re.IGNORECASE,
)


def _parse_repo_directive(description: str) -> Optional[dict]:
    """Return {url, branch, base} dict if description starts with a
    `[repo:...]` directive; else None."""
    if not description:
        return None
    m = _REPO_DIRECTIVE_RE.match(description)
    if not m:
        return None
    return {
        "url": m.group("url"),
        "branch": m.group("branch"),
        "base": m.group("base") or "main",
        "stripped_description": _REPO_DIRECTIVE_RE.sub("", description, count=1).strip(),
    }


def _git(workspace: str, *args, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in workspace; capture stdout/stderr; raise on check=True
    and non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=check,
    )


def _prepare_workspace(job_id: str, repo: dict, agent_alias: str = "") -> Optional[str]:
    """Clone repo + checkout branch into the AGENT'S workspace.

    zeroclaw 0.8 file_write/shell tools are jailed to the agent's
    configured workspace (default `~/.zeroclaw/agents/<alias>/workspace`).
    Cloning the repo INTO that path lets the agent edit files directly.

    Per-instance isolation: workspace path includes INSTANCE id so
    multiple workers on same alias don't collide.

    Returns workspace path on success; None on failure.
    """
    short = job_id[:8]
    alias = agent_alias or os.environ.get("ZEROCLAW_AGENT", "hive")
    instance = os.environ.get("ZEROCLAW_INSTANCE_ID", os.environ.get("INSTANCE", "1"))
    # Agent's workspace root — same path zeroclaw's file_write tool writes to
    workspace = os.path.expanduser(f"~/.zeroclaw/agents/{alias}/workspace")
    try:
        # Wipe + re-create
        if os.path.exists(workspace):
            subprocess.run(["rm", "-rf", workspace], check=False)
        os.makedirs(os.path.dirname(workspace), exist_ok=True)
        # Shallow clone (depth=10 for diff context)
        r = subprocess.run(
            ["git", "clone", "--depth=10", "--branch", repo["base"], repo["url"], workspace],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode != 0:
            print(f"[zc-worker] clone failed: {r.stderr[:400]}", flush=True)
            return None
        # Set per-workspace git identity
        _git(workspace, "config", "user.name", GIT_USER_NAME)
        _git(workspace, "config", "user.email", GIT_USER_EMAIL)
        # Determine working branch
        branch = repo.get("branch") or f"hive/{short}"
        # Try checkout existing OR create from base
        check = subprocess.run(
            ["git", "ls-remote", "--heads", repo["url"], branch],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.stdout.strip():
            # Branch exists upstream — fetch + checkout
            _git(workspace, "fetch", "origin", branch, check=False)
            _git(workspace, "checkout", "-B", branch, f"origin/{branch}", check=False)
        else:
            _git(workspace, "checkout", "-B", branch, f"origin/{repo['base']}", check=False)
        print(f"[zc-worker] workspace ready: {workspace} branch={branch}", flush=True)
        return workspace
    except Exception as exc:
        print(f"[zc-worker] workspace prep failed: {type(exc).__name__}: {exc}", flush=True)
        return None


def _capture_changes(workspace: str) -> dict:
    """Inspect workspace for uncommitted changes + return summary."""
    try:
        st = _git(workspace, "status", "--porcelain", check=False)
        lines = [ln for ln in st.stdout.split("\n") if ln.strip()]
        files = sorted({ln[3:].strip() for ln in lines if len(ln) > 3})
        diffstat = _git(workspace, "diff", "HEAD", "--stat", check=False)
        return {"files_changed": files, "diffstat": diffstat.stdout[-4000:]}
    except Exception as exc:
        return {"files_changed": [], "diffstat": f"err: {exc}"}


def _commit_and_push(workspace: str, job_id: str, kind: str, branch: str, base_ref: str = "main") -> list[str]:
    """Stage uncommitted changes + commit + capture any prior commits
    made by the agent, then push them all to origin/<branch>.

    The agent may have already done git add+commit inside its workspace,
    so we collect all commits ahead of origin/<base_ref> (the pre-job
    starting point) as job output."""
    short = job_id[:8]
    commits: list[str] = []
    try:
        # 1. Stage + commit any leftover uncommitted changes (catches file_write w/o git)
        _git(workspace, "add", "-A")
        st = _git(workspace, "status", "--porcelain", check=False)
        if st.stdout.strip():
            msg = f"hive[{kind}]: job-{short} auto-commit\n\nGenerated by zeroclaw worker {os.environ.get('AGENT_HOST', socket.gethostname())}\nJob-Id: {job_id}\n"
            _git(workspace, "commit", "-m", msg, check=False)

        # 2. Collect ALL commits on this branch beyond origin/<base_ref>
        log = _git(workspace, "log", f"origin/{base_ref}..HEAD", "--pretty=format:%H", check=False)
        if log.stdout.strip():
            commits = [c for c in log.stdout.strip().split("\n") if c]
        else:
            # No new commits (agent did nothing)
            return []

        # 3. Push the branch
        push = subprocess.run(
            ["git", "push", "-u", "origin", f"HEAD:{branch}"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if push.returncode != 0:
            print(f"[zc-worker] push failed: {push.stderr[:400]}", flush=True)
        else:
            print(f"[zc-worker] pushed {len(commits)} commit(s) to origin/{branch}: {commits[0][:10]}", flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"[zc-worker] commit/push exc: {exc.stderr[:200] if exc.stderr else exc}", flush=True)
    return commits


def _cleanup_workspace(workspace: str) -> None:
    try:
        subprocess.run(["rm", "-rf", workspace], check=False)
    except Exception:
        pass


TIER_PROVIDER_MAP = {
    "A": os.environ.get("ZC_TIER_A_PROVIDER", "anthropic.opus_4_6"),
    "B": os.environ.get("ZC_TIER_B_PROVIDER", "groq.default"),
    "C": os.environ.get("ZC_TIER_C_PROVIDER", "nvidia.kimi"),
}

# Fallback chains per tier. Each entry = agent ALIAS (NOT provider type).
# zeroclaw 0.8 binds api_key + model + endpoint to agent's model_provider field,
# so we switch -a <alias> per attempt instead of --provider override (which
# only changes endpoint URL, retains agent's key — wrong key for new endpoint).
# Together MiniMax M2.7 = fleet primary. High concurrency, no Groq-style
# 300k TPM ceiling for 39-worker fan-out. Per-instance aliases preserve
# workspace isolation across concurrent workers.
_INST = os.environ.get("ZEROCLAW_INSTANCE_ID", os.environ.get("INSTANCE", "1"))
_PRIMARY_B = os.environ.get("ZC_TIER_B_AGENT", f"hive_together_{_INST}")
TIER_FALLBACK_CHAIN = {
    "A": [
        os.environ.get("ZC_TIER_A_AGENT", "hive_anthropic"),
        "hive_openai",
        "hive_gemini",
        "hive_together",
    ],
    "B": [
        _PRIMARY_B,
        "hive_groq",
        "hive_xai",
        "hive_openai",
        "hive_gemini",
    ],
    "C": [
        os.environ.get("ZC_TIER_C_AGENT", "hive_nvidia"),
        _PRIMARY_B,
        "hive_xai",
    ],
}

# Patterns indicating need to advance to next provider in chain.
# These are all retryable / transient at provider level.
RATE_LIMIT_PATTERNS = [
    "rate_limited",
    "rate_limit_exceeded",
    "429 Too Many Requests",
    "tokens per minute",
    "TPM",
    "quota_exceeded",
    "insufficient_quota",
    "overloaded",
    "service_unavailable",
    "503 Service Unavailable",
    "Resource has been exhausted",
    "All model_providers/models failed",
    "rate limit",
    "RATE_LIMIT",
]


def _is_rate_limited(stderr: str, stdout: str = "") -> bool:
    """Return True if zeroclaw failure indicates provider exhaustion."""
    blob = (stderr or "") + (stdout or "")
    blob_lower = blob.lower()
    for p in RATE_LIMIT_PATTERNS:
        if p.lower() in blob_lower:
            return True
    return False


def run_zeroclaw(description: str, kind: str = "", job_id: str = "", job_heartbeat_fn=None, max_cost_tier: str = "C") -> dict:
    """Execute one job. If description starts with [repo:...] directive,
    clone + checkout + run agent in workspace + commit/push changes.
    Otherwise chat-only mode (current behavior).

    --provider override is ALWAYS passed (per-tier) to bypass zeroclaw 0.8
    `[agents.<alias>] model_provider` dangling-reference validation; CLI
    override resolves correctly even when config validate flags it."""
    repo = _parse_repo_directive(description)
    workspace = None
    branch = None
    if repo:
        # Workspace path is per-agent-alias; use first chain entry (final_provider may
        # change on rate-limit but workspace is shared across all chain attempts)
        first_alias = TIER_FALLBACK_CHAIN.get((max_cost_tier or "C").upper(), TIER_FALLBACK_CHAIN["C"])[0]
        workspace = _prepare_workspace(job_id or "anon", repo, agent_alias=first_alias)
        if workspace:
            branch = repo.get("branch") or f"hive/{(job_id or 'anon')[:8]}"
            description = repo["stripped_description"] or description
    exec_cwd = workspace or WORKDIR

    tier = (max_cost_tier or "C").upper()
    chain = TIER_FALLBACK_CHAIN.get(tier, TIER_FALLBACK_CHAIN["C"])
    start = time.time()
    timeout = timeout_for_kind(kind)
    last_stderr = ""
    last_stdout = ""
    providers_attempted = []
    final_provider = chain[0]
    proc = None

    for attempt_idx, agent_alias in enumerate(chain):
        final_provider = agent_alias
        providers_attempted.append(agent_alias)
        # Switch -a <agent_alias> per attempt; each agent binds to single
        # provider type via its model_provider field. Avoids 0.8 bug where
        # --provider override keeps old agent's api_key.
        cmd = [ZEROCLAW_BIN, "agent", "-a", agent_alias, "-m", description]
        print(f"[zc-worker] $ {' '.join(cmd[:4])} … [desc len={len(description)}] cwd={exec_cwd} (attempt {attempt_idx+1}/{len(chain)})", flush=True)
        attempt_start = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=exec_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
                        if workspace:
                            _cleanup_workspace(workspace)
                        return {
                            "exit_code": -1,
                            "error": f"timeout {timeout}s exceeded (provider={agent_alias})",
                            "duration_sec": round(elapsed, 1),
                            "workdir": exec_cwd,
                            "providers_attempted": providers_attempted,
                        }
                    if job_heartbeat_fn and (time.time() - last_job_hb) >= JOB_HEARTBEAT_INTERVAL:
                        try:
                            job_heartbeat_fn(round(elapsed, 1))
                        except Exception:
                            pass
                        last_job_hb = time.time()

            last_stdout = proc.stdout.read()
            last_stderr = proc.stderr.read()

            # Success → break out of fallback loop
            if proc.returncode == 0:
                print(f"[zc-worker] success on provider={agent_alias} attempt={attempt_idx+1} dur={time.time()-attempt_start:.1f}s", flush=True)
                break

            # Non-zero exit — decide retry or give up
            if _is_rate_limited(last_stderr, last_stdout):
                print(f"[zc-worker] rate-limited on {agent_alias} ({time.time()-attempt_start:.1f}s), advancing chain", flush=True)
                # Continue to next provider in chain
                continue
            else:
                # Non-retryable failure — break + report
                print(f"[zc-worker] non-retryable failure on {agent_alias} exit={proc.returncode}", flush=True)
                break
        except Exception as exc:
            last_stderr = f"{type(exc).__name__}: {exc}"
            print(f"[zc-worker] exc on {agent_alias}: {last_stderr[:200]}", flush=True)
            # Spawn exception is treated as retryable
            continue

    # Build result from last attempt
    err_tag = detect_error(last_stdout)
    t_in, t_out = parse_tokens(last_stdout, last_stderr)
    if t_in == 0 and t_out == 0:
        t_in = max(1, len(description) // 4)
        t_out = max(1, len(last_stdout) // 4)
    result = {
        "exit_code": proc.returncode if proc else -1,
        "stdout": last_stdout[-12000:],
        "stderr": last_stderr[-4000:],
        "duration_sec": round(time.time() - start, 1),
        "zeroclaw_cmd": f"{ZEROCLAW_BIN} agent -a {final_provider}",
        "agent_alias": final_provider,
        "providers_attempted": providers_attempted,
        "tokens_in": t_in,
        "tokens_out": t_out,
        "workdir": exec_cwd,
    }
    if err_tag:
        result["exit_code"] = 1
        result["worker_error"] = err_tag

    # Post-agent: capture + commit/push changes if workspace exists
    try:
        if workspace and proc and proc.returncode == 0:
            changes = _capture_changes(workspace)
            result["files_changed"] = changes["files_changed"]
            result["diffstat"] = changes["diffstat"]
            commits = _commit_and_push(workspace, job_id or "anon", kind, branch, base_ref=repo.get("base") or "main")
            result["commits"] = commits
            if commits:
                result["pushed_branch"] = branch
                result["repo_url"] = repo["url"]
    except Exception as e:
        result["post_agent_error"] = f"{type(e).__name__}: {e}"
    finally:
        if workspace:
            _cleanup_workspace(workspace)
    return result


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
            job.get("description") or job.get("kind", ""),
            job.get("kind", ""),
            job_id=job["id"],
            job_heartbeat_fn=_job_hb,
            max_cost_tier=job.get("max_cost_tier") or job.get("cost_tier") or "B",
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
