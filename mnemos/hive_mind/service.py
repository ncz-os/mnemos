"""
GRAEAE Hive Mind — fleet-wide MCP-compatible agent coordination + triage queue.

Brand: GRAEAE Hive Mind (extension of GRAEAE consensus engine — sister to mnemos memory)
Identity: urn:agent:<kind>:<host>:<session_uuid>   (kinds: claude, goose, opencode, codex, zeroclaw, openclaw, hermes, human, ic-engine, mnemos, ...)
Backend: SQLite WAL (Phase 1) — PG migration documented for Phase 2 when >50 agents OR LISTEN/NOTIFY needed
Pub/sub: SSE (Phase 1) — NATS migration Phase 2 when >20 concurrent OR >100 msg/s
Triage queue: /v1/jobs/next dequeues highest-priority eligible work; no central scheduler — agents self-claim.
"""
from __future__ import annotations
import asyncio
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

DB_PATH = os.environ.get("AGENT_BUS_DB", "/srv/agent-bus/agents.db")

# Phase 2 migration cut 1 (2026-05-23): storage abstraction. SQL methods
# migrate into the repo one at a time; service-level helpers forward.
# See mnemos/hive_mind/repository.py for the Protocol + cut plan.
from mnemos.hive_mind.repository import SqliteHiveMindRepository  # noqa: E402
_REPO = SqliteHiveMindRepository(DB_PATH)
HEARTBEAT_REAP_INTERVAL = 30.0
HEARTBEAT_DEAD_AFTER = 60.0
EVENTS_RETAIN_HOURS = 168  # 7 days
SSE_PING_INTERVAL = 15.0
EVENT_QUEUE: dict[str, asyncio.Queue] = {}  # subscriber_id -> queue


# ---------- helpers ----------

def uuidv7() -> str:
    """Time-ordered UUID for monotonic index inserts."""
    ts_ms = int(time.time() * 1000)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    val = (
        (ts_ms & ((1 << 48) - 1)) << 80
        | (0x7 << 76)
        | (rand_a << 64)
        | (0x2 << 62)
        | rand_b
    )
    return str(uuid.UUID(int=val))


def make_urn(kind: str, host: str, session_id: str) -> str:
    return f"urn:agent:{kind}:{host}:{session_id}"


async def emit_event(db, kind: str, payload: dict) -> None:
    ts = time.time()
    payload_json = json.dumps(payload, separators=(",", ":"))
    await db.execute(
        "INSERT INTO events (ts, kind, payload, agent_urn) VALUES (?, ?, ?, ?)",
        (ts, kind, payload_json, payload.get("urn") or payload.get("agent_urn")),
    )
    await db.commit()
    # broadcast to live SSE subscribers
    for q in list(EVENT_QUEUE.values()):
        try:
            q.put_nowait({"kind": kind, "ts": ts, "payload": payload})
        except asyncio.QueueFull:
            pass


# ---------- schema ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  urn TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  host TEXT NOT NULL,
  session_id TEXT NOT NULL,
  pid INTEGER,
  capabilities TEXT,
  version TEXT,
  started_at REAL NOT NULL,
  last_heartbeat REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('online','idle','offline','error')),
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_kind ON agents(kind);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  submitter_urn TEXT NOT NULL,                  -- who created the work (user/human OR delegating agent)
  parent_job_id TEXT,
  kind TEXT NOT NULL,                           -- code-edit/research/review/build/test/etc
  description TEXT,
  priority INTEGER NOT NULL DEFAULT 0,          -- higher = more urgent
  deadline REAL,
  required_capabilities TEXT,                   -- json array; worker must have ALL
  eligible_kinds TEXT,                          -- json array; agent kinds eligible (null = any)
  project TEXT,                                 -- #10 FIX: separate project tag from capabilities (riskyeats/investorclaw/etc)
  status TEXT NOT NULL CHECK(status IN ('queued','offered','claimed','running','done','failed','cancelled')),
  claimed_by TEXT,                              -- worker urn (set on claim/dequeue)
  claimed_at REAL,
  started_at REAL NOT NULL,                     -- when job ENTERED queue
  ended_at REAL,
  result TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_submitter ON jobs(submitter_urn);
CREATE INDEX IF NOT EXISTS idx_jobs_claimed_by ON jobs(claimed_by);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, priority DESC, started_at ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project);  -- #10 FIX

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  from_urn TEXT NOT NULL,
  to_urn TEXT,
  in_reply_to TEXT,
  topic TEXT NOT NULL,
  payload TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_urn);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  agent_urn TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_urn);
