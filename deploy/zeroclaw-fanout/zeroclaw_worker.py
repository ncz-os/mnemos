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
  CLAUDE_SUBSCRIPTION_TIER  claude_max_100 or claude_max_200
  CHATGPT_PLAN          chatgpt_plus, chatgpt_pro_100, chatgpt_pro_200
  CODEX_PLAN            codex_plus, codex_pro_100, codex_pro_200
  OPENAI_SUBSCRIPTION_POOLS comma-sep exact OpenAI pool aliases; include
                        openai_subscription only when intentionally pooling
                        ChatGPT and Codex capacity together
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
from pathlib import Path
from typing import Optional

HIVE_URL = os.environ.get("HIVE_URL", "http://192.168.207.67:5005")
ZEROCLAW_BIN = os.environ.get("ZEROCLAW_BIN", "zeroclaw")
ZEROCLAW_AGENT = os.environ.get("ZEROCLAW_AGENT", "hive")
AGENT_HOST = os.environ.get("AGENT_HOST", socket.gethostname())
_DEFAULT_CAPABILITIES = ",".join(
    [
        # Core skills
        "code-edit",
        "code-review",
        "code-fix",
        "multi-agent",
        "delegate",
        "orchestration",
        "investigation",
        "refactor",
        "verify",
        "benchmark",
        "docs",
        "architecture",
        "migration",
        "deploy",
        # Languages
        "python",
        "bash",
        "rust",
        "typescript",
        "javascript",
        "sql",
        "yaml",
        "toml",
        "markdown",
        "shell",
        # OS / runtime
        "linux",
        "docker",
        "podman",
        "quadlet",
        "systemd",
        "ssh",
        "nfs",
        "cron",
        "git",
        "gitlab",
        "github",
        # Databases
        "sqlite",
        "postgresql",
        "postgres",
        "oracle",
        "db2",
        "redis",
        # Project domains
        "investorclaw",
        "investorclade",
        "ic-engine",
        "mnemos",
        "graeae",
        "riskyeats",
        "calliope",
        "cixmini-os",
        "ncz-os",
        "fleet-infra",
        "llm-tooling",
        "etlantis",
        "mayaferries",
        "zeroclaw",
        "openclaw",
        "hermes",
        "claude-code-cli",
        "testing",
        "npu",
        "yocto",
    ]
)
AGENT_CAPABILITIES = [
    c.strip() for c in os.environ.get("AGENT_CAPABILITIES", _DEFAULT_CAPABILITIES).split(",") if c.strip()
]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
ZEROCLAW_TIMEOUT = int(os.environ.get("ZEROCLAW_TIMEOUT", "600"))
# Per-provider attempt timeout (separate from total job timeout). Stops hung
# providers (e.g. Groq 8b under TPM cap returning empty for 10 min) from
# burning the full ZEROCLAW_TIMEOUT before advancing chain.
PER_ATTEMPT_TIMEOUT = int(os.environ.get("PER_ATTEMPT_TIMEOUT", "1200"))
ORCHESTRATION_TIMEOUT = int(os.environ.get("ORCHESTRATION_TIMEOUT", "3600"))
WORKDIR = os.environ.get("HIVE_WORKDIR", os.getcwd())
_AGENT_MODEL_RAW = os.environ.get("AGENT_MODEL", "groq/qwen3-32b")
AGENT_PROVIDER = os.environ.get(
    "AGENT_PROVIDER",
    _AGENT_MODEL_RAW.split("/", 1)[0] if "/" in _AGENT_MODEL_RAW else "groq",
).lower()
AGENT_MODEL = os.environ.get(
    "AGENT_MODEL_ID",
    _AGENT_MODEL_RAW.split("/", 1)[1] if "/" in _AGENT_MODEL_RAW else _AGENT_MODEL_RAW,
).lower()


def _model_capability(provider: str, model: str) -> str:
    safe = _re.sub(r"[^a-z0-9]+", "_", f"{provider}_{model}".strip().lower()).strip("_")
    return f"model:{safe}" if safe else "model:unknown"


for _cap in ("coding", _model_capability(AGENT_PROVIDER, AGENT_MODEL)):
    if _cap not in AGENT_CAPABILITIES:
        AGENT_CAPABILITIES.append(_cap)

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


