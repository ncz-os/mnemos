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


class OracleHiveMindRepository:
    """PYTHIA Oracle 23ai backend (ORCLPDB1).

    Stub. The Oracle DDL parity work happens in a sibling commit:
      * memory_jobs table (PK uuidv7 stored as RAW(16))
      * memory_jobs_queue index (status, priority DESC, started_at)
      * memory_agents table mirroring SQLite shape
      * memory_messages + memory_events tables
      * SEQUENCE for events.id (Oracle has no AUTOINCREMENT)
      * SELECT ... FOR UPDATE SKIP LOCKED in claim_next_job
      * MERGE INTO for cache upsert
      * MERGE INTO for worker_kind_stats accumulation

    Until that lands, instantiating this class raises so the operator
    sees the gap at startup instead of mid-request.
    """

    def __init__(self, dsn: str, user: str, password: str) -> None:
        raise NotImplementedError(
            "OracleHiveMindRepository not implemented. See module docstring + "
            "CLAUDE.md directive 12 Phase 2. Use HIVE_REPO=sqlite for now."
        )


__all__ = [
    "HiveMindRepository",
    "SqliteHiveMindRepository",
    "OracleHiveMindRepository",
]
