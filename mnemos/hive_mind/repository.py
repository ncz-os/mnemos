"""Hive Mind storage abstraction.

Phase 2 of the HYDRA → PYTHIA migration (per CLAUDE.md directive 12).
Phase 1 was the SQLite lift-and-shift onto PYTHIA. Phase 2 separates
the storage layer from the FastAPI service so the same hive can run
against either the existing aiosqlite file (single-host dev / fallback)
or the PYTHIA Oracle 23ai ORCLPDB1 (multi-host production with HA).

Scope of this module:
  * ``HiveMindRepository`` — Protocol describing every storage call the
    service makes. One source of truth so backend authors know the
    contract without reading 1500 lines of service.py.
  * ``SqliteHiveMindRepository`` — concrete backend that wraps the
    existing aiosqlite SQL. Initial implementation delegates to inline
    helpers in ``service.py``; subsequent commits will migrate those
    helpers into this class.
  * ``OracleHiveMindRepository`` — stub raising NotImplementedError so
    the dependency-injection wiring can be tested before the Oracle
    schema lands. Schema parity work tracked separately.

Non-goals:
  * No SQL is moved in this commit. The service still imports its
    aiosqlite helpers directly. Cut-over is one-method-per-commit so
    each change can be reverted without breaking the live hive.
  * No selection logic in this module. ``service.py`` chooses a backend
    via ``HIVE_REPO=sqlite|oracle`` env var at startup; that wiring
    lands when at least one method has migrated.

The Protocol surface intentionally mirrors the HTTP endpoint contract,
not the SQL shape, so a NoSQL or KV backend could implement it without
inheriting joins.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class HiveMindRepository(Protocol):
    """Backend-neutral storage contract for the GRAEAE Hive Mind service.

    Implementations MUST be safe to call concurrently from a single
    FastAPI app instance. Atomicity of the dequeue/claim path is the
    responsibility of the backend (SQLite uses BEGIN IMMEDIATE; Oracle
    will use SELECT ... FOR UPDATE SKIP LOCKED).
    """

    # ---------- lifecycle ----------

    async def init(self) -> None:
        """Apply schema + any additive migrations. Idempotent."""
        ...

    async def close(self) -> None:
        """Release pools / file handles. Idempotent."""
        ...

    # ---------- agents ----------

    async def register_agent(self, *, urn: str, kind: str, host: str,
                             runtime: str, model: Optional[str],
                             provider: Optional[str], cost_tier: Optional[str],
                             auth_method: Optional[str], autonomy_level: Optional[str],
                             pid: Optional[int], capabilities: list[str],
                             version: Optional[str], metadata: dict[str, Any]) -> None:
        ...

    async def heartbeat_agent(self, *, urn: str, ts: float) -> bool:
        """Return True if the agent row exists and was updated."""
        ...

    async def get_agent(self, urn: str) -> Optional[dict[str, Any]]:
        ...

    async def list_agents(self, *, status: Optional[str] = None,
                          kind: Optional[str] = None,
                          host: Optional[str] = None,
                          limit: int = 100) -> list[dict[str, Any]]:
        ...

    # ---------- jobs ----------

    async def create_job(self, *, job_id: str, submitter_urn: str,
                         parent_job_id: Optional[str], kind: str,
                         description: Optional[str], priority: int,
                         deadline: Optional[float],
                         required_capabilities: Optional[list[str]],
                         eligible_kinds: Optional[list[str]],
                         project: Optional[str],
                         max_cost_tier: str,
                         preferred_providers: Optional[list[str]],
                         preferred_models: Optional[list[str]],
                         mnemos_refs: Optional[list[str]],
                         depends_on: Optional[list[str]],
                         max_retries: int,
                         started_at: float) -> None:
        ...

    async def claim_next_job(self, *, agent_urn: str, agent_kind: str,
                             agent_capabilities: list[str],
                             agent_cost_tier: str) -> Optional[dict[str, Any]]:
        """Atomic dequeue. Returns full job row or None when nothing eligible."""
        ...

    async def update_job(self, *, job_id: str, status: str,
                         result: Optional[dict[str, Any]],
                         tokens_in: Optional[int], tokens_out: Optional[int],
                         result_mnemos_id: Optional[str],
                         ended_at: Optional[float],
                         estimated_cost_usd: Optional[float]) -> None:
        ...

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        ...

    async def list_jobs(self, *, status: Optional[str] = None,
                        kind: Optional[str] = None,
                        project: Optional[str] = None,
                        submitter_urn: Optional[str] = None,
                        claimed_by: Optional[str] = None,
                        limit: int = 100) -> list[dict[str, Any]]:
        ...

    # ---------- messages + events ----------

    async def post_message(self, *, msg_id: str, from_urn: str,
                           to_urn: Optional[str], in_reply_to: Optional[str],
                           topic: str, payload: dict[str, Any], ts: float) -> None:
        ...

    async def list_messages(self, *, topic: Optional[str] = None,
                            to_urn: Optional[str] = None,
                            limit: int = 100) -> list[dict[str, Any]]:
        ...

    async def emit_event(self, *, ts: float, kind: str,
                         payload: dict[str, Any]) -> None:
        ...

    async def tail_events(self, *, since_ts: Optional[float] = None,
                          limit: int = 500) -> list[dict[str, Any]]:
        ...

    # ---------- cache + stats ----------

    async def cache_lookup(self, cache_key: str) -> Optional[dict[str, Any]]:
        ...

    async def cache_store(self, *, cache_key: str, source_job_id: str,
                          result: dict[str, Any], provider: Optional[str],
                          model: Optional[str], result_mnemos_id: Optional[str],
                          stored_at: float) -> None:
        ...

    async def cache_record_hit(self, *, cache_key: str, cost_saved_usd: float) -> None:
        ...

    async def stats_costs(self, *, group_by: str = "provider",
                          since_hours: int = 24) -> dict[str, Any]:
        ...

    async def stats_workers(self, *, kind: Optional[str] = None,
                            include_system: bool = False,
                            top_n: int = 30) -> list[dict[str, Any]]:
        ...


class SqliteHiveMindRepository:
    """SQLite/aiosqlite backend.

    Methods migrate out of service.py one at a time so each change can
    be smoke-tested against the live hive on PYTHIA without flipping
    the whole queue at once. Unmigrated methods raise NotImplementedError
    via __getattr__ pointing back to this migration plan.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        raise NotImplementedError(
            "SqliteHiveMindRepository.init: migrate service.py lifespan SCHEMA "
            "+ ALTER TABLE block into this method, then have lifespan() call "
            "repo.init() instead of running SQL directly."
        )

    # ---------- agents (Phase 2 migration cut 1) ----------

    async def insert_agent(self, *, urn: str, kind: str, runtime: str,
                           model: str, provider: str, cost_tier: str,
                           autonomy_level: str, auth_method: str,
                           plan_cap_usd: float, host: str, session_id: str,
                           pid: Optional[int], capabilities: Optional[list[str]],
                           version: Optional[str], started_at: float,
                           last_heartbeat: float,
                           metadata: Optional[dict[str, Any]]) -> None:
        """Atomic insert of a newly-registered agent.

        Caller (service.register endpoint) handles all validation +
        urn/session minting + post-insert event emission. This method
        is the SQL-only seam — so the same shape can be re-implemented
        for Oracle (Phase 2 target backend) without dragging FastAPI
        validators into the data layer.
        """
        import json as _json
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO agents (urn, kind, runtime, model, provider, cost_tier, "
                "autonomy_level, auth_method, plan_cap_usd, plan_period_used_usd, "
                "host, session_id, pid, capabilities, version, started_at, "
                "last_heartbeat, status, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'online', ?)",
                (
                    urn, kind, runtime, model, provider, cost_tier, autonomy_level,
                    auth_method, plan_cap_usd,
                    host, session_id, pid,
                    _json.dumps(capabilities) if capabilities else None,
                    version, started_at, last_heartbeat,
                    _json.dumps(metadata) if metadata else None,
                ),
            )
            await db.commit()

    # ---------- jobs (Phase 2 migration cut 2) ----------

    async def insert_job_queued(self, *, job_id: str, submitter_urn: str,
                                parent_job_id: Optional[str], kind: str,
                                description: Optional[str], priority: int,
                                deadline: Optional[float],
                                required_capabilities: Optional[list[str]],
                                eligible_kinds: Optional[list[str]],
                                project: Optional[str], max_cost_tier: str,
                                preferred_providers: Optional[list[str]],
                                preferred_models: Optional[list[str]],
                                mnemos_refs: Optional[list[str]],
                                depends_on: Optional[list[str]],
                                max_retries: int, started_at: float) -> None:
        """Atomic insert of a new queued job. Cache-hit short-circuit path
        is NOT migrated here — that path writes status='done' with cached
        result + claimed_provider/model, very different shape. Cut 3 will
        give it a separate insert_job_cache_hit() method.
        """
        import json as _json
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO jobs (id, submitter_urn, parent_job_id, kind, description, "
                "priority, deadline, required_capabilities, eligible_kinds, project, "
                "max_cost_tier, preferred_providers, preferred_models, "
                "mnemos_refs, depends_on, max_retries, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    job_id, submitter_urn, parent_job_id, kind, description,
                    priority, deadline,
                    _json.dumps(required_capabilities) if required_capabilities else None,
                    _json.dumps(eligible_kinds) if eligible_kinds else None,
                    project,
                    max_cost_tier,
                    _json.dumps(preferred_providers) if preferred_providers else None,
                    _json.dumps(preferred_models) if preferred_models else None,
                    _json.dumps(mnemos_refs) if mnemos_refs else None,
                    _json.dumps(depends_on) if depends_on else None,
                    int(max_retries),
                    started_at,
                ),
            )
            await db.commit()

    async def insert_job_cache_hit(self, *, job_id: str, submitter_urn: str,
                                   parent_job_id: Optional[str], kind: str,
                                   description: Optional[str], priority: int,
                                   deadline: Optional[float],
                                   required_capabilities: Optional[list[str]],
                                   eligible_kinds: Optional[list[str]],
                                   project: Optional[str], max_cost_tier: str,
                                   preferred_providers: Optional[list[str]],
                                   preferred_models: Optional[list[str]],
                                   mnemos_refs: Optional[list[str]],
                                   started_at: float, ended_at: float,
                                   result: dict[str, Any], provider: Optional[str],
                                   model: Optional[str],
                                   result_mnemos_id: Optional[str]) -> None:
        """Cache-hit short-circuit insert: job is born status='done' with the
        prior result, no claim cycle. claimed_cost_tier hard-coded to 'A'
        and estimated_cost_usd to 0 because the work was never actually
        executed (cache served it).
        """
        import json as _json
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO jobs (id, submitter_urn, parent_job_id, kind, description, "
                "priority, deadline, required_capabilities, eligible_kinds, project, "
                "max_cost_tier, preferred_providers, preferred_models, mnemos_refs, "
                "status, started_at, ended_at, result, claimed_provider, claimed_model, "
                "claimed_cost_tier, estimated_cost_usd, result_mnemos_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, ?, 'A', 0, ?)",
                (
                    job_id, submitter_urn, parent_job_id, kind, description,
                    priority, deadline,
                    _json.dumps(required_capabilities) if required_capabilities else None,
                    _json.dumps(eligible_kinds) if eligible_kinds else None,
                    project, max_cost_tier,
                    _json.dumps(preferred_providers) if preferred_providers else None,
                    _json.dumps(preferred_models) if preferred_models else None,
                    _json.dumps(mnemos_refs) if mnemos_refs else None,
                    started_at, ended_at,
                    _json.dumps(result),
                    provider, model,
                    result_mnemos_id,
                ),
            )
            await db.commit()

    # ---------- atomic claim (Phase 2 migration cut 4) ----------

    async def find_and_claim_job(
        self, *, agent_urn: str, agent_kind: str,
        agent_caps: set[str], agent_runtime: str, agent_model: str,
        agent_provider: str, agent_tier: str,
        cost_tier_order: list[str],
        sub_throttled: bool,
        now: float,
    ) -> Optional[dict[str, Any]]:
        """Atomic dequeue. Owns the transaction because claim correctness
        IS storage semantics: dependency gates, retry backoff, and the
        UPDATE-WHERE-status='queued' race guard all live or die with
        the surrounding BEGIN IMMEDIATE.

        Returns the claimed job dict (same shape service expects to
        forward to the worker) or None when nothing is claimable.

        Filter chain executed in-order, cheapest first:
          1. DAG gate (depends_on all done)
          2. eligible_kinds membership
          3. required_capabilities subset (with "*" wildcard escape)
          4. cost-tier ceiling
          5. subscription throttle
          6. preferred_providers / preferred_models
        """
        import json as _json
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id, kind, description, priority, deadline, "
                    "required_capabilities, eligible_kinds, submitter_urn, "
                    "parent_job_id, started_at, max_cost_tier, "
                    "preferred_providers, preferred_models, mnemos_refs, depends_on "
                    "FROM jobs WHERE status='queued' "
                    "AND (retry_backoff_until IS NULL OR retry_backoff_until <= ?) "
                    "ORDER BY priority DESC, started_at ASC",
                    (now,),
                ) as cur:
                    async for r in cur:
                        (job_id, j_kind, j_desc, j_prio, j_dead, j_caps_json,
                         j_kinds_json, j_sub, j_par, j_started, j_max_tier,
                         j_pref_providers, j_pref_models, j_mnemos_refs,
                         j_deps_json) = r
                        # DAG gate
                        if j_deps_json:
                            deps = _json.loads(j_deps_json) or []
                            if deps:
                                ph = ",".join("?" * len(deps))
                                async with db.execute(
                                    f"SELECT COUNT(*) FROM jobs "
                                    f"WHERE id IN ({ph}) AND status='done'",
                                    tuple(deps),
                                ) as dc:
                                    done_count = (await dc.fetchone())[0]
                                if done_count < len(deps):
                                    continue
                        # eligible_kinds
                        if j_kinds_json:
                            kinds = _json.loads(j_kinds_json)
                            if kinds and agent_kind not in kinds:
                                continue
                        # required_capabilities (with "*" claim-any escape)
                        if j_caps_json and "*" not in agent_caps:
                            need = set(_json.loads(j_caps_json))
                            if not need.issubset(agent_caps):
                                continue
                        # cost-tier ceiling
                        job_max_tier = (j_max_tier or "A").upper()
                        if cost_tier_order.index(agent_tier) > cost_tier_order.index(job_max_tier):
                            continue
                        # subscription throttle (Anthropic Max past 85% MTD)
                        if sub_throttled and job_max_tier != "A":
                            continue
                        # preferred_providers
                        if j_pref_providers:
                            provs = _json.loads(j_pref_providers)
                            if provs and agent_provider not in provs:
                                continue
                        if j_pref_models:
                            models = _json.loads(j_pref_models)
                            if models and agent_model not in models:
                                continue
                        # match — claim with race guard
                        await db.execute(
                            "UPDATE jobs SET status='claimed', claimed_by=?, "
                            "claimed_at=?, claimed_runtime=?, claimed_model=?, "
                            "claimed_provider=?, claimed_cost_tier=? "
                            "WHERE id=? AND status='queued'",
                            (agent_urn, now, agent_runtime, agent_model,
                             agent_provider, agent_tier, job_id),
                        )
                        await db.execute("COMMIT")
                        return {
                            "id": job_id, "kind": j_kind,
                            "description": j_desc, "priority": j_prio,
                            "deadline": j_dead,
                            "submitter_urn": j_sub,
                            "parent_job_id": j_par,
                            "claimed_at": now, "queued_at": j_started,
                            "mnemos_refs": (
                                _json.loads(j_mnemos_refs) if j_mnemos_refs else []
                            ),
                            "claimed_resources": {
                                "runtime": agent_runtime, "model": agent_model,
                                "provider": agent_provider,
                                "cost_tier": agent_tier,
                            },
                        }
                await db.execute("COMMIT")
                return None
            except Exception:
                await db.execute("ROLLBACK")
                raise

    # Every other Protocol method raises NotImplementedError until
    # migrated. We don't list them here to keep the file scannable;
    # service.py will type-check against the Protocol so missing
    # methods surface as mypy/pyright errors at the call site, not
    # silent fallthrough at runtime.
    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(
            f"SqliteHiveMindRepository.{name} not yet migrated from service.py. "
            "See repository.py module docstring for the migration plan."
        )