def _pool_slug(value: str) -> str:
    return _re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _add_plan_aliases(pools: set[str], provider: str, value: str, family: str | None = None) -> None:
    raw = _pool_slug(value)
    if not raw:
        return
    pools.add(raw)
    if provider == "anthropic":
        pools.add("anthropic_subscription")
        pools.add("claude_subscription")
        if "200" in raw:
            pools.add("claude_max_200")
        elif "100" in raw:
            pools.add("claude_max_100")
        elif "max" in raw:
            pools.add("claude_max_100")
    elif provider == "openai":
        if raw == "openai_subscription" or family == "openai":
            pools.add("openai_subscription")
            return
        is_codex = family == "codex" or raw.startswith("codex") or "_codex" in raw
        is_chatgpt = family == "chatgpt" or raw.startswith("chatgpt") or "gpt" in raw
        if is_codex:
            pools.add("codex_subscription")
            if "plus" in raw:
                pools.add("codex_plus")
            elif "pro" in raw:
                if "200" in raw:
                    pools.add("codex_pro_200")
                elif "100" in raw:
                    pools.add("codex_pro_100")
        if is_chatgpt:
            pools.add("chatgpt_subscription")
            if "pro" in raw:
                pools.add("chatgpt_pro")
                if "100" in raw:
                    pools.add("chatgpt_pro_100")
                elif "200" in raw:
                    pools.add("chatgpt_pro_200")
            elif "plus" in raw:
                pools.add("chatgpt_plus")