"""


# ---------- models ----------

# Runtime → eligible kinds map (Kimi-K2.6 advisory 2026-05-23).
RUNTIME_KIND_MAP: dict[str, set[str]] = {
    "claude-code":   {"claude", "claude-code"},
    "claude-cli":    {"claude", "claude-code"},
    "opencode":      {"opencode"},                 # opencode CAN run any model — kind=opencode
    "opencode-cli":  {"opencode"},
    # goose RETIRED 2026-05-25 — see ~/.claude/rules/agentic-cli-usage-patterns-2026-05-23.md
    "codex":         {"codex"},
    "codex-cli":     {"codex"},
    "hermes":        {"hermes"},
    "zeroclaw":      {"zeroclaw"},
    "openclaw":      {"openclaw"},
    "ic-engine":     {"ic-engine"},
    "mnemos":        {"mnemos"},
    "human":         {"human"},
    "claude":        {"claude"},
    "system":        {"system"},                    # fleet hosts (ARGOS/TYPHON/HYDRA/MEDUSA/CERBERUS/PROTEUS/cixmini)
    "unknown":       {"unknown"},
}
AUTONOMY_LEVELS = {"autonomous", "confirm-risky", "interactive", "unknown"}
AUTH_METHODS = {"subscription", "api", "free", "unknown"}
# Plan caps (USD/month) per CLAUDE.md:
DEFAULT_PLAN_CAPS = {
    "subscription": 200.0,   # Anthropic Max plan ($200 until 2026-05-31, $100 from 2026-06-01 — operator updates)
    "api":          1000.0,  # pay-per-token has no hard cap; treat as high ceiling for safety
    "free":         0.0,     # no cap (no cost)
    "unknown":      50.0,    # conservative
}
THROTTLE_HEADROOM = 0.85  # at >=85% of plan cap, prefer non-subscription workers for tier-B/C jobs

# ROLE SPLIT (user directive 2026-05-23, goose retired 2026-05-25):
# opencode + codex + hermes + claw-family + ic-engine + unknown = WORKERS (claim-only).
# Cannot submit jobs. Orchestrators: claude-code, human, mnemos. They submit work; workers execute it.
WORKER_ONLY_RUNTIMES: set[str] = {
    "opencode", "opencode-cli",
    # goose RETIRED 2026-05-25
    "codex", "codex-cli",
    "hermes",
    "zeroclaw", "openclaw",
    "ic-engine",
    "system",   # fleet hosts (system-watcher daemons) — sensors + optional build/ci workers, never submitters
}
ORCHESTRATOR_RUNTIMES: set[str] = {
    "claude-code", "claude-cli", "claude",
    "human",
    "mnemos",
    "unknown",
}

# COST-TIER MAP (per ~/.claude/rules/llm-usage-policy-2026-05-22.md):
#   A = FREE   — local + NGC NIM (try first, token-miser)
#   B = CHEAP  — Groq Dev tier, xAI, DeepSeek direct, Together cheap, Gemini-Flash, OpenAI-mini
#   C = RESERVE — Anthropic Opus/Sonnet, OpenAI GPT-5.5/Pro, Gemini Pro, Together DeepSeek-Pro
#                 (Together DeepSeek-V4-Pro = anti-pattern — use DeepSeek direct instead)
PROVIDER_COST_TIER: dict[str, str] = {
    "ngc":             "A",
    "nvidia":          "A",
    "nvidia-ngc":      "A",
    "local-llamacpp":  "A",
    "local-vllm":      "A",
    "ollama":          "A",
    "ollama-cerberus": "A",
    "local":           "A",

    "groq":            "B",
    "xai":             "B",
    "deepseek":        "B",
    "deepseek-direct": "B",
    "together":        "B",  # default — only MiniMax + cheap models; DO NOT default to Together DeepSeek-V4-Pro
    "gemini-flash":    "B",
    "openai-mini":     "B",
    "perplexity":      "B",

    "anthropic":       "C",
    "openai":          "C",
    "openai-pro":      "C",
    "openai-gpt55":    "C",
    "gemini":          "C",
    "gemini-pro":      "C",
    "together-pro":    "C",

    "unknown":         "C",  # treat as expensive until classified
}
COST_TIERS = ["A", "B", "C"]


def cost_tier_for(provider: str) -> str:
    return PROVIDER_COST_TIER.get((provider or "unknown").lower(), "C")


# Per-million-token rates (USD). Workers SHOULD report tokens_in/tokens_out in PATCH result;
# hive computes estimated_cost_usd. Wildcard model "*" applies to any model on that provider.
# Source: ~/.claude/rules/llm-usage-policy-2026-05-22.md
LLM_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "claude-opus-4-7"):       (5.00, 25.00),
    ("anthropic", "claude-opus-4-6"):       (5.00, 25.00),
    ("anthropic", "claude-sonnet-4-6"):     (3.00, 15.00),
    ("anthropic", "claude-sonnet-4-7"):     (3.00, 15.00),
    ("anthropic", "claude-haiku-4-5"):      (1.00,  5.00),
    ("anthropic", "*"):                     (3.00, 15.00),
    ("openai",    "gpt-5.5"):               (5.00, 30.00),
    ("openai",    "gpt-5.5-pro"):           (30.00, 180.00),
    ("openai",    "gpt-5.4-nano"):          (0.20,  1.25),
    ("openai",    "o4-mini"):               (0.55,  2.20),
    ("openai",    "o3"):                    (2.00,  8.00),
    ("openai",    "*"):                     (5.00, 30.00),
    ("xai",       "grok-4.3"):              (1.25,  2.50),
    ("xai",       "grok-4.1-fast"):         (0.20,  0.50),
    ("xai",       "grok-4.20"):             (2.00,  6.00),
    ("xai",       "*"):                     (1.25,  2.50),
    ("groq",      "llama-3.3-70b-versatile"): (0.59,  0.79),
    ("groq",      "llama-3.1-8b-instant"):  (0.05,  0.08),
    ("groq",      "llama-4-scout-17b"):     (0.11,  0.34),
    ("groq",      "qwen3-32b"):             (0.29,  0.59),
    ("groq",      "gpt-oss-120b"):          (0.15,  0.60),
    ("groq",      "gpt-oss-20b"):           (0.075, 0.30),
    ("groq",      "*"):                     (0.29,  0.59),
    ("deepseek-direct", "deepseek-v4-pro"): (0.435, 0.87),
    ("deepseek-direct", "deepseek-v4-flash"): (0.14, 0.28),
    ("deepseek-direct", "*"):               (0.435, 0.87),
    ("deepseek",        "*"):               (0.435, 0.87),
    ("together",  "minimax-m2.7"):          (0.40,  1.20),
    ("together",  "deepseek-v4-pro"):       (2.10,  4.40),
    ("together",  "kimi-k2.6"):             (1.20,  4.40),
    ("together",  "glm-3.5-90"):            (0.10,  0.15),
    ("together",  "qwen2.5-coder-32b"):     (0.05,  0.12),
    ("together",  "*"):                     (0.40,  1.20),
    ("gemini",    "gemini-2.5-flash-lite"): (0.10,  0.40),
    ("gemini",    "gemini-2.5-flash"):      (0.30,  2.50),
    ("gemini",    "gemini-3.1-pro"):        (2.00, 12.00),
    ("gemini",    "*"):                     (1.25, 10.00),
    ("perplexity","sonar"):                 (1.00,  1.00),
    ("perplexity","sonar-pro"):             (3.00, 15.00),
    ("perplexity","*"):                     (1.00,  1.00),
}


def rate_for(provider: str, model: str) -> tuple[float, float]:
    p = (provider or "unknown").lower()
    m = (model or "").lower()
    # exact match first
    if (p, m) in LLM_RATES:
        return LLM_RATES[(p, m)]
    # wildcard model on provider
    if (p, "*") in LLM_RATES:
        return LLM_RATES[(p, "*")]
    # tier A providers = free
    if cost_tier_for(p) == "A":
        return (0.0, 0.0)
    return (1.0, 5.0)  # conservative fallback


def estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    in_rate, out_rate = rate_for(provider, model)
    return round((tokens_in / 1_000_000) * in_rate + (tokens_out / 1_000_000) * out_rate, 6)


def hallucination_check(result: dict) -> Optional[str]:
    """Return reason string if result looks like an LLM token-loop / hallucination,
    else None. Saves marking-done garbage that wastes a cache entry + claims completion.

    Heuristics:
    - >40% of stdout is a single repeated word/phrase
    - stdout >1500 chars with <50 unique words (Kimi-K2.6 'extension extension extension' loop)
    - stdout contains 'Rate limit exceeded' / 'Authentication error' / 'context-length exceeded'
    """
    if not isinstance(result, dict):
        return None
    stdout = result.get("stdout") or result.get("output") or ""
    if not isinstance(stdout, str) or len(stdout) < 200:
        return None
    if "rate limit exceeded" in stdout.lower():
        return "rate_limit_in_output"
    if "authentication error" in stdout.lower() or "authentication failed" in stdout.lower():
        return "auth_error_in_output"
    if "context length" in stdout.lower() and "exceed" in stdout.lower():
        return "context_overflow_in_output"
    # token-loop detection
    words = stdout.split()
    if len(words) > 200:
        from collections import Counter
        top_word, top_count = Counter(words).most_common(1)[0]
        if top_count / len(words) > 0.4:
            return f"token_loop:{top_word!r}_repeated_{top_count}_of_{len(words)}"
    return None


# RESULT CACHE — Nemotron killer feature 2026-05-23.
# Memoize (kind, description, max_cost_tier, sorted-required-capabilities) → result.
# When identical job submitted, return cached result instantly. Cuts LLM spend
# 30-70% on repetitive work + avoids NGC 429 storms from duplicate dispatches.
import hashlib as _hashlib
CACHE_TTL_SECONDS = 24 * 3600  # 24h default; idempotent work like compiles/lints often valid much longer


def cache_key_for(kind: str, description: Optional[str], max_cost_tier: str,
                  required_capabilities: Optional[list[str]]) -> str:
    norm_desc = (description or "").strip()
    norm_caps = ",".join(sorted(required_capabilities or []))
    payload = f"{kind}\n{norm_desc}\n{max_cost_tier}\n{norm_caps}"
    return _hashlib.sha256(payload.encode()).hexdigest()


async def cache_lookup(db, cache_key: str) -> Optional[dict]:
    cutoff = time.time() - CACHE_TTL_SECONDS
    async with db.execute(
        "SELECT result_json, source_job_id, result_mnemos_id, hit_count, cost_saved_usd, model, provider, cached_at "
        "FROM hive_cache WHERE cache_key=? AND cached_at >= ?",
        (cache_key, cutoff),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return {
        "result": json.loads(r[0]) if r[0] else None,
        "source_job_id": r[1], "result_mnemos_id": r[2],
        "hit_count": r[3], "cost_saved_usd": r[4],
        "model": r[5], "provider": r[6], "cached_at": r[7],
    }


async def cache_store(db, cache_key: str, source_job_id: str, result: dict,
                      result_mnemos_id: Optional[str], model: str, provider: str,
                      cost_for_save: float):
    now = time.time()
    await db.execute(
        "INSERT INTO hive_cache (cache_key, result_json, source_job_id, result_mnemos_id, "
        "hit_count, cost_saved_usd, model, provider, cached_at, last_hit_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, NULL) "
        "ON CONFLICT(cache_key) DO UPDATE SET "
        "result_json=excluded.result_json, source_job_id=excluded.source_job_id, "
        "result_mnemos_id=excluded.result_mnemos_id, cached_at=excluded.cached_at, "
        "model=excluded.model, provider=excluded.provider",
        (cache_key, json.dumps(result, default=str)[:32000], source_job_id,
         result_mnemos_id, model, provider, now),
    )


async def cache_record_hit(db, cache_key: str, cost_saved: float):
    await db.execute(
        "UPDATE hive_cache SET hit_count = hit_count + 1, "
        "cost_saved_usd = COALESCE(cost_saved_usd,0) + ?, last_hit_at = ? "
        "WHERE cache_key=?",
        (cost_saved, time.time(), cache_key),
    )


class AgentRegister(BaseModel):
    # OPEN REGISTRATION + RUNTIME→KIND ENFORCEMENT (per Kimi advisory 2026-05-23).
    # Any agent registers; identity is recorded for transparency. Kind must align
    # with runtime (prevents opencode-misregistering-as-claude). Soft fields
    # below default to "unknown" if not declared.
    #
    #   runtime          = TOOL/ORCHESTRATOR (claude-code, opencode, goose, codex, hermes, zeroclaw, openclaw, ic-engine, mnemos, human)
    #   model            = LLM (claude-opus-4-7, grok-4.3, kimi-k2.6, deepseek-v4-pro, gpt-5.5, gemini-3.1-pro, ...)
    #   provider         = INFERENCE HOST (anthropic, xai, ngc, openai, deepseek, together, groq, gemini, local-llamacpp, ...)
    #   kind             = URN routing segment — MUST be in RUNTIME_KIND_MAP[runtime] if runtime known
    #   autonomy_level   = autonomous / confirm-risky / interactive / unknown
    runtime: str = Field("unknown", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    model: str = "unknown"
    provider: str = "unknown"
    host: str
    kind: Optional[str] = Field(None, pattern=r"^[a-z][a-z0-9-]{0,31}$",
                                description="URN routing segment — defaults to runtime")
    autonomy_level: str = Field("unknown",
                                description="autonomous / confirm-risky / interactive / unknown")
    auth_method: str = Field("unknown",
                             description="subscription (Max plan), api (pay-per-token), free, unknown")
    plan_cap_usd: Optional[float] = None  # monthly cap; defaults from DEFAULT_PLAN_CAPS by auth_method
    pid: Optional[int] = None
    capabilities: Optional[list[str]] = None
    version: Optional[str] = None
    metadata: Optional[dict] = None


class AgentHeartbeat(BaseModel):
    urn: str
    status: str = "online"


class JobCreate(BaseModel):
    submitter_urn: str                                # who is asking (user OR delegating agent)
    parent_job_id: Optional[str] = None
    kind: str                                          # work type (code-edit/research/review/build/etc)
    description: Optional[str] = None
    priority: int = 0                                  # higher = more urgent (default 0)
    deadline: Optional[float] = None                   # unix ts, optional SLA
    required_capabilities: Optional[list[str]] = None  # worker must have ALL of these
    eligible_kinds: Optional[list[str]] = None         # restrict to agent kinds; null = any
    # #10 FIX (review 2026-05-23): project tag — separate from worker capabilities.
    # 'riskyeats'/'investorclaw' are PROJECTS, not capabilities. Workers don't gain/lose
    # the ability to do work because of a project label; the label is for filter+routing.
    project: Optional[str] = None
    max_retries: int = 2                               # auto-resubmit after worker reports failed (up to N times)
    # COST DISCIPLINE (per CLAUDE.md llm-usage-policy):
    max_cost_tier: str = "A"                           # cap which tier may execute: A=free, B=cheap, C=reserve. Default A=free first.
    preferred_providers: Optional[list[str]] = None    # ranked preference (first match wins among tier-eligible)
    preferred_models: Optional[list[str]] = None       # ranked model preference
    # MNEMOS provenance:
    mnemos_refs: Optional[list[str]] = None            # mem_XXX ids — context/handoffs/related work the worker should consult
    # DAG support:
    depends_on: Optional[list[str]] = None             # job ids that must be status='done' before this job is dequeueable


class JobUpdate(BaseModel):
    status: str
    result: Optional[dict] = None
    claimed_by: Optional[str] = None
    tokens_in: Optional[int] = None    # workers SHOULD report token usage on done/failed for cost audit
    tokens_out: Optional[int] = None
    result_mnemos_id: Optional[str] = None   # mem_XXX id where worker stored the outcome — closes provenance loop


class ScheduleCreate(BaseModel):
    name: str
    interval_seconds: int = Field(..., ge=60, le=86400 * 30,
                                  description="60s minimum (avoid hot-loop); 30d maximum")
    job_template: dict   # full JobCreate body that will be submitted each tick
    enabled: bool = True


class MessagePublish(BaseModel):
    from_urn: str
    to_urn: Optional[str] = None
    in_reply_to: Optional[str] = None
    topic: str
    payload: dict


# ---------- lifecycle ----------

CLAIM_STALE_AFTER = 1800.0   # claim → still 'claimed'/'running' without update >30min ⇒ orphan
async def reaper_task(app: FastAPI):
    while True:
        await asyncio.sleep(HEARTBEAT_REAP_INTERVAL)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # 1. Heartbeat reaper — mark dead agents offline
                cutoff = time.time() - HEARTBEAT_DEAD_AFTER
                async with db.execute(
                    "SELECT urn FROM agents WHERE status != 'offline' AND last_heartbeat < ?",
                    (cutoff,),
                ) as cur:
                    dead = [row[0] async for row in cur]
                if dead:
                    await db.executemany(
                        "UPDATE agents SET status='offline' WHERE urn=?",
                        [(u,) for u in dead],
                    )
                    await db.commit()
                    for urn in dead:
                        await emit_event(db, "agent.offline", {"urn": urn, "reason": "heartbeat_timeout"})
                # 2. ORPHAN CLAIM RECOVERY — jobs claimed by dead/stale workers go back to queue
                stale_cutoff = time.time() - CLAIM_STALE_AFTER
                async with db.execute(
                    "SELECT j.id, j.claimed_by FROM jobs j "
                    "LEFT JOIN agents a ON a.urn = j.claimed_by "
                    "WHERE j.status IN ('claimed','running') "
                    "AND ( a.status = 'offline' OR a.urn IS NULL OR j.claimed_at < ? )",
                    (stale_cutoff,)
                ) as cur:
                    orphans = [(r[0], r[1]) async for r in cur]
                if orphans:
                    for job_id, claimer in orphans:
                        await db.execute(
                            "UPDATE jobs SET status='queued', claimed_by=NULL, claimed_at=NULL, "
                            "claimed_runtime=NULL, claimed_model=NULL, claimed_provider=NULL, "
                            "claimed_cost_tier=NULL WHERE id=? AND status IN ('claimed','running')",
                            (job_id,))
                        await emit_event(db, "job.unclaimed", {
                            "id": job_id, "prior_claimer": claimer,
                            "reason": "worker_offline_or_stale_claim",
                        })
                    await db.commit()
                # 3. Scheduler — fire due interval-based jobs
                now_ts = time.time()
                async with db.execute(
                    "SELECT id, name, created_by_urn, interval_seconds, job_template, fire_count "
                    "FROM scheduled_jobs WHERE enabled=1 AND next_fire_at <= ?",
                    (now_ts,)
                ) as cur:
                    due = [tuple(r) async for r in cur]
                for sched_id, sname, sub_urn, interval, tpl_json, fcount in due:
                    try:
                        tpl = json.loads(tpl_json)
                        tpl["submitter_urn"] = sub_urn
                        # synth a JobCreate + go through the cache+role machinery
                        await create_job(JobCreate(**tpl))  # type: ignore
                        await db.execute(
                            "UPDATE scheduled_jobs SET last_fired_at=?, next_fire_at=?, "
                            "fire_count=fire_count+1 WHERE id=?",
                            (now_ts, now_ts + interval, sched_id),
                        )
                        await emit_event(db, "schedule.fired", {
                            "schedule_id": sched_id, "name": sname,
                            "fire_count": fcount + 1, "next_at": now_ts + interval,
                        })
                    except Exception as se:
                        print(f"scheduler error {sched_id}: {se}", flush=True)
                if due:
                    await db.commit()
                # 4. Auto-cancel stale jobs (queued > 7 days untouched)
                stale_job_cutoff = time.time() - 7 * 24 * 3600
                async with db.execute(
                    "SELECT id FROM jobs WHERE status='queued' AND started_at < ?",
                    (stale_job_cutoff,)
                ) as cur:
                    stale_ids = [r[0] async for r in cur]
                if stale_ids:
                    await db.executemany(
                        "UPDATE jobs SET status='cancelled', ended_at=? WHERE id=?",
                        [(time.time(), j) for j in stale_ids],
                    )
                    for j in stale_ids:
                        await emit_event(db, "job.cancelled", {"id": j, "reason": "stale_>7d"})
                    await db.commit()
                # 5. Purge old events
                retain_cutoff = time.time() - EVENTS_RETAIN_HOURS * 3600
                await db.execute("DELETE FROM events WHERE ts < ?", (retain_cutoff,))
                await db.commit()
        except Exception as e:
            print(f"reaper error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        await db.executescript(SCHEMA)
        # #10 FIX (review 2026-05-23): additive migrations for live DBs (jobs.project).
        # Add-only via ALTER TABLE; ignore "duplicate column" errors so reruns are no-ops.
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN project TEXT",
        ):
            try:
                await db.execute(stmt)
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"migration warn ({stmt!r}): {e}", flush=True)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project)")
        await db.commit()
    task = asyncio.create_task(reaper_task(app))
    yield
    task.cancel()


app = FastAPI(title="GRAEAE Hive Mind", version="0.1.0", lifespan=lifespan)


# ---------- endpoints ----------

@app.get("/health")
async def health():
    return {"status": "healthy", "ts": time.time(), "service": "graeae-hive-mind", "version": "0.1.0"}


@app.get("/")
async def dashboard():
    """Minimal HTML+JS dashboard. Auto-refreshes /v1/agents + /v1/jobs + /v1/stats/* + SSE."""
    from fastapi.responses import HTMLResponse
    p = "/srv/agent-bus/dashboard.html"
    try:
        with open(p) as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse(f"<h1>dashboard.html missing</h1><p>Expected at {p}</p>", status_code=404)


@app.post("/v1/agents/register")
async def register(req: AgentRegister):
    # #5 FIX (review 2026-05-23): reject minimally-incomplete registrations with 422 so
    # callers see the error instead of getting a null URN that bricks their session.
    if not req.host or not req.host.strip():
        raise HTTPException(422, "host is required (e.g., 'studio' or hostname -s)")
    runtime = (req.runtime or "unknown").lower()
    kind = (req.kind or runtime).lower()
    if runtime == "unknown" and not req.kind:
        raise HTTPException(
            422, "must provide runtime (claude-code/opencode/goose/codex/...) OR explicit kind. "
                 "Defaulting to 'unknown' was masking session-bricking misregistrations."
        )
    allowed = RUNTIME_KIND_MAP.get(runtime, {runtime, "unknown"})
    if runtime != "unknown" and kind not in allowed and kind != runtime:
        raise HTTPException(
            status_code=422,
            detail=(
                f"identity-mismatch: runtime={runtime!r} cannot register as kind={kind!r}. "
                f"Allowed kinds for this runtime: {sorted(allowed)}. "
                f"Set kind to one of those (or omit it) — fixes opencode-misregistering-as-claude per advisory."
            ),
        )
    autonomy = (req.autonomy_level or "unknown").lower()
    if autonomy not in AUTONOMY_LEVELS:
        raise HTTPException(422, f"autonomy_level must be one of {sorted(AUTONOMY_LEVELS)}, got {autonomy!r}")
    auth_method = (req.auth_method or "unknown").lower()
    if auth_method not in AUTH_METHODS:
        raise HTTPException(422, f"auth_method must be one of {sorted(AUTH_METHODS)}, got {auth_method!r}")
    plan_cap_usd = req.plan_cap_usd if req.plan_cap_usd is not None else DEFAULT_PLAN_CAPS.get(auth_method, 50.0)
    provider = (req.provider or "unknown").lower()
    model = (req.model or "unknown").lower()
    tier = cost_tier_for(provider)
    session_id = str(uuid.uuid4())
    urn = make_urn(kind, req.host, session_id)
    now = time.time()
    # Phase 2 cut 1: INSERT delegated to SqliteHiveMindRepository.insert_agent.
    # emit_event stays inline (separate transaction is acceptable — event log
    # is observability, not the agent row's source of truth).
    await _REPO.insert_agent(
        urn=urn, kind=kind, runtime=runtime, model=model, provider=provider,
        cost_tier=tier, autonomy_level=autonomy, auth_method=auth_method,
        plan_cap_usd=plan_cap_usd, host=req.host, session_id=session_id,
        pid=req.pid, capabilities=req.capabilities, version=req.version,
        started_at=now, last_heartbeat=now, metadata=req.metadata,
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await emit_event(db, "agent.online", {
            "urn": urn, "kind": kind, "runtime": runtime,
            "model": model, "provider": provider, "cost_tier": tier,
            "host": req.host, "autonomy_level": autonomy,
        })
        await db.commit()
    return {
        "urn": urn, "session_id": session_id, "registered_at": now,
        "kind": kind, "runtime": runtime, "model": model,
        "provider": provider, "cost_tier": tier, "autonomy_level": autonomy,
        "auth_method": auth_method, "plan_cap_usd": plan_cap_usd,
    }


@app.post("/v1/agents/heartbeat")
async def heartbeat(req: AgentHeartbeat):
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE agents SET last_heartbeat=?, status=? WHERE urn=?",
            (now, req.status, req.urn),
        )
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"agent not found: {req.urn}")
    return {"ack": True, "ts": now}


@app.get("/v1/agents")
async def list_agents(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    host: Optional[str] = None,
    runtime: Optional[str] = None,
    pid: Optional[int] = None,
    cost_tier: Optional[str] = None,
):
    sql = ("SELECT urn, kind, host, status, last_heartbeat, capabilities, version, metadata, "
           "pid, runtime, model, provider, cost_tier, autonomy_level "
           "FROM agents WHERE 1=1")
    args: list = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if host:
        sql += " AND host=?"
        args.append(host)
    if runtime:
        sql += " AND runtime=?"
        args.append(runtime)
    if pid is not None:
        sql += " AND pid=?"
        args.append(pid)
    if cost_tier:
        sql += " AND cost_tier=?"
        args.append(cost_tier)
    sql += " ORDER BY last_heartbeat DESC"
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, args) as cur:
            async for r in cur:
                meta = json.loads(r[7]) if r[7] else {}
                # build display name: kind@host[pid] cwd:cwd_basename — distinguishes multiple sessions on same host
                cwd = (meta or {}).get("cwd") if isinstance(meta, dict) else None
                cwd_short = cwd.split("/")[-1] if cwd else None
                display = f"{r[1]}@{r[2]}"
                if r[8] is not None:
                    display += f"[pid={r[8]}]"
                if cwd_short:
                    display += f" cwd={cwd_short}"
                rows.append({
                    "urn": r[0], "kind": r[1], "host": r[2], "status": r[3],
                    "last_heartbeat": r[4],
                    "capabilities": json.loads(r[5]) if r[5] else None,
                    "version": r[6],
                    "metadata": meta,
                    "pid": r[8],
                    "runtime": r[9],
                    "model": r[10],
                    "provider": r[11],
                    "cost_tier": r[12],
                    "autonomy_level": r[13],
                    "display": display,
                })
    return {"count": len(rows), "agents": rows}


@app.get("/v1/agents/{urn_path:path}/throttle")
async def agent_throttle(urn_path: str):
    """Inspect an agent's plan-cap throttle state."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT urn, kind, runtime, auth_method, plan_cap_usd, plan_period_used_usd "
            "FROM agents WHERE urn=?", (urn_path,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"agent not found: {urn_path}")
    used = row[5] or 0
    cap = row[4] or 0
    pct = (100 * used / cap) if cap else 0
    return {
        "urn": row[0], "kind": row[1], "runtime": row[2],
        "auth_method": row[3], "plan_cap_usd": cap,
        "plan_period_used_usd": round(used, 4),
        "plan_period_pct": round(pct, 1),
        "throttled": row[3] == "subscription" and pct >= 85.0,
        "headroom_pct": THROTTLE_HEADROOM * 100,
    }


@app.post("/v1/agents/{urn_path:path}/plan-reset")
async def reset_plan_period(urn_path: str):
    """Operator zeros the MTD usage — call monthly on billing rollover."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE agents SET plan_period_used_usd = 0 WHERE urn=?", (urn_path,))
        await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, f"agent not found: {urn_path}")
    return {"ok": True, "urn": urn_path, "reset_at": time.time()}


@app.get("/v1/agents/whoami")
async def whoami(host: str, pid: int):
    """Help a session find ITS OWN urn by (host, pid) — most recent online registration wins.
    Useful when a session forgets its urn after restart and needs to re-discover its identity."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT urn, kind, runtime, model, provider, cost_tier, autonomy_level, started_at "
            "FROM agents WHERE host=? AND pid=? AND status='online' "
            "ORDER BY started_at DESC LIMIT 1",
            (host, pid),
        ) as cur:
            r = await cur.fetchone()
    if not r:
        raise HTTPException(404, f"no online agent matches host={host} pid={pid}")
    return {
        "urn": r[0], "kind": r[1], "runtime": r[2], "model": r[3],
        "provider": r[4], "cost_tier": r[5], "autonomy_level": r[6],
        "started_at": r[7],
    }


@app.post("/v1/jobs")
async def create_job(req: JobCreate):
    """Submit work to the triage queue. No agent assignment — workers self-claim via /v1/jobs/next.

    ROLE ENFORCEMENT (user directive 2026-05-23):
    Worker runtimes (opencode/goose/codex/hermes/claw-family/ic-engine) are CLAIMERS, not submitters.
    Posting jobs requires submitter to be registered with an orchestrator runtime (claude-code/human/mnemos).
    Workers attempting to POST jobs get 403 — they should call /v1/jobs/next instead.
    """
    job_id = uuidv7()
    now = time.time()
    if req.mnemos_refs:
        bad = [r for r in req.mnemos_refs if not (isinstance(r, str) and r.startswith("mem_"))]
        if bad:
            raise HTTPException(422, f"mnemos_refs must be mem_XXX ids — bad entries: {bad}")
    # DAG: validate depends_on targets exist + no self-cycle
    if req.depends_on:
        if job_id in req.depends_on:
            raise HTTPException(422, "depends_on cannot include self")
        async with aiosqlite.connect(DB_PATH) as _vd:
            placeholders = ",".join("?" * len(req.depends_on))
            async with _vd.execute(
                f"SELECT id FROM jobs WHERE id IN ({placeholders})",
                tuple(req.depends_on),
            ) as cur:
                found = {row[0] async for row in cur}
            missing = [d for d in req.depends_on if d not in found]
            if missing:
                raise HTTPException(422, f"depends_on references unknown job ids: {missing}")
    # RESULT-CACHE CHECK: identical (kind, description, max_cost_tier, required_caps) within TTL → return cached result, mark new job done immediately
    ck = cache_key_for(req.kind, req.description, (req.max_cost_tier or "A").upper(), req.required_capabilities)
    async with aiosqlite.connect(DB_PATH) as db:
        cached = await cache_lookup(db, ck)
        if cached:
            # Phase 2 cut 3: cache-hit insert delegated to repo. Cache-hit
            # accounting (cache_record_hit) stays inline because it needs
            # the existing db handle for emit_event.
            cached_result = {**(cached["result"] or {}), "cache_hit": True,
                             "source_job_id": cached["source_job_id"]}
            await _REPO.insert_job_cache_hit(
                job_id=job_id, submitter_urn=req.submitter_urn,
                parent_job_id=req.parent_job_id, kind=req.kind,
                description=req.description, priority=req.priority,
                deadline=req.deadline,
                required_capabilities=req.required_capabilities,
                eligible_kinds=req.eligible_kinds, project=req.project,
                max_cost_tier=(req.max_cost_tier or "A").upper(),
                preferred_providers=req.preferred_providers,
                preferred_models=req.preferred_models,
                mnemos_refs=req.mnemos_refs,
                started_at=now, ended_at=now, result=cached_result,
                provider=cached["provider"], model=cached["model"],
                result_mnemos_id=cached.get("result_mnemos_id"),
            )
            # record cache hit with estimated cost-saving (use prior job's cost or fallback)
            saved = 0.01  # conservative fallback if no token data
            await cache_record_hit(db, ck, saved)
            await db.commit()
            await emit_event(db, "job.cached", {
                "id": job_id, "source_job_id": cached["source_job_id"],
                "kind": req.kind, "cost_saved_usd": saved,
            })
            return {
                "id": job_id, "created_at": now,
                "status": "done", "cache_hit": True,
                "source_job_id": cached["source_job_id"],
                "result": cached["result"],
                "result_mnemos_id": cached.get("result_mnemos_id"),
            }
        # role check: submitter must be a registered orchestrator
        async with db.execute(
            "SELECT runtime FROM agents WHERE urn=?", (req.submitter_urn,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            submitter_runtime = (row[0] or "unknown").lower()
            if submitter_runtime in WORKER_ONLY_RUNTIMES:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"role-violation: runtime={submitter_runtime!r} is a WORKER, not an orchestrator. "
                        f"Workers CLAIM jobs via POST /v1/jobs/next, they don't SUBMIT them. "
                        f"If you need to delegate, message a human or claude-code orchestrator instead. "
                        f"Orchestrators: {sorted(ORCHESTRATOR_RUNTIMES)}."
                    ),
                )
        # else: submitter not registered → allowed (human/external curl)
        # Phase 2 cut 2: INSERT delegated to repo.insert_job_queued.
        # The cache-hit short-circuit path above still runs inline (different
        # row shape — status='done' + claimed_provider — cut 3 target).
        await _REPO.insert_job_queued(
            job_id=job_id, submitter_urn=req.submitter_urn,
            parent_job_id=req.parent_job_id, kind=req.kind,
            description=req.description, priority=req.priority,
            deadline=req.deadline,
            required_capabilities=req.required_capabilities,
            eligible_kinds=req.eligible_kinds, project=req.project,
            max_cost_tier=(req.max_cost_tier or "A").upper(),
            preferred_providers=req.preferred_providers,
            preferred_models=req.preferred_models,
            mnemos_refs=req.mnemos_refs, depends_on=req.depends_on,
            max_retries=req.max_retries, started_at=now,
        )
        await emit_event(db, "job.queued", {
            "id": job_id, "submitter": req.submitter_urn, "kind": req.kind,
            "description": req.description, "priority": req.priority,
            "required_capabilities": req.required_capabilities,
            "eligible_kinds": req.eligible_kinds,
            "max_cost_tier": (req.max_cost_tier or "A").upper(),
            "mnemos_refs": req.mnemos_refs,
        })
    return {"id": job_id, "created_at": now, "mnemos_refs": req.mnemos_refs}


@app.post("/v1/jobs/next")
async def dequeue_next_job(agent_urn: str):
    """Atomic dequeue: highest-priority queued job that this agent is eligible for.

    Self-assignment for swarm: agent calls this in its main loop. Server:
      1. Looks up agent's kind + capabilities.
      2. Finds top-priority queued job where (eligible_kinds covers agent kind) AND (required_capabilities subset of agent capabilities).
      3. Atomically claims it (status='claimed', claimed_by=agent_urn, claimed_at=now).
      4. Returns job to caller; or 204 No Content if nothing matches.

    Race-safe via SQLite immediate-mode UPDATE...WHERE rowid=(SELECT...LIMIT 1) under a transaction.
    """
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT kind, capabilities, runtime, model, provider, cost_tier, "
            "auth_method, plan_cap_usd, plan_period_used_usd "
            "FROM agents WHERE urn=? AND status IN ('online','idle')",
            (agent_urn,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"agent not registered or offline: {agent_urn}")
        agent_kind, caps_json, a_runtime, a_model, a_provider, a_tier, a_auth, a_cap, a_used = row
        agent_caps = set(json.loads(caps_json)) if caps_json else set()
        a_tier = a_tier or "C"
        a_auth = (a_auth or "unknown").lower()
        # Throttle: subscription agents over 85% MTD usage get refused tier-B/C jobs;
        # they can still claim tier-A (free) work. Forces API/free fallback as plan cap nears.
        sub_throttled = (a_auth == "subscription" and a_cap and a_used and a_used >= THROTTLE_HEADROOM * a_cap)

    # Phase 2 cut 4: atomic claim delegated to repo (transaction +
    # filter chain are storage semantics; only event emission stays here).
    claimed = await _REPO.find_and_claim_job(
        agent_urn=agent_urn, agent_kind=agent_kind, agent_caps=agent_caps,
        agent_runtime=a_runtime or "unknown", agent_model=a_model or "unknown",
        agent_provider=a_provider or "unknown", agent_tier=a_tier,
        cost_tier_order=list(COST_TIERS),
        sub_throttled=bool(sub_throttled), now=now,
    )
    if claimed:
        async with aiosqlite.connect(DB_PATH) as db2:
            await emit_event(db2, "job.claimed", {
                "id": claimed["id"], "claimed_by": agent_urn,
                "kind": claimed["kind"],
                "runtime": a_runtime, "model": a_model,
                "provider": a_provider, "cost_tier": a_tier,
            })
        return claimed
    return JSONResponse(status_code=204, content=None)


@app.patch("/v1/jobs/{job_id}")
async def update_job(job_id: str, req: JobUpdate):
    now = time.time()
    # #3 FIX: Hallucination guard now surfaces failure_reason + exit_code=-2 (per review 2026-05-23)
    halluc_reason = None
    if req.status == "done" and req.result:
        halluc_reason = hallucination_check(req.result)
        if halluc_reason:
            # patch result: exit_code=-2 + top-level failure_reason for easy filtering
            patched = dict(req.result or {})
            patched["exit_code"] = -2
            patched["failure_reason"] = f"hallucination_guard:{halluc_reason}"
            patched["_hallucination_guard"] = halluc_reason
            req = JobUpdate(
                status="failed",
                result=patched,
                claimed_by=req.claimed_by, tokens_in=req.tokens_in,
                tokens_out=req.tokens_out, result_mnemos_id=req.result_mnemos_id,
            )
    fields = ["status=?"]
    args: list = [req.status]
    if req.status in ("done", "failed", "cancelled"):
        fields.append("ended_at=?")
        args.append(now)
    if req.result is not None:
        fields.append("result=?")
        args.append(json.dumps(req.result))
    if req.claimed_by:
        fields.extend(["claimed_by=?", "claimed_at=?"])
        args.extend([req.claimed_by, now])
    # token usage + cost estimation
    cost_estimate = None
    claimed_by_urn = None
    if req.tokens_in is not None or req.tokens_out is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT claimed_provider, claimed_model, claimed_by FROM jobs WHERE id=?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
        if row:
            prov, mod, claimed_by_urn = row
            t_in = int(req.tokens_in or 0)
            t_out = int(req.tokens_out or 0)
            cost_estimate = estimate_cost(prov or "unknown", mod or "unknown", t_in, t_out)
            fields.extend(["tokens_in=?", "tokens_out=?", "estimated_cost_usd=?"])
            args.extend([t_in, t_out, cost_estimate])
    args.append(job_id)
    sql = f"UPDATE jobs SET {', '.join(fields)} WHERE id=?"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        # Roll MTD spend onto claimer (subscription throttle requires this)
        if cost_estimate and cost_estimate > 0 and claimed_by_urn:
            await db.execute(
                "UPDATE agents SET plan_period_used_usd = COALESCE(plan_period_used_usd,0) + ? WHERE urn=?",
                (cost_estimate, claimed_by_urn),
            )
        # On failed: auto-retry if under max_retries. Exponential backoff: 30s × 2^retry_count
        if req.status == "failed":
            async with db.execute(
                "SELECT retry_count, max_retries FROM jobs WHERE id=?", (job_id,)
            ) as rcur:
                rrow = await rcur.fetchone()
            if rrow and rrow[0] < rrow[1]:
                backoff = 30.0 * (2 ** rrow[0])
                next_at = time.time() + backoff
                # #4 FIX: clear ended_at + result + tokens + cost when re-queueing — prevents
                # impossible state (status=queued AND ended_at IS NOT NULL) per review 2026-05-23.
                await db.execute(
                    "UPDATE jobs SET status='queued', retry_count=retry_count+1, "
                    "retry_backoff_until=?, claimed_by=NULL, claimed_at=NULL, "
                    "claimed_runtime=NULL, claimed_model=NULL, claimed_provider=NULL, claimed_cost_tier=NULL, "
                    "ended_at=NULL, result=NULL, tokens_in=NULL, tokens_out=NULL, estimated_cost_usd=NULL "
                    "WHERE id=?",
                    (next_at, job_id),
                )
                await emit_event(db, "job.retry", {
                    "id": job_id, "retry_count": rrow[0] + 1,
                    "max_retries": rrow[1], "backoff_sec": backoff,
                })
                # fall through to stats roll-up below (count as fail for this attempt)
        # On done/failed/cancelled: roll per-worker per-kind stats (capability scoring)
        if req.status in ("done", "failed", "cancelled"):
            async with db.execute(
                "SELECT kind, description, max_cost_tier, required_capabilities, "
                "claimed_model, claimed_provider, claimed_by, result, started_at "
                "FROM jobs WHERE id=?", (job_id,)
            ) as cur2:
                jrow = await cur2.fetchone()
            if jrow:
                kind_j, desc_j, mtier, reqcaps_json, mdl_j, prov_j, claimed_by_j, result_j, started_j = jrow
                # capability scoring stats
                if claimed_by_j and kind_j:
                    duration = (time.time() - (started_j or time.time())) if started_j else 0
                    col = {"done": "success_count", "failed": "fail_count", "cancelled": "cancelled_count"}[req.status]
                    await db.execute(
                        f"INSERT INTO worker_kind_stats (urn, kind, {col}, total_tokens_in, total_tokens_out, "
                        f"total_cost_usd, total_duration_sec, last_run) "
                        f"VALUES (?, ?, 1, ?, ?, ?, ?, ?) "
                        f"ON CONFLICT(urn, kind) DO UPDATE SET "
                        f"{col} = {col} + 1, "
                        f"total_tokens_in = total_tokens_in + ?, "
                        f"total_tokens_out = total_tokens_out + ?, "
                        f"total_cost_usd = total_cost_usd + ?, "
                        f"total_duration_sec = total_duration_sec + ?, "
                        f"last_run = ?",
                        (
                            claimed_by_j, kind_j,
                            int(req.tokens_in or 0), int(req.tokens_out or 0),
                            cost_estimate or 0, duration, time.time(),
                            int(req.tokens_in or 0), int(req.tokens_out or 0),
                            cost_estimate or 0, duration, time.time(),
                        ),
                    )
                # cache only successful results
                if req.status == "done":
                    ck = cache_key_for(kind_j, desc_j, (mtier or "A").upper(),
                                       json.loads(reqcaps_json) if reqcaps_json else None)
                    rdict = json.loads(result_j) if result_j else (req.result or {})
                    ec = (rdict or {}).get("exit_code")
                    if ec == 0 or ec is None:
                        await cache_store(db, ck, job_id, rdict, req.result_mnemos_id,
                                          mdl_j or "unknown", prov_j or "unknown", cost_estimate or 0)
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"job not found: {job_id}")
        await emit_event(db, f"job.{req.status}", {
            "id": job_id, "claimed_by": req.claimed_by,
            "tokens_in": req.tokens_in, "tokens_out": req.tokens_out,
            "estimated_cost_usd": cost_estimate,
        })
    return {"ok": True, "ts": now, "estimated_cost_usd": cost_estimate}


@app.post("/v1/schedules")
async def create_schedule(req: ScheduleCreate):
    """Create a recurring scheduled job. Re-fires every `interval_seconds`.
    job_template is the JobCreate body that will be submitted each tick.
    Submitter_urn auto-injected from caller — they take responsibility for the cron loop.
    """
    sid = uuidv7()
    now = time.time()
    # Validate the template parses as a JobCreate
    try:
        tpl_copy = dict(req.job_template)
        tpl_copy.setdefault("submitter_urn", "urn:agent:human:scheduler-placeholder")
        JobCreate(**tpl_copy)
    except Exception as e:
        raise HTTPException(422, f"job_template invalid: {e}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO scheduled_jobs (id, name, created_by_urn, interval_seconds, "
            "job_template, enabled, last_fired_at, next_fire_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (sid, req.name, tpl_copy.get("submitter_urn"), int(req.interval_seconds),
             json.dumps(req.job_template), 1 if req.enabled else 0,
             now + req.interval_seconds, now),
        )
        await db.commit()
    return {"id": sid, "name": req.name, "next_fire_at": now + req.interval_seconds}


@app.get("/v1/schedules")
async def list_schedules():
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, created_by_urn, interval_seconds, enabled, "
            "last_fired_at, next_fire_at, fire_count, created_at FROM scheduled_jobs"
        ) as cur:
            async for r in cur:
                rows.append({
                    "id": r[0], "name": r[1], "created_by": r[2],
                    "interval_seconds": r[3], "enabled": bool(r[4]),
                    "last_fired_at": r[5], "next_fire_at": r[6],
                    "fire_count": r[7], "created_at": r[8],
                })
    return {"count": len(rows), "schedules": rows}


@app.patch("/v1/schedules/{sid}")
async def patch_schedule(sid: str, enabled: Optional[bool] = None,
                         interval_seconds: Optional[int] = None):
    sets, args = [], []
    if enabled is not None:
        sets.append("enabled=?")
        args.append(1 if enabled else 0)
    if interval_seconds is not None:
        if interval_seconds < 60 or interval_seconds > 86400 * 30:
            raise HTTPException(422, "interval_seconds must be 60..2592000")
        sets.append("interval_seconds=?")
        args.append(int(interval_seconds))
        sets.append("next_fire_at=?")
        args.append(time.time() + int(interval_seconds))
    if not sets:
        raise HTTPException(422, "no fields to update")
    args.append(sid)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE scheduled_jobs SET {', '.join(sets)} WHERE id=?", args)
        await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "schedule not found")
    return {"ok": True}


@app.delete("/v1/schedules/{sid}")
async def delete_schedule(sid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM scheduled_jobs WHERE id=?", (sid,))
        await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "schedule not found")
    return {"ok": True}


@app.get("/v1/stats/workers")
async def worker_stats(kind: Optional[str] = None, top_n: int = 30,
                       include_system: bool = False):
    """Per-worker per-kind capability scores. Submitters use this to pick best worker for a kind.

    #8 FIX (review 2026-05-23): exclude `system` kind agents by default (they're host monitors,
    not compute workers — including them was misleading). Pass include_system=true to override.
    """
    sql = ("SELECT urn, kind, success_count, fail_count, cancelled_count, "
           "total_tokens_in, total_tokens_out, ROUND(total_cost_usd,4), "
           "ROUND(total_duration_sec/NULLIF(success_count+fail_count,0),1) AS avg_dur, "
           "datetime(last_run,'unixepoch') AS last_run, "
           "ROUND(100.0 * success_count / NULLIF(success_count+fail_count+cancelled_count,0), 1) AS success_pct "
           "FROM worker_kind_stats WHERE 1=1")
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if not include_system:
        sql += " AND urn NOT LIKE 'urn:agent:system:%'"
    sql += " ORDER BY success_count DESC, success_pct DESC LIMIT ?"
    args.append(int(top_n))
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, args) as cur:
            async for r in cur:
                rows.append({
                    "urn": r[0], "kind": r[1],
                    "success_count": r[2], "fail_count": r[3], "cancelled_count": r[4],
                    "total_tokens_in": r[5], "total_tokens_out": r[6],
                    "total_cost_usd": r[7], "avg_duration_sec": r[8],
                    "last_run": r[9], "success_pct": r[10],
                })
    return {"count": len(rows), "workers": rows}


@app.get("/v1/stats/cache")
async def cache_stats(top_n: int = 20):
    """Result-cache hit-rate + cost savings."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(hit_count), ROUND(SUM(cost_saved_usd),4) FROM hive_cache"
        ) as cur:
            r = await cur.fetchone()
        totals = {"cached_jobs": r[0] or 0, "total_hits": r[1] or 0,
                  "total_cost_saved_usd": r[2] or 0}
        # job-side stats
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE result LIKE '%\"cache_hit\": true%'"
        ) as cur:
            r2 = await cur.fetchone()
        totals["jobs_short_circuited"] = r2[0] or 0
        # top cached entries
        async with db.execute(
            "SELECT cache_key, hit_count, cost_saved_usd, model, provider, "
            "datetime(cached_at,'unixepoch') AS cached_at, source_job_id "
            "FROM hive_cache ORDER BY hit_count DESC, cost_saved_usd DESC LIMIT ?",
            (int(top_n),)
        ) as cur:
            entries = []
            async for row in cur:
                entries.append({
                    "cache_key": row[0][:16], "hit_count": row[1],
                    "cost_saved_usd": row[2], "model": row[3],
                    "provider": row[4], "cached_at": row[5],
                    "source_job_id": row[6],
                })
    return {"totals": totals, "top_entries": entries}


@app.get("/v1/stats/costs")
async def cost_stats(since_hours: int = 168, group_by: str = "provider"):
    """Aggregated cost stats. group_by: provider | model | runtime | cost_tier | claimed_by | day.
    since_hours: window in hours (default 168 = 7 days).
    """
    if group_by not in {"provider", "model", "runtime", "cost_tier", "claimed_by", "day", "kind"}:
        raise HTTPException(422, "group_by must be one of: provider, model, runtime, cost_tier, claimed_by, day, kind")
    cutoff = time.time() - since_hours * 3600
    if group_by == "day":
        sel = "DATE(ended_at, 'unixepoch')"
    elif group_by == "provider":
        sel = "COALESCE(claimed_provider,'unknown')"
    elif group_by == "model":
        sel = "COALESCE(claimed_model,'unknown')"
    elif group_by == "runtime":
        sel = "COALESCE(claimed_runtime,'unknown')"
    elif group_by == "cost_tier":
        sel = "COALESCE(claimed_cost_tier,'unknown')"
    elif group_by == "claimed_by":
        sel = "COALESCE(claimed_by,'unknown')"
    else:  # kind
        sel = "kind"
    sql = (
        f"SELECT {sel} AS bucket, "
        "COUNT(*) AS job_count, "
        "SUM(COALESCE(tokens_in,0)) AS tot_in, "
        "SUM(COALESCE(tokens_out,0)) AS tot_out, "
        "ROUND(SUM(COALESCE(estimated_cost_usd,0)),4) AS tot_cost_usd, "
        "ROUND(AVG(CASE WHEN ended_at IS NOT NULL THEN ended_at-started_at END),2) AS avg_dur_s "
        "FROM jobs WHERE ended_at IS NOT NULL AND ended_at >= ? "
        f"GROUP BY bucket ORDER BY tot_cost_usd DESC, job_count DESC"
    )
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, (cutoff,)) as cur:
            async for r in cur:
                rows.append({
                    "bucket": r[0], "job_count": r[1],
                    "tokens_in": r[2], "tokens_out": r[3],
                    "estimated_cost_usd": r[4], "avg_duration_sec": r[5],
                })
    # totals
    totals = {
        "job_count": sum(r["job_count"] for r in rows),
        "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
        "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
        "estimated_cost_usd": round(sum(r["estimated_cost_usd"] or 0 for r in rows), 4),
    }
    return {"since_hours": since_hours, "group_by": group_by, "totals": totals, "buckets": rows}


@app.post("/v1/jobs/{job_id}/claim")
async def claim_job(job_id: str, by: str):
    """First-write-wins claim: prevents duplicate execution by multiple agents."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE jobs SET status='claimed', claimed_by=?, claimed_at=? "
            "WHERE id=? AND status IN ('queued','offered') AND claimed_by IS NULL",
            (by, now, job_id),
        )
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "job already claimed or not available")
        await emit_event(db, "job.claimed", {"id": job_id, "claimed_by": by})
    return {"claimed": True, "ts": now}


@app.get("/v1/jobs")
async def list_jobs(
    status: Optional[str] = None,
    agent_urn: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 100,
):
    sql = "SELECT id, submitter_urn, parent_job_id, kind, description, priority, status, claimed_by, started_at, ended_at, result FROM jobs WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if agent_urn:
        sql += " AND (submitter_urn=? OR claimed_by=?)"
        args.extend([agent_urn, agent_urn])
    if since:
        sql += " AND started_at >= ?"
        args.append(since)
    sql += f" ORDER BY priority DESC, started_at DESC LIMIT {int(limit)}"
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, args) as cur:
            async for r in cur:
                rows.append({
                    "id": r[0], "submitter_urn": r[1], "parent_job_id": r[2],
                    "kind": r[3], "description": r[4], "priority": r[5],
                    "status": r[6], "claimed_by": r[7],
                    "started_at": r[8], "ended_at": r[9],
                    "result": json.loads(r[10]) if r[10] else None,
                })
    return {"count": len(rows), "jobs": rows}


@app.post("/v1/messages")
async def publish_message(req: MessagePublish):
    msg_id = uuidv7()
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (id, from_urn, to_urn, in_reply_to, topic, payload, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, req.from_urn, req.to_urn, req.in_reply_to, req.topic, json.dumps(req.payload), now),
        )
        await db.commit()
        await emit_event(db, "message", {
            "id": msg_id, "from": req.from_urn, "to": req.to_urn,
            "topic": req.topic, "payload": req.payload,
        })
    return {"id": msg_id, "ts": now}


@app.get("/v1/messages")
async def list_messages(
    to_urn: Optional[str] = None,
    topic: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 100,
):
    sql = "SELECT id, from_urn, to_urn, in_reply_to, topic, payload, ts FROM messages WHERE 1=1"
    args: list = []
    if to_urn:
        sql += " AND (to_urn=? OR to_urn IS NULL)"
        args.append(to_urn)
    if topic:
        sql += " AND topic LIKE ?"
        args.append(topic.replace("*", "%"))
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += f" ORDER BY ts DESC LIMIT {int(limit)}"
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, args) as cur:
            async for r in cur:
                rows.append({
                    "id": r[0], "from": r[1], "to": r[2], "in_reply_to": r[3],
                    "topic": r[4], "payload": json.loads(r[5]), "ts": r[6],
                })
    return {"count": len(rows), "messages": rows}


@app.get("/v1/events")
async def stream_events(request: Request, since_id: Optional[int] = None):
    """SSE stream of events. Optional since_id for catch-up."""
    sub_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    EVENT_QUEUE[sub_id] = queue

    async def gen():
        try:
            # catch-up phase
            if since_id is not None:
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute(
                        "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id ASC",
                        (since_id,),
                    ) as cur:
                        async for r in cur:
                            yield {
                                "id": str(r[0]),
                                "event": r[2],
                                "data": json.dumps({"ts": r[1], "kind": r[2], "payload": json.loads(r[3])}),
                            }
            # live stream
            last_ping = time.time()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=SSE_PING_INTERVAL)
                    yield {
                        "event": evt["kind"],
                        "data": json.dumps(evt),
                    }
                except asyncio.TimeoutError:
                    if time.time() - last_ping >= SSE_PING_INTERVAL:
                        yield {"event": "ping", "data": str(int(time.time()))}
                        last_ping = time.time()
        finally:
            EVENT_QUEUE.pop(sub_id, None)

    return EventSourceResponse(gen())


@app.get("/v1/events/log")
async def events_log(since_id: Optional[int] = None, limit: int = 100):
    """JSON polling alternative to SSE."""
    sql = "SELECT id, ts, kind, payload, agent_urn FROM events"
    args: list = []
    if since_id is not None:
        sql += " WHERE id > ?"
        args.append(since_id)
    sql += f" ORDER BY id ASC LIMIT {int(limit)}"
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, args) as cur:
            async for r in cur:
                rows.append({
                    "id": r[0], "ts": r[1], "kind": r[2],
                    "payload": json.loads(r[3]), "agent_urn": r[4],
                })
    return {"count": len(rows), "events": rows, "last_id": rows[-1]["id"] if rows else since_id}


# ---------- minimal MCP shim ----------
# Maps a few common MCP-style JSON-RPC calls to the REST endpoints.
# Phase 2: replace with full mcp-server-sdk.

@app.post("/mcp")
async def mcp_rpc(body: dict):
    method = body.get("method", "")
    params = body.get("params", {})
    handlers = {
        "agent.register": register,
        "agent.heartbeat": heartbeat,
        "agent.list": list_agents,
        "job.create": create_job,
        "job.list": list_jobs,
        "message.publish": publish_message,
        "message.list": list_messages,
    }
    if method not in handlers:
        return JSONResponse({"error": f"unknown method: {method}"}, status_code=404)
    # crude param coercion - production MCP would use proper validation
    try:
        if method == "agent.register":
            return await register(AgentRegister(**params))
        if method == "agent.heartbeat":
            return await heartbeat(AgentHeartbeat(**params))
        if method == "agent.list":
            return await list_agents(**params)
        if method == "job.create":
            return await create_job(JobCreate(**params))
        if method == "job.list":
            return await list_jobs(**params)
        if method == "message.publish":
            return await publish_message(MessagePublish(**params))
        if method == "message.list":
            return await list_messages(**params)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