def _epoch_to_tstz_sql() -> str:
    """SQL fragment converting an epoch-seconds bind variable into a TWTZ.

    Oracle has no ``FROM_UNIXTIME`` — we synthesise it from a fixed
    epoch anchor and ``NUMTODSINTERVAL``. Use as e.g.
    ``INSERT INTO ... VALUES (..., ``_epoch_to_tstz_sql()``, ...)`` with
    the matching bind name passed below.
    """
    return ("TIMESTAMP '1970-01-01 00:00:00 UTC' "
            "+ NUMTODSINTERVAL(:{bind}, 'SECOND')")


def _twtz_to_epoch(value: Any) -> Optional[float]:
    """Read-side: convert oracledb's TIMESTAMP WITH TIME ZONE (a
    ``datetime.datetime`` with tzinfo) into a Python unix epoch float.

    Returns ``None`` for SQL NULL.
    """
    if value is None:
        return None
    # oracledb hands TWTZ back as tz-aware datetime
    return value.timestamp()


def _json_or_none(value: Any) -> Optional[str]:
    """JSON-encode for CLOB columns, preserving NULL semantics."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _json.dumps(value)


class OracleHiveMindRepository:
    """PYTHIA Oracle 23ai backend (ORCLPDB1).

    Schema source-of-truth: ``db/migrations_oracle/0010_hive_mind.sql``
    + ``db/migrations_oracle/0011_hive_mind_extended_columns.sql``.

    Concurrency posture: the dequeue uses ``SELECT ... FOR UPDATE SKIP
    LOCKED`` so multiple workers can poll concurrently without blocking
    each other; the UPDATE then uses an ``AND status='queued'`` race
    guard as belt-and-suspenders (in practice the row lock already
    prevents the race, but the guard makes the intent explicit).
    """

    def __init__(self, dsn: str, *, user: Optional[str] = None,
                 password: Optional[str] = None) -> None:
        # Accept either:
        #   * full ``oracle://user:pass@host:port/service`` DSN
        #   * bare host + explicit user/password (legacy compatibility)
        self.dsn = dsn
        self._user = user
        self._password = password
        self._pool: Any = None

    async def init(self) -> None:
        """Open the oracledb async pool. Idempotent.

        Schema bootstrap is NOT done here — operators apply the
        ``db/migrations_oracle/0010_hive_mind.sql`` +
        ``0011_hive_mind_extended_columns.sql`` files via sqlplus once
        per Oracle instance. Auto-applying DDL on every API boot would
        fight with the existing mnemos migration runner.
        """
        if self._pool is not None:
            return
        from mnemos.persistence.oracle import make_oracle_pool  # local import
        # If caller supplied explicit user/password (legacy three-arg
        # constructor), splice them onto the DSN.
        dsn = self.dsn
        if self._user and self._password and "://" in dsn and "@" not in dsn.split("://", 1)[1].split("/", 1)[0]:
            scheme, rest = dsn.split("://", 1)
            dsn = f"{scheme}://{self._user}:{self._password}@{rest}"
        self._pool = make_oracle_pool(dsn)

    async def close(self) -> None:
        """Close the pool. Idempotent."""
        if self._pool is None:
            return
        try:
            await self._pool.close()
        finally:
            self._pool = None

    # ---------- agents ----------

    async def insert_agent(self, *, urn: str, kind: str, runtime: str,
                           model: str, provider: str, cost_tier: str,
                           autonomy_level: str, auth_method: str,
                           plan_cap_usd: float, host: str, session_id: str,
                           pid: Optional[int], capabilities: Optional[list[str]],
                           version: Optional[str], started_at: float,
                           last_heartbeat: float,
                           metadata: Optional[dict[str, Any]]) -> None:
        """Atomic insert of a newly-registered agent into ``hive_agents``."""
        if self._pool is None:
            await self.init()
        sql = (
            "INSERT INTO hive_agents ("
            "urn, kind, runtime, model, provider, cost_tier, autonomy_level, "
            "auth_method, plan_cap_usd, plan_period_used_usd, host, session_id, "
            "pid, capabilities, version, started_at, last_heartbeat, status, "
            "metadata) "
            "VALUES (:urn, :kind, :runtime, :model, :provider, :cost_tier, "
            ":autonomy_level, :auth_method, :plan_cap_usd, 0, :host, "
            ":session_id, :pid, :capabilities, :version, "
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:started_at, 'SECOND'), "
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:last_heartbeat, 'SECOND'), "
            "'online', :metadata)"
        )
        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(sql, {
                    "urn": urn, "kind": kind, "runtime": runtime, "model": model,
                    "provider": provider, "cost_tier": cost_tier,
                    "autonomy_level": autonomy_level, "auth_method": auth_method,
                    "plan_cap_usd": float(plan_cap_usd),
                    "host": host, "session_id": session_id, "pid": pid,
                    "capabilities": _json_or_none(capabilities),
                    "version": version,
                    "started_at": float(started_at),
                    "last_heartbeat": float(last_heartbeat),
                    "metadata": _json_or_none(metadata),
                })
            await conn.commit()

    # ---------- jobs ----------

    async def insert_job_queued(self, *, job_id: str, submitter_urn: str,
                                parent_job_id: Optional[str], kind: str,
                                description: Optional[str], priority: int,
                                deadline: Optional[float],
                                required_capabilities: Optional[list[str]],
                                eligible_kinds: Optional[list[str]],
                                project: Optional[str], max_cost_tier: str,
                                preferred_providers: Optional[list[str]],
                                preferred_models: Optional[list[str]],
                                mnemos_refs: Optional[list[str]],
                                depends_on: Optional[list[str]],
                                max_retries: int, started_at: float) -> None:
        """Atomic insert of a new queued job into ``hive_jobs``."""
        if self._pool is None:
            await self.init()
        sql = (
            "INSERT INTO hive_jobs ("
            "id, submitter_urn, parent_job_id, kind, description, priority, "
            "deadline, required_capabilities, eligible_kinds, project, "
            "max_cost_tier, preferred_providers, preferred_models, mnemos_refs, "
            "depends_on, max_retries, status, started_at) "
            "VALUES (:id, :submitter_urn, :parent_job_id, :kind, :description, "
            ":priority, "
            "CASE WHEN :deadline IS NULL THEN NULL ELSE "
            "  TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:deadline, 'SECOND') "
            "END, "
            ":required_capabilities, :eligible_kinds, :project, :max_cost_tier, "
            ":preferred_providers, :preferred_models, :mnemos_refs, :depends_on, "
            ":max_retries, 'queued', "
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:started_at, 'SECOND'))"
        )
        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(sql, {
                    "id": job_id, "submitter_urn": submitter_urn,
                    "parent_job_id": parent_job_id, "kind": kind,
                    "description": description, "priority": int(priority),
                    "deadline": float(deadline) if deadline is not None else None,
                    "required_capabilities": _json_or_none(required_capabilities),
                    "eligible_kinds": _json_or_none(eligible_kinds),
                    "project": project, "max_cost_tier": max_cost_tier,
                    "preferred_providers": _json_or_none(preferred_providers),
                    "preferred_models": _json_or_none(preferred_models),
                    "mnemos_refs": _json_or_none(mnemos_refs),
                    "depends_on": _json_or_none(depends_on),
                    "max_retries": int(max_retries),
                    "started_at": float(started_at),
                })
            await conn.commit()

    async def insert_job_cache_hit(self, *, job_id: str, submitter_urn: str,
                                   parent_job_id: Optional[str], kind: str,
                                   description: Optional[str], priority: int,
                                   deadline: Optional[float],
                                   required_capabilities: Optional[list[str]],
                                   eligible_kinds: Optional[list[str]],
                                   project: Optional[str], max_cost_tier: str,
                                   preferred_providers: Optional[list[str]],
                                   preferred_models: Optional[list[str]],
                                   mnemos_refs: Optional[list[str]],
                                   started_at: float, ended_at: float,
                                   result: dict[str, Any], provider: Optional[str],
                                   model: Optional[str],
                                   result_mnemos_id: Optional[str]) -> None:
        """Cache-hit short-circuit insert. Job is born status='done'."""
        if self._pool is None:
            await self.init()
        sql = (
            "INSERT INTO hive_jobs ("
            "id, submitter_urn, parent_job_id, kind, description, priority, "
            "deadline, required_capabilities, eligible_kinds, project, "
            "max_cost_tier, preferred_providers, preferred_models, mnemos_refs, "
            "status, started_at, ended_at, result, claimed_provider, "
            "claimed_model, claimed_cost_tier, estimated_cost_usd, result_mnemos_id) "
            "VALUES (:id, :submitter_urn, :parent_job_id, :kind, :description, "
            ":priority, "
            "CASE WHEN :deadline IS NULL THEN NULL ELSE "
            "  TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:deadline, 'SECOND') "
            "END, "
            ":required_capabilities, :eligible_kinds, :project, :max_cost_tier, "
            ":preferred_providers, :preferred_models, :mnemos_refs, "
            "'done', "
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:started_at, 'SECOND'), "
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:ended_at, 'SECOND'), "
            ":result, :provider, :model, 'A', 0, :result_mnemos_id)"
        )
        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(sql, {
                    "id": job_id, "submitter_urn": submitter_urn,
                    "parent_job_id": parent_job_id, "kind": kind,
                    "description": description, "priority": int(priority),
                    "deadline": float(deadline) if deadline is not None else None,
                    "required_capabilities": _json_or_none(required_capabilities),
                    "eligible_kinds": _json_or_none(eligible_kinds),
                    "project": project, "max_cost_tier": max_cost_tier,
                    "preferred_providers": _json_or_none(preferred_providers),
                    "preferred_models": _json_or_none(preferred_models),
                    "mnemos_refs": _json_or_none(mnemos_refs),
                    "started_at": float(started_at),
                    "ended_at": float(ended_at),
                    "result": _json.dumps(result),
                    "provider": provider, "model": model,
                    "result_mnemos_id": result_mnemos_id,
                })
            await conn.commit()

    # ---------- atomic claim ----------

    async def find_and_claim_job(
        self, *, agent_urn: str, agent_kind: str,
        agent_caps: set[str], agent_runtime: str, agent_model: str,
        agent_provider: str, agent_tier: str,
        cost_tier_order: list[str],
        sub_throttled: bool,
        now: float,
    ) -> Optional[dict[str, Any]]:
        """Atomic dequeue using ``SELECT ... FOR UPDATE SKIP LOCKED``.

        Unlike SQLite ``BEGIN IMMEDIATE`` which serialises every claimer,
        SKIP LOCKED lets N workers poll in parallel — each sees a
        disjoint subset of the queue. The candidate filter chain matches
        the SQLite implementation (DAG gate, eligible_kinds, capabilities,
        cost tier, throttle, preferred providers/models) so behavior is
        identical from the agent's perspective.
        """
        if self._pool is None:
            await self.init()
        now_epoch_sql = (
            "TIMESTAMP '1970-01-01 00:00:00 UTC' + NUMTODSINTERVAL(:now, 'SECOND')"
        )
        # Walk candidates in priority order. We page through ROWS_PER_BATCH at
        # a time, holding row locks on each batch via FOR UPDATE SKIP LOCKED.
        # Filter is in Python (matches the Sqlite implementation) so dequeue
        # semantics stay backend-neutral; in practice the queue is small enough
        # (<10k queued) that this is fine.
        ROWS_PER_BATCH = 200
        candidate_sql = (
            "SELECT id, kind, description, priority, deadline, "
            "required_capabilities, eligible_kinds, submitter_urn, "
            "parent_job_id, started_at, max_cost_tier, "
            "preferred_providers, preferred_models, mnemos_refs, depends_on "
            "FROM hive_jobs "
            "WHERE status = 'queued' "
            "AND (retry_backoff_until IS NULL OR retry_backoff_until <= "
            + now_epoch_sql + ") "
            "ORDER BY priority DESC, started_at ASC "
            "FETCH FIRST :batch ROWS ONLY "
            "FOR UPDATE SKIP LOCKED"
        )
        claim_sql = (
            "UPDATE hive_jobs SET "
            "status = 'claimed', claimed_by = :urn, "
            "claimed_at = " + now_epoch_sql + ", "
            "claimed_runtime = :runtime, claimed_model = :model, "
            "claimed_provider = :provider, claimed_cost_tier = :tier "
            "WHERE id = :id AND status = 'queued'"
        )

        async with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute(candidate_sql, {
                    "now": float(now), "batch": ROWS_PER_BATCH,
                })
                rows = await cur.fetchall()

                for r in rows:
                    (job_id, j_kind, j_desc, j_prio, j_dead, j_caps_json,
                     j_kinds_json, j_sub, j_par, j_started, j_max_tier,
                     j_pref_providers, j_pref_models, j_mnemos_refs,
                     j_deps_json) = r

                    # Materialise LOBs to Python strings before we touch them.
                    j_desc = await _read_lob(j_desc)
                    j_caps_json = await _read_lob(j_caps_json)
                    j_kinds_json = await _read_lob(j_kinds_json)
                    j_pref_providers = await _read_lob(j_pref_providers)
                    j_pref_models = await _read_lob(j_pref_models)
                    j_mnemos_refs = await _read_lob(j_mnemos_refs)
                    j_deps_json = await _read_lob(j_deps_json)

                    # DAG gate — all dependencies must be 'done'.
                    if j_deps_json:
                        deps = _json.loads(j_deps_json) or []
                        if deps:
                            ph = ", ".join(f":dep{i}" for i in range(len(deps)))
                            await cur.execute(
                                f"SELECT COUNT(*) FROM hive_jobs "
                                f"WHERE id IN ({ph}) AND status='done'",
                                {f"dep{i}": d for i, d in enumerate(deps)},
                            )
                            done_count = (await cur.fetchone())[0]
                            if done_count < len(deps):
                                continue

                    # eligible_kinds
                    if j_kinds_json:
                        kinds = _json.loads(j_kinds_json)
                        if kinds and agent_kind not in kinds:
                            continue

                    # required_capabilities (with "*" claim-any escape)
                    if j_caps_json and "*" not in agent_caps:
                        need = set(_json.loads(j_caps_json))
                        if not need.issubset(agent_caps):
                            continue

                    # cost-tier ceiling
                    job_max_tier = (j_max_tier or "A").upper()
                    if cost_tier_order.index(agent_tier) > cost_tier_order.index(job_max_tier):
                        continue

                    # subscription throttle
                    if sub_throttled and job_max_tier != "A":
                        continue

                    # preferred providers/models
                    if j_pref_providers:
                        provs = _json.loads(j_pref_providers)
                        if provs and agent_provider not in provs:
                            continue
                    if j_pref_models:
                        models = _json.loads(j_pref_models)
                        if models and agent_model not in models:
                            continue

                    # Match — claim with race guard. Even though SKIP LOCKED
                    # already excludes concurrent claimers, the WHERE
                    # status='queued' assertion is belt+suspenders.
                    await cur.execute(claim_sql, {
                        "urn": agent_urn, "now": float(now),
                        "runtime": agent_runtime, "model": agent_model,
                        "provider": agent_provider, "tier": agent_tier,
                        "id": job_id,
                    })
                    rowcount = cur.rowcount
                    if rowcount == 0:
                        # Lost race despite SKIP LOCKED (extremely rare —
                        # only happens if another path bypassed the lock).
                        continue
                    await conn.commit()
                    return {
                        "id": job_id, "kind": j_kind,
                        "description": j_desc, "priority": j_prio,
                        "deadline": _twtz_to_epoch(j_dead),
                        "submitter_urn": j_sub,
                        "parent_job_id": j_par,
                        "claimed_at": now,
                        "queued_at": _twtz_to_epoch(j_started),
                        "mnemos_refs": (
                            _json.loads(j_mnemos_refs) if j_mnemos_refs else []
                        ),
                        "claimed_resources": {
                            "runtime": agent_runtime, "model": agent_model,
                            "provider": agent_provider,
                            "cost_tier": agent_tier,
                        },
                    }

                # No claimable row in this batch — release row locks and
                # tell caller nothing was claimed.
                await conn.commit()
                return None


async def _read_lob(value: Any) -> Optional[str]:
    """Materialise a possibly-LOB column to ``str``.

    oracledb's async driver returns CLOBs as ``AsyncLOB`` proxies.
    Plain VARCHAR2 columns come through as ``str`` and pass through
    unchanged. ``None`` (SQL NULL) is preserved.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        return value
    read = getattr(value, "read", None)
    if read is None:
        return value
    res = read()
    # ``read()`` returns a coroutine on AsyncLOB; sync .read() on a
    # regular Cursor LOB returns the data directly.
    if hasattr(res, "__await__"):
        return await res
    return res


__all__ = [
    "HiveMindRepository",
    "SqliteHiveMindRepository",
    "OracleHiveMindRepository",
]