def _scan_subscription_config(path: Path, pools: set[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return
    for pool in (
        "claude_max_200",
        "claude_max_100",
        "chatgpt_plus",
        "chatgpt_pro",
        "chatgpt_pro_100",
        "chatgpt_pro_200",
        "chatgpt_subscription",
        "openai_subscription",
        "anthropic_subscription",
        "codex_plus",
        "codex_pro_100",
        "codex_pro_200",
        "codex_subscription",
    ):
        if pool in text.replace("-", "_"):
            pools.add(pool)


def _detect_subscription_pools() -> list[str]:
    pools: set[str] = set()
    home = Path.home()

    for env_name in ("CLAUDE_SUBSCRIPTION_TIER",):
        if os.environ.get(env_name):
            _add_plan_aliases(pools, "anthropic", os.environ[env_name])
    if os.environ.get("CHATGPT_PLAN"):
        _add_plan_aliases(pools, "openai", os.environ["CHATGPT_PLAN"], family="chatgpt")
    if os.environ.get("CODEX_PLAN"):
        _add_plan_aliases(pools, "openai", os.environ["CODEX_PLAN"], family="codex")
    for pool in os.environ.get("OPENAI_SUBSCRIPTION_POOLS", "").split(","):
        if pool.strip():
            _add_plan_aliases(
                pools, "openai", pool, family="openai" if _pool_slug(pool) == "openai_subscription" else None
            )

    for config_path in (home / ".claude" / "config.toml", home / ".codex" / "config.toml"):
        _scan_subscription_config(config_path, pools)

    if (home / ".anthropic" / "auth.json").exists():
        pools.update({"anthropic_subscription", "claude_subscription"})

    return sorted(pools)


def register() -> str:
    """Register with hive. Retry with exponential backoff until success.
    Never returns failure — workers cannot operate without a URN."""
    global _urn, _last_heartbeat
    body = {
        "kind": "zeroclaw",
        "host": AGENT_HOST,
        "pid": os.getpid(),
        "capabilities": AGENT_CAPABILITIES,
        "provider": AGENT_PROVIDER,
        "model": AGENT_MODEL,
        "version": _zeroclaw_version(),
        "subscription_pools": _detect_subscription_pools(),
        "metadata": {
            "daemon": "zeroclaw_worker.py",
            "agent_alias": ZEROCLAW_AGENT,
            "started_at": time.time(),
            "cores": _probe_cores(),
            "ram_mb": _probe_ram_mb(),
            "instance_id": _instance_id(),
        },
    }
    attempt = 0
    backoff = 5.0
    while _running:
        attempt += 1
        code, resp = _http("POST", "/v1/agents/register", body)
        if code == 200 and resp and resp.get("urn"):
            _urn = resp["urn"]
            _last_heartbeat = time.time()
            print(f"[zc-worker] registered urn={_urn} (attempt {attempt})", flush=True)
            return _urn
        print(
            f"[zc-worker] register attempt={attempt} failed code={code} resp={str(resp)[:200]} — retry in {backoff:.0f}s",
            flush=True,
        )
        # Sleep in 1s chunks so SIGTERM is responsive
        slept = 0.0
        while slept < backoff and _running:
            time.sleep(1.0)
            slept += 1.0
        backoff = min(backoff * 1.5, 60.0)  # cap 60s
    # _running flipped (SIGTERM) — exit cleanly
    print("[zc-worker] shutting down before register success", flush=True)
    sys.exit(0)


def heartbeat():
    """Send heartbeat. If URN unknown (404), re-register."""
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL:
        return
    code, resp = _http("POST", "/v1/agents/heartbeat", {"urn": _urn})
    _last_heartbeat = time.time()
    # If hive lost our URN (404 or "not found"), re-register
    if code == 404 or (code in (400, 410) and resp and "not found" in str(resp).lower()):
        print(f"[zc-worker] heartbeat 404 — URN expired, re-registering", flush=True)
        register()


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
# Per-instance aliases preserve workspace isolation across concurrent workers.
_INST = os.environ.get("ZEROCLAW_INSTANCE_ID", os.environ.get("INSTANCE", "1"))
_PRIMARY_BC = os.environ.get("ZC_TIER_BC_AGENT", f"hive_nvidia_{_INST}")
_FALLBACK_TOGETHER = os.environ.get("ZC_FALLBACK_TOGETHER", f"hive_together_{_INST}")


# Fleet LLM registry — single source of truth at NFS path, mutates as we
# discover new providers / pricing. Workers consult on startup + on each
# rate-limit pivot. Falls back to hard-coded chain if registry unreachable.
_REGISTRY_PATH = os.environ.get(
    "LLM_REGISTRY_PATH",
    "/mnt/argonas/datapool/projects/fleet-registry/llm_provider_registry.json",
)
_REGISTRY_CACHE = {"loaded_at": 0, "data": None}


def _load_registry(max_age_s: int = 300):
    """Return registry dict or None if unavailable. Cached 5 min."""
    now = time.time()
    if _REGISTRY_CACHE["data"] and (now - _REGISTRY_CACHE["loaded_at"]) < max_age_s:
        return _REGISTRY_CACHE["data"]
    try:
        with open(_REGISTRY_PATH) as f:
            data = json.load(f)
        _REGISTRY_CACHE["data"] = data
        _REGISTRY_CACHE["loaded_at"] = now
        return data
    except Exception:
        return _REGISTRY_CACHE["data"]  # stale OK, or None


def _registry_chain_for_tier(tier: str) -> list:
    """Map registry tier_picks → zeroclaw agent aliases. Returns empty list if no registry."""
    reg = _load_registry()
    if not reg:
        return []
    # Map our internal A/B/C tiers to registry tier-pick keys
    tier_map = {
        "A": ["C_g1_critical", "B_quality_surgical"],  # premium first
        "B": ["B_fanout_high_volume", "S_free_default"],
        "C": ["S_free_default", "B_fanout_high_volume"],
    }
    pick_keys = tier_map.get(tier.upper(), tier_map["C"])
    alias_chain = []
    for pk in pick_keys:
        for entry in reg.get("tier_picks", {}).get(pk, []):
            # entry format "provider.model" — map back to zeroclaw agent alias
            prov, _ = entry.split(".", 1) if "." in entry else (entry, "")
            alias = _provider_to_alias(prov)
            if alias and alias not in alias_chain:
                alias_chain.append(alias)
    return alias_chain


def _provider_to_alias(prov: str) -> str:
    """Map registry provider name → zeroclaw agent alias in v3.toml config."""
    mapping = {
        "ngc_integrate": _PRIMARY_BC,  # per-instance hive_nvidia_<N>
        "ngc_inference": _PRIMARY_BC,
        "groq": "hive_groq_8b",
        "deepseek_direct": f"hive_deepseek_{_INST}",
        "siliconflow": f"hive_siliconflow_{_INST}",
        "together": _FALLBACK_TOGETHER,
        "xai": "hive_xai",
        "openai": "hive_openai",
        "anthropic": "hive_anthropic",
        "gemini": "hive_gemini",
    }
    return mapping.get(prov, "")


def _build_tier_chain(tier: str) -> list:
    """Build chain for a tier. Prefer registry; fall back to hard-coded."""
    reg_chain = _registry_chain_for_tier(tier)
    if reg_chain:
        return reg_chain
    # Hard-coded fallback
    return _HARDCODED_FALLBACK[tier.upper()]


_FALLBACK_DEEPSEEK = os.environ.get("ZC_FALLBACK_DEEPSEEK", f"hive_deepseek_{_INST}")
_FALLBACK_SILICONFLOW = os.environ.get("ZC_FALLBACK_SILICONFLOW", f"hive_siliconflow_{_INST}")

# Fallback chain optimized 2026-05-25:
# 1. FREE first (NGC NIM kimi-k2.6) — rate-limit-prone but free
# 2. Cheap paid Groq 8b-instant — fast, but TPM-capped
# 3. **DeepSeek V4-Flash direct** ($0.14/$0.28, 2500 concur) — funded $50, reliable
# 4. Together MiniMax-M2.7 ($0.40/$1.20) — last paid resort
# 5. xAI Grok 4.1 Fast / OpenAI — emergency
_HARDCODED_FALLBACK = {
    "A": [
        os.environ.get("ZC_TIER_A_AGENT", "hive_anthropic"),
        "hive_openai",
        "hive_gemini",
        _FALLBACK_DEEPSEEK,
        _FALLBACK_TOGETHER,
    ],
    "B": [
        _PRIMARY_BC,
        _FALLBACK_DEEPSEEK,
        "hive_groq_8b",
        _FALLBACK_TOGETHER,
        _FALLBACK_SILICONFLOW,
        "hive_xai",
        "hive_openai",
    ],
    "C": [
        _PRIMARY_BC,
        _FALLBACK_DEEPSEEK,
        "hive_groq_8b",
        _FALLBACK_TOGETHER,
    ],
}


class _ChainProxy(dict):
    """Lazy dict that calls _build_tier_chain on .get/__getitem__ so registry
    updates between job runs reflect immediately without restart."""

    def get(self, key, default=None):
        chain = _build_tier_chain(key)
        return chain if chain else default

    def __getitem__(self, key):
        chain = _build_tier_chain(key)
        if not chain:
            raise KeyError(key)
        return chain


TIER_FALLBACK_CHAIN = _ChainProxy()

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
    # Together-specific (2026-05-25 — Together MiniMax M2.7 was failing "non-retryable"):
    "model_not_available",
    "context_length",
    "max_tokens",
    "model_invalid",
    "Bad Request",
    "400 Bad Request",
    "internal_server_error",
    "500 Internal Server Error",
    "ECONNRESET",
    "ETIMEDOUT",
    "Timed out",
    "Connection reset",
    "stream interrupted",
    "no_completion",
    # NGC-specific
    "gateway timeout",
    "504",
    "model is currently being loaded",
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


def run_zeroclaw(
    description: str, kind: str = "", job_id: str = "", job_heartbeat_fn=None, max_cost_tier: str = "C"
) -> dict:
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
        print(
            f"[zc-worker] $ {' '.join(cmd[:4])} … [desc len={len(description)}] cwd={exec_cwd} (attempt {attempt_idx+1}/{len(chain)})",
            flush=True,
        )
        attempt_start = time.time()
        try:
            # Pass full env so subprocess sees HOME, PATH, API keys, XDG_*, etc.
            # Without env=, Popen uses parent process env which under systemd may
            # be sparse if EnvironmentFile didn't set everything.
            child_env = os.environ.copy()
            # Ensure essential paths
            child_env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            child_env.setdefault("HOME", os.path.expanduser("~"))
            proc = subprocess.Popen(
                cmd, cwd=exec_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env
            )
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
                print(
                    f"[zc-worker] success on provider={agent_alias} attempt={attempt_idx+1} dur={time.time()-attempt_start:.1f}s",
                    flush=True,
                )
                break

            # Non-zero exit — decide retry or give up
            if _is_rate_limited(last_stderr, last_stdout):
                print(
                    f"[zc-worker] rate-limited on {agent_alias} ({time.time()-attempt_start:.1f}s), advancing chain",
                    flush=True,
                )
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
