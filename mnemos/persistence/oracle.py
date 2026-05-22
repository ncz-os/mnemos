"""Oracle persistence backend for MNEMOS.

Wraps a ``python-oracledb`` async connection pool and exposes
ABC-conformant subclasses for every repository surface
(``memories``, ``kg_triples``, ``memory_versions``, ``memory_branches``,
``compression``, ``webhooks``, ``consultations_audit``, ``federation``,
``state_kv``) plus :class:`OracleBackend` and a transactional context.

Each repo instantiates with full :class:`~abc.ABC` coverage. Methods
that have real implementations against the Oracle schema land in this
module; method bodies awaiting the Oracle 23ai VECTOR / Text rollout
or the namespace-policy visibility predicate raise
:class:`NotImplementedError` at call time (not attribute access), so
attribute lookups remain safe across the whole backend graph.

See ``docs/oracle-port-status.md`` for the running M7 plan.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import os
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.persistence.base import (
    BranchRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    FederationRepository,
    JournalRepository,
    KGRepository,
    MemoryRepository,
    PersistenceBackend,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

_LOG = logging.getLogger(__name__)

# Vector input validation caps. Per Oracle eng review O5: the
# vector literal sent to ``TO_VECTOR`` must be bounded (NaN/Inf
# rejected, dimensionality capped) to keep the cursor buffer
# predictable and to fail loud on caller bugs (e.g., a routing
# typo that hands the search call a tokenized text array).
_DEFAULT_VECTOR_DIM_MAX = 4096


def _vector_dim_max() -> int:
    """Resolve the per-call vector-dimensionality cap.

    Reads ``MNEMOS_VECTOR_DIM_MAX`` from the environment so operators
    can raise the cap for unusual embedding stacks without a code
    change. Falls back to :data:`_DEFAULT_VECTOR_DIM_MAX` if the env
    var is missing or unparsable.
    """
    raw = os.environ.get("MNEMOS_VECTOR_DIM_MAX", "").strip()
    if not raw:
        return _DEFAULT_VECTOR_DIM_MAX
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_VECTOR_DIM_MAX
    if parsed <= 0:
        return _DEFAULT_VECTOR_DIM_MAX
    return parsed


def _validate_and_format_vector(
    embedding: Sequence[float],
    expected_dim: int | None = None,
) -> str:
    """Validate ``embedding`` and emit the Oracle ``TO_VECTOR`` literal.

    The result is the bracketed comma-joined float string consumed by
    ``TO_VECTOR(:q)`` on Oracle 23ai and ``TO_VECTOR(:q)`` on Db2
    12.1.x (Oracle-compat mode). Reuses the same helper from
    :mod:`mnemos.persistence.db2` so both backends share one
    rejection contract.

    Raises:
        ValueError: if ``embedding`` is empty, contains NaN/Inf, has
            a length mismatching ``expected_dim``, or exceeds the
            ``MNEMOS_VECTOR_DIM_MAX`` cap.
    """
    if embedding is None:
        raise ValueError("embedding must not be None")
    # Materialize once — Sequence may be a generator-like view in
    # tests; the loop below would silently truncate otherwise.
    values = list(embedding)
    if not values:
        raise ValueError("embedding must not be empty")
    dim = len(values)
    cap = _vector_dim_max()
    if dim > cap:
        raise ValueError(
            f"embedding dimensionality {dim} exceeds MNEMOS_VECTOR_DIM_MAX "
            f"cap of {cap}; bump the env var if this is intentional."
        )
    if expected_dim is not None and dim != expected_dim:
        raise ValueError(f"embedding dimensionality mismatch: got {dim}, expected {expected_dim}.")
    formatted: list[str] = []
    for idx, value in enumerate(values):
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"embedding[{idx}] is not float-convertible: {value!r}") from exc
        if not math.isfinite(num):
            raise ValueError(f"embedding[{idx}] is non-finite ({num!r}); NaN and Inf are rejected.")
        formatted.append(f"{num:.7f}")
    return "[" + ",".join(formatted) + "]"


def _render_visibility(
    visibility: VisibilityFilter,
    *,
    table_alias: str = "",
    param_prefix: str = "vis",
) -> tuple[str, dict[str, Any]]:
    """Render a :class:`VisibilityFilter` into an Oracle WHERE clause.

    Mirrors the Postgres helper but emits named binds instead of $N.
    READABLE mirrors the v1_multiuser policy used by Postgres and
    SQLite: own rows, federation rows, world-readable rows, and
    group-readable rows, all namespace-pinned.
    """
    p = f"{table_alias}." if table_alias else ""

    if visibility.scope == VisibilityScope.ROOT_BYPASS:
        if visibility.namespace is None:
            return "", {}
        return f"{p}namespace = :{param_prefix}_ns", {f"{param_prefix}_ns": visibility.namespace}

    if visibility.namespace is None:
        return "1=0", {}

    if visibility.scope == VisibilityScope.OWN_ONLY:
        return (
            f"{p}owner_id = :{param_prefix}_owner AND {p}namespace = :{param_prefix}_ns",
            {
                f"{param_prefix}_owner": visibility.user_id,
                f"{param_prefix}_ns": visibility.namespace,
            },
        )

    group_ids = list(visibility.group_ids)
    params: dict[str, Any] = {
        f"{param_prefix}_owner": visibility.user_id,
        f"{param_prefix}_ns": visibility.namespace,
    }
    if group_ids:
        group_placeholders, group_params = _in_placeholders(group_ids, f"{param_prefix}_group")
        group_clause = f"{p}group_id IN ({group_placeholders})"
        params.update(group_params)
    else:
        group_clause = "0=1"

    return (
        "("
        f"{p}owner_id = :{param_prefix}_owner"
        f" OR {p}federation_source IS NOT NULL"
        f" OR MOD(NVL({p}permission_mode, 0), 10) >= 4"
        f" OR (MOD(TRUNC(NVL({p}permission_mode, 0) / 10), 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        f") AND {p}namespace = :{param_prefix}_ns",
        params,
    )


def _content_hash(content: Any) -> str:
    normalized = ("" if content is None else str(content)).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_unique_violation(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates a unique-constraint violation.

    Dialect-aware (Opus review O6 / Codex review A6) — covers Oracle's
    ``ORA-00001`` plus Db2's SQLSTATE ``23505`` and ``SQL0803N``. Used
    by federation duplicate-pull deduplication and any other path that
    swallows duplicate-key races as a soft no-op.
    """
    err = exc.args[0] if getattr(exc, "args", None) else None
    if getattr(err, "code", None) == 1:
        return True
    msg = str(exc)
    # Oracle
    if "ORA-00001" in msg:
        return True
    # Db2 — both SQLSTATE form and SQLCODE form
    if "SQLSTATE=23505" in msg or "SQL0803N" in msg:
        return True
    # Db2 unique-key violation also surfaces under -803
    if "SQLCODE=-803" in msg:
        return True
    return False


def _conn_from_tx(tx: Any) -> Any:
    """Resolve an oracledb connection from a backend-neutral tx handle."""
    if tx is None:
        return None
    return getattr(tx, "conn", tx)


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


async def _materialize_value(value: Any) -> Any:
    """Resolve oracledb async LOBs into plain strings/bytes.

    The python-oracledb async driver returns CLOB / BLOB columns as
    :class:`AsyncLOB` whose ``read()`` is a coroutine. The sync driver
    returns LOB objects whose ``read()`` is synchronous. This helper
    handles both shapes so callers always see a materialized value.
    """
    read = getattr(value, "read", None)
    if not callable(read):
        return value
    result = read()
    if inspect.isawaitable(result):
        return await result
    return result


async def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    names = [col[0].lower() for col in cursor.description]
    out: dict[str, Any] = {}
    for name, value in zip(names, row):
        out[name] = await _materialize_value(value)
    return out


def _parse_oracle_dsn(dsn: str) -> dict[str, Any]:
    """Parse oracle://user:pass@host:port/service into oracledb kwargs.

    Accepts ``oracle://`` and ``oracle+oracledb://``. Returns a dict
    suitable for ``oracledb.create_pool_async(**kwargs)``.
    """
    if "://" not in dsn:
        return {"dsn": dsn}
    scheme, rest = dsn.split("://", 1)
    parsed = urlparse(f"oracle://{rest}")
    host = parsed.hostname or "localhost"
    port = parsed.port or 1521
    service = (parsed.path or "/").lstrip("/") or "FREEPDB1"
    kwargs: dict[str, Any] = {
        "dsn": f"{host}:{port}/{service}",
    }
    if parsed.username:
        kwargs["user"] = unquote(parsed.username)
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    return kwargs


# Oracle eng review (2026-05-21, finding R1 "production pool posture"):
# bare ``create_pool_async`` leaves statement_cache_size unset, no
# session callback for NLS/PDB pinning, no DRCP support, no thick-mode
# init hook. Defaults here follow
# https://python-oracledb.readthedocs.io/en/latest/user_guide/connection_handling.html#connection-pooling
# and let operators override every knob via env vars without code change.

_DEFAULT_ORACLE_POOL_MIN = 2
_DEFAULT_ORACLE_POOL_MAX = 10
_DEFAULT_ORACLE_POOL_INCREMENT = 1
_DEFAULT_ORACLE_STMT_CACHE_SIZE = 20
_DEFAULT_ORACLE_POOL_ACQUIRE_TIMEOUT = 60.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %.1f", name, raw, default)
        return default


def _env_flag(name: str) -> bool:
    """Return True if the env var equals ``YES`` / ``1`` / ``TRUE`` (case-insensitive)."""
    return os.environ.get(name, "").strip().upper() in {"YES", "1", "TRUE", "ON"}


def _build_oracle_session_callback(settings: Any) -> Any:
    """Build a session callback that pins NLS + optionally switches PDB.

    Runs on every new physical session checkout. Both ALTER SESSION
    statements are best-effort: benign errors (e.g. ORA-65049 when
    already inside the target PDB) are logged at DEBUG and the session
    continues. This keeps the pool usable in mixed PDB/CDB topologies
    while still giving operators a defensive locale + container pin.
    """
    pdb_target = os.environ.get("MNEMOS_ORACLE_PDB", "").strip()

    def _session_callback(conn: Any, requested_tag: Any) -> None:
        cur = None
        try:
            cur = conn.cursor()
            # Defensive NLS pinning: prevents locale-dependent decimal
            # parsing of vector literals + numeric binds.
            try:
                cur.execute("ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '. '")
            except Exception as exc:  # pragma: no cover - driver-dependent
                _LOG.debug("ALTER SESSION SET NLS_NUMERIC_CHARACTERS failed: %s", exc)
            if pdb_target:
                try:
                    cur.execute(f"ALTER SESSION SET CONTAINER = {pdb_target}")
                except Exception as exc:  # pragma: no cover - driver-dependent
                    # ORA-65049 = already in the requested PDB; benign.
                    _LOG.debug(
                        "ALTER SESSION SET CONTAINER=%s failed (continuing): %s",
                        pdb_target,
                        exc,
                    )
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # pragma: no cover
                    pass

    return _session_callback


async def create_oracle_pool(
    dsn: str,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    increment: int | None = None,
    statement_cache_size: int | None = None,
    acquire_timeout: float | None = None,
    settings: Any | None = None,
) -> Any:
    """Create an oracledb async connection pool with production-grade defaults.

    Knobs (env-overridable; see ``docs/INSTALL.md`` Oracle section):

    * ``MNEMOS_ORACLE_POOL_MIN`` / ``MNEMOS_ORACLE_POOL_MAX`` /
      ``MNEMOS_ORACLE_POOL_INCREMENT`` — pool sizing.
    * ``MNEMOS_ORACLE_STMT_CACHE_SIZE`` — cursor cache per session
      (defaults to 20; raise for hot statement sites).
    * ``MNEMOS_ORACLE_POOL_ACQUIRE_TIMEOUT`` — seconds before an
      ``acquire()`` call gives up waiting (defaults to 60s; oracledb's
      built-in wait mode would otherwise block indefinitely).
    * ``MNEMOS_ORACLE_PDB`` — when set + DSN points at CDB$ROOT,
      session callback issues ``ALTER SESSION SET CONTAINER = <pdb>``.
    * ``MNEMOS_ORACLE_DRCP=YES`` — enable Database Resident Connection
      Pooling: pool advertises ``cclass='MNEMOS'`` + ``purity=SELF``;
      operator must also configure DRCP on the server side
      (``EXECUTE DBMS_CONNECTION_POOL.START_POOL``).
    * ``MNEMOS_ORACLE_THICK=YES`` — call ``oracledb.init_oracle_client``
      before pool creation. Requires Oracle Instant Client on the host;
      fails loud if unavailable so operators don't silently fall back
      to thin mode and lose thick-only features.

    See:
    https://python-oracledb.readthedocs.io/en/latest/user_guide/connection_handling.html#connection-pooling
    """
    import oracledb  # local import: optional dependency

    # Thick-mode init must happen before any pool creation; fail loud if
    # the operator asked for it but the Instant Client isn't present.
    if _env_flag("MNEMOS_ORACLE_THICK"):
        try:
            oracledb.init_oracle_client()
        except Exception as exc:
            raise RuntimeError(
                "MNEMOS_ORACLE_THICK=YES but oracledb.init_oracle_client() "
                "failed; install Oracle Instant Client or unset the env var."
            ) from exc

    pool_min = min_size if min_size is not None else _env_int("MNEMOS_ORACLE_POOL_MIN", _DEFAULT_ORACLE_POOL_MIN)
    pool_max = max_size if max_size is not None else _env_int("MNEMOS_ORACLE_POOL_MAX", _DEFAULT_ORACLE_POOL_MAX)
    pool_increment = (
        increment if increment is not None else _env_int("MNEMOS_ORACLE_POOL_INCREMENT", _DEFAULT_ORACLE_POOL_INCREMENT)
    )
    stmt_cache = (
        statement_cache_size
        if statement_cache_size is not None
        else _env_int("MNEMOS_ORACLE_STMT_CACHE_SIZE", _DEFAULT_ORACLE_STMT_CACHE_SIZE)
    )
    pool_acquire_timeout = (
        acquire_timeout
        if acquire_timeout is not None
        else _env_float(
            "MNEMOS_ORACLE_POOL_ACQUIRE_TIMEOUT",
            _DEFAULT_ORACLE_POOL_ACQUIRE_TIMEOUT,
        )
    )

    kwargs = _parse_oracle_dsn(dsn)
    kwargs.setdefault("min", pool_min)
    kwargs.setdefault("max", pool_max)
    kwargs.setdefault("increment", pool_increment)
    kwargs.setdefault("statement_cache_size", stmt_cache)
    # Explicit WAIT mode + timeout: default behaviour would otherwise
    # block forever when the pool is saturated.
    kwargs.setdefault("getmode", oracledb.POOL_GETMODE_WAIT)
    kwargs.setdefault("timeout", pool_acquire_timeout)
    kwargs.setdefault("session_callback", _build_oracle_session_callback(settings))

    # DRCP support: server must also be configured with
    # ``EXECUTE DBMS_CONNECTION_POOL.START_POOL`` for these to take effect.
    if _env_flag("MNEMOS_ORACLE_DRCP"):
        kwargs.setdefault("cclass", "MNEMOS")
        purity_self = getattr(oracledb, "PURITY_SELF", None)
        if purity_self is not None:
            kwargs.setdefault("purity", purity_self)

    return oracledb.create_pool_async(**kwargs)


class _OracleTransaction:
    """Backend-neutral transaction handle wrapping an oracledb connection."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def conn(self) -> Any:
        return self._conn

    async def commit(self) -> None:
        if self._closed:
            return
        result = self._conn.commit()
        if inspect.isawaitable(result):
            await result
        self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        result = self._conn.rollback()
        if inspect.isawaitable(result):
            await result
        self._closed = True


def _stub_method(method_name: str):
    """Build a coroutine stub that raises NotImplementedError on call."""

    async def _stub(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__}.{method_name} is not implemented yet — "
            "P1 of the Oracle port. See docs/oracle-port-status.md."
        )

    _stub.__name__ = method_name
    return _stub


class OracleKGRepository(KGRepository):
    """Oracle KG triples repo — minimal coverage for insert + fetch-by-id."""

    async def insert_kg_triple(
        self,
        tx: Transaction,
        *,
        triple_id: str,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str | None,
        object_type: str | None,
        valid_from: Any,
        valid_until: Any,
        memory_id: str | None,
        confidence: float | None,
        created: Any,
        owner_id: str,
        namespace: str | None,
    ) -> str:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        affected = 0
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO kg_triples (
                    id, subject, predicate, object, subject_type, object_type,
                    valid_from, valid_until, memory_id, confidence, created,
                    owner_id, namespace
                )
                SELECT
                    :id, :subject, :predicate, :object, :subject_type, :object_type,
                    NVL(CAST(:valid_from AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP),
                    CAST(:valid_until AS TIMESTAMP WITH TIME ZONE),
                    :memory_id,
                    NVL(CAST(:confidence AS NUMBER), 1.0),
                    NVL(CAST(:created AS DATE), SYSDATE),
                    :owner_id, NVL(:namespace, 'default')
                FROM dual
                WHERE NOT EXISTS (SELECT 1 FROM kg_triples WHERE id = :id)
                """,
                {
                    "id": triple_id,
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "subject_type": subject_type,
                    "object_type": object_type,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "memory_id": memory_id,
                    "confidence": confidence,
                    "created": created,
                    "owner_id": owner_id,
                    "namespace": namespace,
                },
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_kg_triple_by_id(self, tx: Transaction, triple_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, subject, predicate, object, subject_type, object_type,
                       valid_from, valid_until, memory_id, confidence,
                       owner_id, namespace, metadata,
                       created, deleted_at
                  FROM kg_triples
                 WHERE id = :id AND deleted_at IS NULL
                """,
                {"id": triple_id},
            )
            row = await _call(cursor.fetchone)
            return await _row_to_dict(cursor, row)
        finally:
            await _call(cursor.close)

    async def fetch_kg_triples_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        include_unattached: bool,
        hard_limit: int,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where: list[str] = ["deleted_at IS NULL"]
            params: dict[str, Any] = {}
            if memory_ids:
                placeholders, mid_params = _in_placeholders(memory_ids, "mid")
                if include_unattached:
                    where.append(f"(memory_id IS NULL OR memory_id IN ({placeholders}))")
                else:
                    where.append(f"memory_id IN ({placeholders})")
                params.update(mid_params)
            elif include_unattached:
                where.append("memory_id IS NULL")
            else:
                return []
            if effective_owner:
                where.append("owner_id = :owner_id")
                params["owner_id"] = effective_owner
            if effective_ns:
                where.append("namespace = :ns")
                params["ns"] = effective_ns
            sql = (
                "SELECT id, subject, predicate, object, subject_type, object_type, "
                "valid_from, valid_until, memory_id, confidence, created, owner_id, "
                "namespace FROM kg_triples WHERE " + " AND ".join(where) + " "
                f"FETCH FIRST {int(hard_limit) + 1} ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class OracleVersionRepository(VersionRepository):
    """Oracle memory_versions repo — minimal coverage for insert + fetch-by-id."""

    async def insert_memory_version(
        self,
        tx: Transaction,
        *,
        version_id: str,
        memory_id: str,
        version_num: int,
        content: str,
        category: str | None,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str | None,
        owner_id: str,
        namespace: str | None,
        permission_mode: int | None,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        snapshot_at: Any,
        snapshot_by: str | None,
        change_type: str | None,
        commit_hash: str | None,
        parent_version_id: str | None,
        branch: str | None,
        merge_parents: Any,
    ) -> str:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        affected = 0
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_versions (
                    id, memory_id, version_num, content, category, subcategory,
                    metadata, verbatim_content, owner_id, namespace,
                    permission_mode, source_model, source_provider, source_session,
                    source_agent, snapshot_at, snapshot_by, change_type,
                    commit_hash, parent_version_id, branch, merge_parents
                )
                SELECT
                    :id, :memory_id, :version_num, :content, :category, :subcategory,
                    :metadata, :verbatim_content, :owner_id,
                    NVL(:namespace, 'default'),
                    NVL(CAST(:permission_mode AS NUMBER), 600),
                    :source_model, :source_provider,
                    :source_session, :source_agent,
                    NVL(CAST(:snapshot_at AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP),
                    :snapshot_by,
                    NVL(:change_type, 'create'),
                    :commit_hash, :parent_version_id,
                    NVL(:branch, 'main'),
                    :merge_parents
                FROM dual
                WHERE NOT EXISTS (SELECT 1 FROM memory_versions WHERE id = :id)
                """,
                {
                    "id": version_id,
                    "memory_id": memory_id,
                    "version_num": version_num,
                    "content": content,
                    "category": category,
                    "subcategory": subcategory,
                    "metadata": metadata_json,
                    "verbatim_content": verbatim_content,
                    "owner_id": owner_id,
                    "namespace": namespace,
                    "permission_mode": permission_mode,
                    "source_model": source_model,
                    "source_provider": source_provider,
                    "source_session": source_session,
                    "source_agent": source_agent,
                    "snapshot_at": snapshot_at,
                    "snapshot_by": snapshot_by,
                    "change_type": change_type,
                    "commit_hash": commit_hash,
                    "parent_version_id": parent_version_id,
                    "branch": branch,
                    "merge_parents": merge_parents,
                },
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_memory_version_by_id(self, tx: Transaction, version_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, memory_id, version_num, content, category, subcategory,
                       metadata, verbatim_content, owner_id, namespace,
                       permission_mode, source_model, source_provider,
                       source_session, source_agent, snapshot_at, snapshot_by,
                       change_type, commit_hash, parent_version_id, branch,
                       merge_parents, deleted_at
                  FROM memory_versions
                 WHERE id = :id AND deleted_at IS NULL
                """,
                {"id": version_id},
            )
            row = await _call(cursor.fetchone)
            out = await _row_to_dict(cursor, row)
            if out is not None and isinstance(out.get("merge_parents"), str):
                try:
                    out["merge_parents"] = json.loads(out["merge_parents"])
                except json.JSONDecodeError:
                    pass
            return out
        finally:
            await _call(cursor.close)

    async def fetch_memory_versions_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        effective_ns: str | None,
        hard_limit: int,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(memory_ids, "mid")
            where = ["deleted_at IS NULL", f"memory_id IN ({placeholders})"]
            if effective_owner:
                where.append("owner_id = :owner_id")
                params["owner_id"] = effective_owner
            if effective_ns:
                where.append("namespace = :ns")
                params["ns"] = effective_ns
            sql = (
                "SELECT id, memory_id, version_num, content, category, subcategory, "
                "metadata, verbatim_content, owner_id, namespace, permission_mode, "
                "source_model, source_provider, source_session, source_agent, "
                "snapshot_at, snapshot_by, change_type, commit_hash, parent_version_id, "
                "branch, merge_parents "
                "FROM memory_versions WHERE " + " AND ".join(where) + " "
                "ORDER BY memory_id ASC, branch ASC, version_num ASC "
                f"FETCH FIRST {int(hard_limit) + 1} ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_memory_versions_by_ids(self, tx: Transaction, version_ids: Sequence[str]) -> list[Row]:
        if not version_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(version_ids, "vid")
            sql = (
                f"SELECT id, memory_id, owner_id, namespace FROM memory_versions "
                f"WHERE id IN ({placeholders}) AND deleted_at IS NULL"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class OracleBranchRepository(BranchRepository):
    """Oracle memory_branches repo — supports upsert head, stubs others."""

    async def upsert_memory_branch_head(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        branch: str,
        head_version_id: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO memory_branches m
                USING (SELECT :memory_id AS memory_id, :name AS name FROM dual) s
                   ON (m.memory_id = s.memory_id AND m.name = s.name)
                WHEN MATCHED THEN
                    UPDATE SET head_version_id = :head_version_id
                WHEN NOT MATCHED THEN
                    INSERT (id, memory_id, name, head_version_id)
                    VALUES (:branch_id, :memory_id, :name, :head_version_id)
                """,
                {
                    "memory_id": memory_id,
                    "name": branch,
                    "branch_id": f"{memory_id}:{branch}",
                    "head_version_id": head_version_id,
                },
            )
        finally:
            await _call(cursor.close)

    async def fetch_memory_branch_heads(
        self,
        tx: Transaction,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            mid_ph, params = _in_placeholders(memory_ids, "mid")
            where = [f"memory_id IN ({mid_ph})", "deleted_at IS NULL"]
            if authorized_version_uuids is not None:
                vid_ph, vid_params = _in_placeholders(authorized_version_uuids, "vid")
                if not vid_ph:
                    return []
                where.append(f"id IN ({vid_ph})")
                params.update(vid_params)
            sql = (
                "SELECT memory_id, branch, head_version_id FROM ("
                "  SELECT memory_id, branch, id AS head_version_id, "
                "         ROW_NUMBER() OVER ("
                "             PARTITION BY memory_id, branch "
                "             ORDER BY version_num DESC"
                "         ) AS rn "
                "  FROM memory_versions"
                "  WHERE " + " AND ".join(where) + ""
                ") WHERE rn = 1"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_memory_branches_for_memories(self, tx: Transaction, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return None
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(memory_ids, "mid")
            await _call(
                cursor.execute,
                f"DELETE FROM memory_branches WHERE memory_id IN ({placeholders})",
                params,
            )
        finally:
            await _call(cursor.close)
        return None

    async def create_memory_branch(
        self,
        tx: Transaction,
        memory_id: str,
        name: str,
        from_commit: str | None,
        user: Any,
    ) -> dict[str, Any]:
        _ = user
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            head_version_id: str | None = None
            if from_commit is not None:
                await _call(
                    cursor.execute,
                    """
                    SELECT id FROM memory_versions
                     WHERE memory_id = :memory_id
                       AND commit_hash = :commit
                       AND deleted_at IS NULL
                       FETCH FIRST 1 ROWS ONLY
                    """,
                    {"memory_id": memory_id, "commit": from_commit},
                )
                row = await _call(cursor.fetchone)
                if row is not None:
                    head_version_id = row[0]
            branch_id = f"{memory_id}:{name}"
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_branches (id, memory_id, name, head_version_id)
                VALUES (:id, :memory_id, :name, :head_version_id)
                """,
                {
                    "id": branch_id,
                    "memory_id": memory_id,
                    "name": name,
                    "head_version_id": head_version_id,
                },
            )
            return {
                "id": branch_id,
                "memory_id": memory_id,
                "name": name,
                "head_version_id": head_version_id,
            }
        finally:
            await _call(cursor.close)


async def _fetch_all_dicts(cursor: Any) -> list[dict[str, Any]]:
    rows = await _call(cursor.fetchall)
    out: list[dict[str, Any]] = []
    for raw in rows or []:
        d = await _row_to_dict(cursor, raw)
        if d is not None:
            out.append(d)
    return out


def _in_placeholders(ids: Sequence[str], prefix: str = "id") -> tuple[str, dict[str, Any]]:
    """Build Oracle ``IN (:id0, :id1, ...)`` placeholders + params dict.

    Oracle has no array bind in the thin client; expand to named binds.
    Returns ``("", {})`` for empty input so callers can short-circuit
    to ``[]`` rather than emit ``IN ()`` (Oracle syntax error).
    """
    if not ids:
        return "", {}
    placeholders = ",".join(f":{prefix}{i}" for i in range(len(ids)))
    params = {f"{prefix}{i}": value for i, value in enumerate(ids)}
    return placeholders, params


class OracleMemoryRepository(MemoryRepository):
    """Oracle memories repo — ABC-conformant.

    Implements the lightweight CRUD surface that maps cleanly to the
    Oracle schema in ``db/migrations_oracle/0001_core_schema.sql``.
    Visibility-filtered, vector, and FTS paths are stubbed pending
    Oracle 23ai VECTOR setup and a namespace-policy translation of the
    Postgres ``read_visibility_predicate`` helper (P1 follow-up).
    """

    async def insert_memory(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        quality_rating: int,
        owner_id: str,
        namespace: str,
        permission_mode: int,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        verbatim_content: str | None,
        created: Any,
        updated: Any,
    ) -> str:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        affected = 0
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memories (
                    id, content, category, subcategory, metadata, content_hash,
                    quality_rating, verbatim_content, owner_id, namespace, permission_mode,
                    source_model, source_provider, source_session, source_agent,
                    created, updated
                )
                SELECT
                    :id, :content, :category, :subcategory, :metadata, :content_hash,
                    :quality_rating, :verbatim_content, :owner_id, :namespace, :permission_mode,
                    :source_model, :source_provider, :source_session, :source_agent,
                    NVL(CAST(:created AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP),
                    NVL(CAST(:updated AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)
                FROM dual
                WHERE NOT EXISTS (SELECT 1 FROM memories WHERE id = :id)
                """,
                {
                    "id": memory_id,
                    "content": content,
                    "category": category,
                    "subcategory": subcategory,
                    "metadata": metadata_json,
                    "content_hash": _content_hash(content),
                    "quality_rating": quality_rating,
                    "verbatim_content": verbatim_content,
                    "owner_id": owner_id,
                    "namespace": namespace,
                    "permission_mode": permission_mode,
                    "source_model": source_model,
                    "source_provider": source_provider,
                    "source_session": source_session,
                    "source_agent": source_agent,
                    "created": created,
                    "updated": updated,
                },
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_memory_by_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, content, category, subcategory, metadata, quality_rating,
                       compressed_content, verbatim_content, owner_id, namespace,
                       permission_mode, source_model, source_provider, source_session,
                       source_agent, group_id, created, updated, archived_at, deleted_at
                  FROM memories
                 WHERE id = :id AND deleted_at IS NULL
                """,
                {"id": memory_id},
            )
            row = await _call(cursor.fetchone)
            return await _row_to_dict(cursor, row)
        finally:
            await _call(cursor.close)

    async def set_suppress_version_snapshot(self, tx: Transaction) -> None:
        # No-op for Oracle — Postgres uses a session GUC to bypass the
        # version-snapshot trigger; the Oracle schema has no equivalent
        # trigger yet, so suppression is implicit.
        return None

    async def fetch_versioned_memory_ids(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ",".join(f":id{i}" for i in range(len(memory_ids)))
            params = {f"id{i}": mid for i, mid in enumerate(memory_ids)}
            await _call(
                cursor.execute,
                f"""
                SELECT DISTINCT memory_id
                  FROM memory_versions
                 WHERE memory_id IN ({placeholders})
                   AND deleted_at IS NULL
                """,
                params,
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_memory_head_checks(self, tx: Transaction, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ",".join(f":id{i}" for i in range(len(memory_ids)))
            params = {f"id{i}": mid for i, mid in enumerate(memory_ids)}
            await _call(
                cursor.execute,
                f"""
                SELECT m.id, m.content AS memory_content, mv.content AS head_content
                  FROM memories m
                  LEFT JOIN memory_branches b
                    ON b.memory_id = m.id
                   AND b.name = 'main'
                  LEFT JOIN memory_versions mv
                    ON mv.id = b.head_version_id
                   AND mv.deleted_at IS NULL
                 WHERE m.id IN ({placeholders})
                   AND m.deleted_at IS NULL
                """,
                params,
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def gather_stats(self, tx: Transaction):
        from mnemos.persistence.base import MemoryStatsRow

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN metadata IS NULL OR DBMS_LOB.GETLENGTH(metadata) = 0
                              OR INSTR(metadata, '\"federation_origin\"') = 0
                             THEN 1 ELSE 0 END) AS native_count,
                    SUM(CASE WHEN INSTR(metadata, '\"federation_origin\"') > 0
                             THEN 1 ELSE 0 END) AS federated_count,
                    AVG(quality_rating) AS avg_quality
                  FROM memories
                 WHERE deleted_at IS NULL
                """,
            )
            row = await _call(cursor.fetchone) or (0, 0, 0, None)
            total, native, federated, avg_q = row
            await _call(
                cursor.execute,
                """
                SELECT category, COUNT(*)
                  FROM memories
                 WHERE deleted_at IS NULL AND category IS NOT NULL
                 GROUP BY category
                """,
            )
            by_cat: dict[str, int] = {}
            for cat, n in await _call(cursor.fetchall) or []:
                by_cat[str(cat)] = int(n)
            return MemoryStatsRow(
                total_memories=int(total or 0),
                native_memories=int(native or 0),
                federated_memories=int(federated or 0),
                memories_by_category=by_cat,
                avg_quality_rating=float(avg_q) if avg_q is not None else None,
            )
        finally:
            await _call(cursor.close)

    async def get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.id = :id", "m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if clause:
                where.append(clause)
            params["id"] = memory_id
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, m.group_id, "
                "m.created, m.updated, m.archived_at, m.deleted_at, "
                "m.recall_count, m.last_recalled_at, m.content_hash, "
                "m.federation_source, m.federation_remote_updated "
                "FROM memories m WHERE " + " AND ".join(where)
            )
            await _call(cursor.execute, sql, params)
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def list_memories(
        self,
        tx: Transaction,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> tuple[list[Row], int]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if clause:
                where.append(clause)
            if category is not None:
                where.append("m.category = :cat")
                params["cat"] = category
            if subcategory is not None:
                where.append("m.subcategory = :sub")
                params["sub"] = subcategory
            where_sql = " AND ".join(where)

            await _call(
                cursor.execute,
                f"SELECT COUNT(*) FROM memories m WHERE {where_sql}",
                params,
            )
            (total,) = await _call(cursor.fetchone) or (0,)

            page_params = dict(params, limit=limit, offset=offset)
            await _call(
                cursor.execute,
                f"""
                SELECT m.id, m.content, m.category, m.subcategory, m.metadata,
                       m.quality_rating, m.compressed_content, m.verbatim_content,
                       m.owner_id, m.namespace, m.permission_mode, m.source_model,
                       m.source_provider, m.source_session, m.source_agent,
                       m.group_id, m.created, m.updated, m.archived_at, m.deleted_at
                  FROM memories m
                 WHERE {where_sql}
                 ORDER BY m.created DESC, m.id ASC
                 OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
                """,
                page_params,
            )
            rows = await _fetch_all_dicts(cursor)
            return rows, int(total or 0)
        finally:
            await _call(cursor.close)

    _UPDATABLE_FIELDS = frozenset(
        {
            "content",
            "category",
            "subcategory",
            "metadata",
            "quality_rating",
            "compressed_content",
            "verbatim_content",
            "permission_mode",
            "source_model",
            "source_provider",
            "source_session",
            "source_agent",
            "group_id",
            "archived_at",
        }
    )

    async def update_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        fields: dict[str, Any],
    ) -> Row | None:
        if not fields:
            return await self.get_memory(tx, memory_id, visibility=visibility)
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            sets: list[str] = []
            params: dict[str, Any] = {}
            for key, value in fields.items():
                if key not in self._UPDATABLE_FIELDS:
                    continue
                sets.append(f"{key} = :f_{key}")
                params[f"f_{key}"] = value
            if "content" in fields and "content" in self._UPDATABLE_FIELDS:
                sets.append("content_hash = :f_content_hash")
                params["f_content_hash"] = _content_hash(fields["content"])
            if not sets:
                return await self.get_memory(tx, memory_id, visibility=visibility)
            sets.append("updated = SYSTIMESTAMP")

            clause, vis_params = _render_visibility(visibility)
            where = ["id = :id", "deleted_at IS NULL"]
            if clause:
                where.append(clause)
            params.update(vis_params)
            params["id"] = memory_id

            sql = f"UPDATE memories SET {', '.join(sets)} WHERE " + " AND ".join(where)
            await _call(cursor.execute, sql, params)
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_memory(tx, memory_id, visibility=visibility)

    async def delete_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        requested_by: str | None = None,
        requested_at: Any = None,
        request_kind: str = "admin_purge",
        reason: str | None = None,
        source: Sequence[str] | None = None,
    ) -> Row | None:
        _ = (requested_by, requested_at, request_kind, reason, source)
        row = await self.get_memory(tx, memory_id, visibility=visibility, include_archived=True)
        if row is None:
            return None
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility)
            where = ["id = :id", "deleted_at IS NULL"]
            if clause:
                where.append(clause)
            vis_params["id"] = memory_id
            await _call(
                cursor.execute,
                "UPDATE memories SET deleted_at = SYSTIMESTAMP WHERE " + " AND ".join(where),
                vis_params,
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return row

    async def fts_search(
        self,
        tx: Transaction,
        *,
        query: str,
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if clause:
                where.append(clause)
            # DBMS_LOB.INSTR is a substring locator, not a LIKE pattern —
            # no wildcards. Strip them defensively in case the caller
            # passes a LIKE-shaped query from a sqlite-targeted helper.
            params["q"] = query.strip().strip("%")
            where.append("DBMS_LOB.INSTR(m.content, :q) > 0")
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    where.append(f"m.{col} = :flt_{col}")
                    params[f"flt_{col}"] = val
            params["limit"] = limit
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.owner_id, m.namespace, m.created, m.updated "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                "ORDER BY m.updated DESC FETCH FIRST :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def assert_memory_readable(self, tx: Transaction, memory_id: str, user: Any) -> None:
        # Lightweight check used by domain code; relies on a READ
        # visibility constructed by the caller. We re-create a permissive
        # READABLE filter from the user context when called directly.
        from mnemos.core.auth_context import UserContext

        if isinstance(user, UserContext):
            ns = getattr(user, "namespace", None) or "default"
            visibility = VisibilityFilter.for_read(user, namespace=ns)
        else:
            visibility = VisibilityFilter(
                scope=VisibilityScope.ROOT_BYPASS,
                user_id=None,
                group_ids=(),
                namespace=None,
            )
        row = await self.get_memory(tx, memory_id, visibility=visibility, include_archived=True)
        if row is None:
            raise PermissionError("Memory not found")

    async def fetch_memory_export(
        self,
        tx: Transaction,
        *,
        effective_owner: str | None,
        effective_ns: str | None,
        category: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = ["deleted_at IS NULL"]
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if effective_owner:
                where.append("owner_id = :owner_id")
                params["owner_id"] = effective_owner
            if effective_ns:
                where.append("namespace = :ns")
                params["ns"] = effective_ns
            if category:
                where.append("category = :cat")
                params["cat"] = category
            sql = (
                "SELECT id, content, category, subcategory, created, updated, "
                "owner_id, namespace, permission_mode, quality_rating, "
                "source_model, source_provider, source_session, source_agent, "
                "metadata "
                "FROM memories WHERE " + " AND ".join(where) + " "
                "ORDER BY created ASC "
                "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Transaction,
        *,
        referenced_ids: Sequence[str],
        scope_owner: str | None = None,
        scope_namespace: str | None = None,
    ) -> list[Row]:
        if not referenced_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(referenced_ids, "ref")
            where = [f"id IN ({placeholders})", "deleted_at IS NULL"]
            if scope_owner is not None:
                where.append("owner_id = :scope_owner")
                params["scope_owner"] = scope_owner
            if scope_namespace is not None:
                where.append("namespace = :scope_ns")
                params["scope_ns"] = scope_namespace
            sql = "SELECT id, owner_id, namespace FROM memories WHERE " + " AND ".join(where)
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        content_hash: str,
        cross_namespace: bool = False,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = [
                "deleted_at IS NULL",
                "archived_at IS NULL",
                "content_hash = :h",
                "owner_id = :owner_id",
            ]
            params = {"h": content_hash, "owner_id": owner_id}
            if not cross_namespace:
                where.append("namespace = :ns")
                params["ns"] = namespace
            sql = (
                "SELECT id, content, category, subcategory, owner_id, namespace, "
                "created, updated FROM memories WHERE " + " AND ".join(where) + " "
                "FETCH FIRST 1 ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def fetch_memory_log(
        self,
        tx: Transaction,
        memory_id: str,
        branch: str,
        limit: int,
        user: Any,
    ) -> list[Row]:
        # Simplified vs the Postgres recursive-CTE walk: returns the
        # latest N versions on this branch ordered by version_num DESC.
        # Handler-level visibility is honoured by callers via the
        # higher-level assert_memory_readable check; per-version
        # namespace predicates land with the v1_multiuser group-policy
        # port (P1.4 follow-up).
        _ = user
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, memory_id, version_num, content, commit_hash,
                       parent_version_id, branch, snapshot_at, snapshot_by,
                       change_type, category, subcategory, owner_id, namespace
                  FROM memory_versions
                 WHERE memory_id = :memory_id
                   AND branch = :branch
                   AND deleted_at IS NULL
                 ORDER BY version_num DESC
                 FETCH FIRST :limit ROWS ONLY
                """,
                {"memory_id": memory_id, "branch": branch, "limit": limit},
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_diff_commit_pair(
        self,
        tx: Transaction,
        memory_id: str,
        commit_a: str,
        commit_b: str,
        user: Any,
    ) -> tuple[Row | None, Row | None]:
        _ = user
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            sql = (
                "SELECT content, version_num FROM memory_versions "
                "WHERE memory_id = :memory_id AND commit_hash = :commit "
                "AND deleted_at IS NULL"
            )
            await _call(cursor.execute, sql, {"memory_id": memory_id, "commit": commit_a})
            row_a = await _row_to_dict(cursor, await _call(cursor.fetchone))
            await _call(cursor.execute, sql, {"memory_id": memory_id, "commit": commit_b})
            row_b = await _row_to_dict(cursor, await _call(cursor.fetchone))
            return row_a, row_b
        finally:
            await _call(cursor.close)

    async def fetch_checkout_commit(
        self,
        tx: Transaction,
        memory_id: str,
        commit_hash: str,
        user: Any,
    ) -> Row | None:
        _ = user
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT commit_hash, version_num, branch, category, subcategory,
                       content, change_type, snapshot_at, snapshot_by
                  FROM memory_versions
                 WHERE memory_id = :memory_id
                   AND commit_hash = :commit
                   AND deleted_at IS NULL
                """,
                {"memory_id": memory_id, "commit": commit_hash},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def bump_recall_and_get_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility)
            where = ["id = :id", "deleted_at IS NULL"]
            if clause:
                where.append(clause)
            vis_params["id"] = memory_id
            await _call(
                cursor.execute,
                """
                UPDATE memories SET
                    recall_count = NVL(recall_count, 0) + 1,
                    last_recalled_at = SYSTIMESTAMP
                 WHERE """
                + " AND ".join(where),
                vis_params,
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_memory(tx, memory_id, visibility=visibility)

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = ["deleted_at IS NULL", "content_hash IS NOT NULL"]
            params: dict[str, Any] = {}
            if namespace is not None:
                where.append("namespace = :ns")
                params["ns"] = namespace
            sql = (
                "SELECT content_hash, COUNT(*) AS cnt, "
                "MIN(id) KEEP (DENSE_RANK FIRST ORDER BY created ASC) AS canonical_id "
                "FROM memories WHERE " + " AND ".join(where) + " "
                "GROUP BY content_hash HAVING COUNT(*) > 1 "
                "ORDER BY cnt DESC, content_hash ASC"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def consolidate_duplicate_memories(
        self,
        tx: Transaction,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(duplicate_ids, "dup")
            params["canonical_id"] = canonical_id
            await _call(
                cursor.execute,
                f"""
                UPDATE memories
                   SET deleted_at = SYSTIMESTAMP
                 WHERE id IN ({placeholders})
                   AND id != :canonical_id
                   AND deleted_at IS NULL
                """,
                params,
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def semantic_search(
        self,
        tx: Transaction,
        *,
        embedding: Sequence[float],
        limit: int,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_agent: str | None = None,
        include_archived: bool = False,
        boost_recency: bool = False,
        recency_weight: float = 0.15,
    ) -> list[Row]:
        if not embedding:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Validate + format via the shared helper (Oracle eng O5).
            vec_literal = _validate_and_format_vector(embedding)
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL", "m.embedding IS NOT NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if clause:
                where.append(clause)
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    where.append(f"m.{col} = :flt_{col}")
                    params[f"flt_{col}"] = val
            params["q"] = vec_literal
            params["limit"] = limit
            # Oracle 23ai VECTOR_DISTANCE returns 0 for identical vectors
            # and grows with dissimilarity, so ORDER BY ASC matches the
            # Postgres pgvector ``<=>`` ordering. Recency boost subtracts
            # a bounded age penalty so the wider candidate set still
            # surfaces freshly-touched rows when the caller opts in.
            if boost_recency:
                rank = (
                    "VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE) "
                    "- :w * (1.0 / (1.0 + (SYSDATE - CAST(m.updated AS DATE))))"
                )
                params["w"] = float(recency_weight)
            else:
                rank = "VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE)"
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, "
                "m.group_id, m.created, m.updated, m.archived_at, "
                "m.recall_count, m.last_recalled_at, "
                f"({rank}) AS rank_score "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                f"ORDER BY {rank} ASC "
                "FETCH FIRST :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_memory_context(
        self,
        tx: Transaction,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        # Resolve an embedding via the lifecycle-owned embedder. Import
        # is local to avoid a hard top-level cycle on lifecycle.
        from mnemos.core.lifecycle import _get_embedding

        embedding = await _get_embedding(query)
        if not embedding:
            return []

        if hasattr(user, "user_id"):
            from mnemos.core.security import is_root

            if is_root(user):
                visibility = VisibilityFilter(
                    scope=VisibilityScope.ROOT_BYPASS,
                    user_id=None,
                    group_ids=(),
                    namespace=None,
                )
            else:
                ns = getattr(user, "namespace", None) or "default"
                visibility = VisibilityFilter.for_read(user, namespace=ns)
        else:
            visibility = VisibilityFilter(
                scope=VisibilityScope.ROOT_BYPASS,
                user_id=None,
                group_ids=(),
                namespace=None,
            )

        rows = await self.semantic_search(tx, embedding=embedding, limit=limit, visibility=visibility)
        return [dict(row) for row in rows]


class OracleCompressionRepository(CompressionRepository):
    """Oracle compression repo — schema present, basic CRUD wired."""

    async def compression_candidate_exists(
        self,
        tx: Transaction,
        *,
        candidate_id: str,
        memory_id: str,
        owner_id: str,
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT 1 FROM memory_compression_candidates
                 WHERE id = :candidate_id
                   AND memory_id = :memory_id
                   AND owner_id = :owner_id
                """,
                {
                    "candidate_id": candidate_id,
                    "memory_id": memory_id,
                    "owner_id": owner_id,
                },
            )
            row = await _call(cursor.fetchone)
            return row is not None
        finally:
            await _call(cursor.close)

    async def insert_compressed_variant(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        owner_id: str,
        winner_candidate_id: str | None,
        engine_id: str,
        engine_version: str | None,
        compressed_content: str | None,
        compressed_tokens: int | None,
        compression_ratio: float | None,
        quality_score: float | None,
        composite_score: float | None,
        scoring_profile: str | None,
        judge_model: str | None,
        selected_at: Any,
    ) -> str:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        affected = 0
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_compressed_variants (
                    memory_id, owner_id, winner_candidate_id, engine_id, engine_version,
                    compressed_content, compressed_tokens, compression_ratio,
                    quality_score, composite_score, scoring_profile, judge_model,
                    selected_at
                )
                SELECT
                    :memory_id, :owner_id, :winner_candidate_id, :engine_id, :engine_version,
                    :compressed_content, :compressed_tokens, :compression_ratio,
                    :quality_score, :composite_score,
                    COALESCE(:scoring_profile, 'balanced'), :judge_model,
                    COALESCE(:selected_at, SYSTIMESTAMP)
                FROM dual
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_compressed_variants WHERE memory_id = :memory_id
                )
                """,
                {
                    "memory_id": memory_id,
                    "owner_id": owner_id,
                    "winner_candidate_id": winner_candidate_id,
                    "engine_id": engine_id,
                    "engine_version": engine_version,
                    "compressed_content": compressed_content,
                    "compressed_tokens": compressed_tokens,
                    "compression_ratio": compression_ratio,
                    "quality_score": quality_score,
                    "composite_score": composite_score,
                    "scoring_profile": scoring_profile,
                    "judge_model": judge_model,
                    "selected_at": selected_at,
                },
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_compressed_variant_by_memory_id(self, tx: Transaction, memory_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT memory_id, owner_id, winner_candidate_id, engine_id, engine_version,
                       compressed_content, compressed_tokens, compression_ratio,
                       quality_score, composite_score, scoring_profile, judge_model,
                       selected_at
                  FROM memory_compressed_variants
                 WHERE memory_id = :memory_id
                """,
                {"memory_id": memory_id},
            )
            row = await _call(cursor.fetchone)
            return await _row_to_dict(cursor, row)
        finally:
            await _call(cursor.close)

    async def gather_stats(self, tx: Transaction):
        from mnemos.persistence.base import CompressionStatsRow

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT COUNT(*), AVG(compression_ratio),
                       SUM(CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END)
                  FROM memory_compressed_variants
                """,
            )
            row = await _call(cursor.fetchone) or (0, None, 0)
            total, avg_ratio, unreviewed = row
            return CompressionStatsRow(
                total_compressions=int(total or 0),
                average_compression_ratio=float(avg_ratio) if avg_ratio is not None else None,
                unreviewed_compressions=int(unreviewed or 0),
            )
        finally:
            await _call(cursor.close)

    async def fetch_compressed_variants_for_export(
        self,
        tx: Transaction,
        *,
        memory_ids: Sequence[str],
        effective_owner: str | None,
        hard_limit: int,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders, params = _in_placeholders(memory_ids, "mid")
            where = [f"memory_id IN ({placeholders})"]
            if effective_owner:
                where.append("owner_id = :owner_id")
                params["owner_id"] = effective_owner
            sql = (
                "SELECT memory_id, owner_id, winner_candidate_id, engine_id, "
                "engine_version, compressed_content, compressed_tokens, "
                "compression_ratio, quality_score, composite_score, "
                "scoring_profile, judge_model, selected_at "
                "FROM memory_compressed_variants WHERE " + " AND ".join(where) + " "
                f"FETCH FIRST {int(hard_limit) + 1} ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class OracleWebhookRepository(WebhookRepository):
    """Oracle webhook repo — outbox dispatch inserts delivery rows."""

    async def dispatch_event(
        self,
        tx: Transaction,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        import json
        import uuid as _uuid

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            sub_where = ["revoked = 0"]
            sub_params: dict[str, Any] = {}
            if owner_id is not None:
                sub_where.append("owner_id = :owner_id")
                sub_params["owner_id"] = owner_id
            if namespace is not None:
                sub_where.append("namespace = :ns")
                sub_params["ns"] = namespace
            # Subscription opts in to an event by listing it in the JSON
            # array stored in ``events``. Use DBMS_LOB.INSTR to match the
            # quoted event-name token; tolerates either compact (``["x"]``)
            # or pretty-printed JSON without requiring the JSON parser.
            sub_where.append("DBMS_LOB.INSTR(events, :ev_token) > 0")
            sub_params["ev_token"] = f'"{event_type}"'
            sql_sub = "SELECT id, owner_id, namespace FROM webhook_subscriptions WHERE " + " AND ".join(sub_where)
            await _call(cursor.execute, sql_sub, sub_params)
            subs = await _fetch_all_dicts(cursor)
            if not subs:
                return []

            payload_json = json.dumps(payload, default=str, separators=(",", ":"))
            delivery_ids: list[str] = []
            for sub in subs:
                d_id = _uuid.uuid4().hex
                await _call(
                    cursor.execute,
                    """
                    INSERT INTO webhook_deliveries (
                        id, subscription_id, event_type, payload, owner_id,
                        namespace, state, attempt_count, next_attempt_at
                    ) VALUES (
                        :id, :sub_id, :event_type, :payload, :owner_id,
                        :namespace, 'pending', 0, SYSTIMESTAMP
                    )
                    """,
                    {
                        "id": d_id,
                        "sub_id": sub["id"],
                        "event_type": event_type,
                        "payload": payload_json,
                        "owner_id": sub.get("owner_id") or "default",
                        "namespace": sub.get("namespace") or "default",
                    },
                )
                delivery_ids.append(d_id)
            return delivery_ids
        finally:
            await _call(cursor.close)


class OracleConsultationAuditRepository(ConsultationAuditRepository):
    """Oracle consultations-audit repo — safe-default returns.

    The Postgres implementation delegates to the ``mcp_repo`` /
    ``openai_compat_repo`` modules which read from the model registry
    (model_registry / model_recommendations / provider_routing tables).
    Those tables have not yet been ported to Oracle, so every call here
    returns the empty / None value that signals "no Oracle-side audit
    data" to the GRAEAE engine. The engine falls back to its built-in
    provider defaults (see lifecycle docstring), which is the same
    posture as a fresh Postgres install before model_registry seeding.
    """

    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        _ = (tx, task_type, cost_budget, quality_floor)
        return None, []

    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        _ = (tx, task_type, cost_budget, quality_floor)
        return None

    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        _ = (tx, model)
        return None

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        _ = tx
        return []

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        _ = (tx, model_id)
        return None


class OracleFederationRepository(FederationRepository):
    """Oracle federation repo — peer CRUD wired, sync paths stubbed."""

    async def list_peers(self, tx: Transaction) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM federation_peers ORDER BY created",
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def get_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM federation_peers WHERE id = :id",
                {"id": peer_id},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def delete_peer(self, tx: Transaction, peer_id: str) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "DELETE FROM federation_peers WHERE id = :id",
                {"id": peer_id},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_due_peers(self, tx: Transaction, *, limit: int = 10) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT * FROM federation_peers
                 WHERE enabled = 1
                   AND (last_sync_at IS NULL
                        OR last_sync_at < SYSTIMESTAMP - NUMTODSINTERVAL(sync_interval_secs, 'SECOND'))
                 ORDER BY COALESCE(last_sync_at, TIMESTAMP '1970-01-01 00:00:00')
                 FETCH FIRST :limit ROWS ONLY
                """,
                {"limit": limit},
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    _ALLOWED_PEER_COLS = frozenset(
        {
            "name",
            "base_url",
            "auth_token",
            "namespace_filter",
            "category_filter",
            "enabled",
            "sync_interval_secs",
            "compat_mode",
        }
    )

    async def fetch_memory_page(
        self,
        tx: Transaction,
        *,
        updated_after: Any | None = None,
        id_after: str | None = None,
        limit: int = 100,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            if updated_after is not None and id_after is not None:
                sql = (
                    "SELECT id, content, category, subcategory, metadata, "
                    "owner_id, namespace, updated FROM memories "
                    "WHERE deleted_at IS NULL "
                    "AND (updated > :upd OR (updated = :upd AND id > :id_after)) "
                    "ORDER BY updated ASC, id ASC "
                    "FETCH FIRST :limit ROWS ONLY"
                )
                params = {"upd": updated_after, "id_after": id_after, "limit": limit}
            else:
                sql = (
                    "SELECT id, content, category, subcategory, metadata, "
                    "owner_id, namespace, updated FROM memories "
                    "WHERE deleted_at IS NULL "
                    "ORDER BY updated ASC, id ASC "
                    "FETCH FIRST :limit ROWS ONLY"
                )
                params = {"limit": limit}
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def create_peer(
        self,
        tx: Transaction,
        *,
        name: str,
        base_url: str,
        auth_token: str,
        namespace_filter: Sequence[str] | None,
        category_filter: Sequence[str] | None,
        enabled: bool,
        sync_interval_secs: int,
        compat_mode: str,
    ) -> Row:
        import json
        import uuid as _uuid

        peer_id = str(_uuid.uuid4())
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO federation_peers (
                    id, name, base_url, auth_token, namespace_filter,
                    category_filter, enabled, sync_interval_secs, compat_mode
                ) VALUES (
                    :id, :name, :base_url, :auth_token, :ns_filter,
                    :cat_filter, :enabled, :sync_interval, :compat_mode
                )
                """,
                {
                    "id": peer_id,
                    "name": name,
                    "base_url": base_url,
                    "auth_token": auth_token,
                    "ns_filter": json.dumps(list(namespace_filter)) if namespace_filter is not None else None,
                    "cat_filter": json.dumps(list(category_filter)) if category_filter is not None else None,
                    "enabled": 1 if enabled else 0,
                    "sync_interval": sync_interval_secs,
                    "compat_mode": compat_mode or "strict",
                },
            )
        finally:
            await _call(cursor.close)
        return await self.get_peer(tx, peer_id)

    async def update_peer(self, tx: Transaction, peer_id: str, updates: dict[str, Any]) -> Row | None:
        bad = set(updates) - self._ALLOWED_PEER_COLS
        if bad:
            raise ValueError(f"unknown federation peer fields: {sorted(bad)}")
        if not updates:
            return await self.get_peer(tx, peer_id)
        import json

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            sets: list[str] = []
            params: dict[str, Any] = {"id": peer_id}
            for col, value in updates.items():
                if col == "enabled":
                    sets.append("enabled = :enabled")
                    params["enabled"] = 1 if value else 0
                elif col in ("namespace_filter", "category_filter"):
                    sets.append(f"{col} = :{col}")
                    params[col] = json.dumps(list(value)) if value is not None else None
                else:
                    sets.append(f"{col} = :{col}")
                    params[col] = value
            if not sets:
                return await self.get_peer(tx, peer_id)
            sets.append("updated = SYSTIMESTAMP")
            sql = f"UPDATE federation_peers SET {', '.join(sets)} WHERE id = :id"
            await _call(cursor.execute, sql, params)
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_peer(tx, peer_id)

    async def upsert_peer(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        base_url: str,
        name: str | None = None,
        enabled: bool = True,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO federation_peers p
                USING (SELECT :id AS id FROM dual) s
                   ON (p.id = s.id)
                WHEN MATCHED THEN UPDATE SET
                    base_url = :base_url,
                    name = NVL(:name, p.name),
                    enabled = :enabled,
                    updated = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (
                    id, name, base_url, auth_token, enabled
                ) VALUES (
                    :id, NVL(:name, :id), :base_url, '', :enabled
                )
                """,
                {
                    "id": peer_id,
                    "name": name,
                    "base_url": base_url,
                    "enabled": 1 if enabled else 0,
                },
            )
        finally:
            await _call(cursor.close)

    async def get_sync_peer(self, tx: Transaction, peer_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM federation_peers WHERE id = :id",
                {"id": peer_id},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def fetch_sync_log(self, tx: Transaction, peer_id: str, limit: int) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, started_at, finished_at, memories_pulled,
                       memories_new, memories_updated, error,
                       cursor_before, cursor_after
                  FROM federation_sync_log
                 WHERE peer_id = :peer_id
                 ORDER BY started_at DESC
                 FETCH FIRST :limit ROWS ONLY
                """,
                {"peer_id": peer_id, "limit": limit},
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def create_sync_log(self, tx: Transaction, peer_id: str, cursor_before: Any) -> Any:
        import uuid as _uuid

        log_id = str(_uuid.uuid4())
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO federation_sync_log (id, peer_id, cursor_before)
                VALUES (:id, :peer_id, :cursor_before)
                """,
                {"id": log_id, "peer_id": peer_id, "cursor_before": cursor_before},
            )
        finally:
            await _call(cursor.close)
        return log_id

    async def finish_sync_log(
        self,
        tx: Transaction,
        *,
        log_id: Any,
        memories_pulled: int,
        memories_new: int,
        memories_updated: int,
        error: str | None,
        cursor_after: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE federation_sync_log SET
                    finished_at = SYSTIMESTAMP,
                    memories_pulled = :pulled,
                    memories_new = :new_count,
                    memories_updated = :upd_count,
                    error = :err,
                    cursor_after = :cur_after
                 WHERE id = :id
                """,
                {
                    "id": log_id,
                    "pulled": memories_pulled,
                    "new_count": memories_new,
                    "upd_count": memories_updated,
                    "err": error,
                    "cur_after": cursor_after,
                },
            )
        finally:
            await _call(cursor.close)

    async def record_sync_error(self, tx: Transaction, peer_id: str, error: str) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE federation_peers SET
                    last_sync_at = SYSTIMESTAMP,
                    last_error = :err,
                    last_error_at = SYSTIMESTAMP
                 WHERE id = :id
                """,
                {"id": peer_id, "err": error},
            )
        finally:
            await _call(cursor.close)

    async def record_sync_success(
        self,
        tx: Transaction,
        peer_id: str,
        cursor: Any,
        total_pulled: int,
    ) -> None:
        conn = _conn_from_tx(tx)
        cur = await _call(conn.cursor)
        try:
            await _call(
                cur.execute,
                """
                UPDATE federation_peers SET
                    last_sync_at = SYSTIMESTAMP,
                    last_sync_cursor = :cursor,
                    last_error = NULL,
                    last_error_at = NULL,
                    total_pulled = total_pulled + :delta
                 WHERE id = :id
                """,
                {"id": peer_id, "cursor": cursor, "delta": total_pulled},
            )
        finally:
            await _call(cur.close)

    async def record_schema_abort(
        self,
        tx: Transaction,
        *,
        peer_id: str,
        peer_version: str | None,
        cursor_before: Any,
        error: str,
        is_transient: bool,
    ) -> None:
        _ = (peer_version, is_transient)
        log_id = await self.create_sync_log(tx, peer_id, cursor_before)
        await self.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=0,
            memories_new=0,
            memories_updated=0,
            error=error,
            cursor_after=cursor_before,
        )
        await self.record_sync_error(tx, peer_id, error)

    async def update_peer_schema_check(
        self,
        tx: Transaction,
        peer_id: str,
        peer_version: str | None,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE federation_peers SET
                    peer_mnemos_version = :pv,
                    last_schema_check_at = SYSTIMESTAMP
                 WHERE id = :id
                """,
                {"id": peer_id, "pv": peer_version},
            )
        finally:
            await _call(cursor.close)

    async def fetch_federated_memory_marker(self, tx: Transaction, local_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT federation_remote_updated
                  FROM memories
                 WHERE id = :id AND deleted_at IS NULL
                """,
                {"id": local_id},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def insert_federated_memory(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        source_model: str | None,
        source_provider: str | None,
        source_session: str | None,
        source_agent: str | None,
        peer_name: str,
        remote_updated: Any,
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            try:
                await _call(
                    cursor.execute,
                    """
                    INSERT INTO memories (
                        id, content, category, subcategory, metadata,
                        verbatim_content, quality_rating, owner_id, namespace,
                        permission_mode, source_model, source_provider,
                        source_session, source_agent, federation_source,
                        federation_remote_updated, created, updated
                    ) VALUES (
                        :id, :content, :category, :subcategory, :metadata,
                        :verbatim, :quality, 'federation', :namespace,
                        644, :s_model, :s_provider, :s_session, :s_agent,
                        :peer_name,
                        CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE),
                        SYSTIMESTAMP,
                        CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE)
                    )
                    """,
                    {
                        "id": local_id,
                        "content": content,
                        "category": category,
                        "subcategory": subcategory,
                        "metadata": metadata_json,
                        "verbatim": verbatim_content,
                        "quality": quality_rating,
                        "namespace": namespace,
                        "s_model": source_model,
                        "s_provider": source_provider,
                        "s_session": source_session,
                        "s_agent": source_agent,
                        "peer_name": peer_name,
                        "remote_updated": remote_updated,
                    },
                )
                return True
            except Exception as e:
                # ORA-00001 unique constraint violated -> already imported
                if "ORA-00001" in str(e):
                    return False
                raise
        finally:
            await _call(cursor.close)

    async def update_federated_memory_if_newer(
        self,
        tx: Transaction,
        *,
        local_id: str,
        content: str,
        category: str,
        subcategory: str | None,
        metadata_json: str,
        verbatim_content: str,
        quality_rating: int,
        namespace: str,
        remote_updated: Any,
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE memories SET
                    content = :content,
                    category = :category,
                    subcategory = :subcategory,
                    metadata = :metadata,
                    verbatim_content = :verbatim,
                    quality_rating = :quality,
                    namespace = :namespace,
                    federation_remote_updated = CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE),
                    updated = CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE)
                 WHERE id = :id
                   AND deleted_at IS NULL
                   AND (
                        federation_remote_updated IS NULL
                        OR federation_remote_updated <
                            CAST(:remote_updated AS TIMESTAMP WITH TIME ZONE)
                   )
                """,
                {
                    "id": local_id,
                    "content": content,
                    "category": category,
                    "subcategory": subcategory,
                    "metadata": metadata_json,
                    "verbatim": verbatim_content,
                    "quality": quality_rating,
                    "namespace": namespace,
                    "remote_updated": remote_updated,
                },
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def apply_consolidation_tombstone(
        self,
        tx: Transaction,
        *,
        local_id: str,
        local_canonical_id: str,
        consolidated_at: Any,
        remote_id: str,
        canonical_remote_id: str,
        peer_name: str,
    ) -> bool:
        # Oracle schema lacks the memories.consolidated_into column the
        # Postgres path mutates. Instead we record the tombstone in the
        # dedicated federation_consolidation_tombstones table and
        # soft-delete the source row so re-pulls do not resurrect it.
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO federation_consolidation_tombstones t
                USING (SELECT :peer_name AS peer_name, :remote_id AS remote_id FROM dual) s
                   ON (t.peer_name = s.peer_name AND t.remote_id = s.remote_id)
                WHEN MATCHED THEN UPDATE SET
                    local_id = :local_id,
                    local_canonical_id = :local_canonical_id,
                    canonical_remote_id = :canonical_remote_id,
                    consolidated_at = NVL(
                        CAST(:consolidated_at AS TIMESTAMP WITH TIME ZONE),
                        SYSTIMESTAMP
                    )
                WHEN NOT MATCHED THEN INSERT (
                    peer_name, remote_id, local_id, local_canonical_id,
                    canonical_remote_id, consolidated_at
                ) VALUES (
                    :peer_name, :remote_id, :local_id, :local_canonical_id,
                    :canonical_remote_id,
                    NVL(CAST(:consolidated_at AS TIMESTAMP WITH TIME ZONE), SYSTIMESTAMP)
                )
                """,
                {
                    "peer_name": peer_name,
                    "remote_id": remote_id,
                    "local_id": local_id,
                    "local_canonical_id": local_canonical_id,
                    "canonical_remote_id": canonical_remote_id,
                    "consolidated_at": consolidated_at,
                },
            )
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET deleted_at = SYSTIMESTAMP
                 WHERE id = :id
                   AND deleted_at IS NULL
                   AND EXISTS (
                       SELECT 1 FROM memories c
                        WHERE c.id = :canonical_id AND c.deleted_at IS NULL
                   )
                """,
                {"id": local_id, "canonical_id": local_canonical_id},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def delete_federated_memory(self, tx: Transaction, peer_name: str, memory_id: str) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET deleted_at = SYSTIMESTAMP
                 WHERE id = :id
                   AND federation_source = :peer
                   AND deleted_at IS NULL
                """,
                {"id": memory_id, "peer": peer_name},
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def feed_query(
        self,
        tx: Transaction,
        *,
        since_updated: Any | None,
        since_id: str | None,
        namespaces: Sequence[str],
        categories: Sequence[str],
        limit: int,
        prefer_compressed: bool,
    ) -> list[Row]:
        _ = prefer_compressed
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = [
                "m.deleted_at IS NULL",
                "m.federation_source IS NULL",
                "m.archived_at IS NULL",
            ]
            params: dict[str, Any] = {"limit": limit}
            if since_updated is not None and since_id is not None:
                where.append("(m.updated > :upd OR (m.updated = :upd AND m.id > :since_id))")
                params["upd"] = since_updated
                params["since_id"] = since_id
            if namespaces:
                ns_ph, ns_params = _in_placeholders(namespaces, "ns")
                where.append(f"m.namespace IN ({ns_ph})")
                params.update(ns_params)
            if categories:
                cat_ph, cat_params = _in_placeholders(categories, "cat")
                where.append(f"m.category IN ({cat_ph})")
                params.update(cat_params)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.verbatim_content, m.owner_id, m.namespace, "
                "m.permission_mode, m.source_model, m.source_provider, "
                "m.source_session, m.source_agent, m.created, m.updated, "
                "m.archived_at "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                "ORDER BY m.updated ASC, m.id ASC "
                "FETCH FIRST :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def get_feed_memory(
        self,
        tx: Transaction,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = [
                "m.id = :id",
                "m.deleted_at IS NULL",
                "m.federation_source IS NULL",
            ]
            params: dict[str, Any] = {"id": memory_id}
            if namespaces:
                ns_ph, ns_params = _in_placeholders(namespaces, "ns")
                where.append(f"m.namespace IN ({ns_ph})")
                params.update(ns_params)
            if categories:
                cat_ph, cat_params = _in_placeholders(categories, "cat")
                where.append(f"m.category IN ({cat_ph})")
                params.update(cat_params)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.verbatim_content, m.owner_id, m.namespace, "
                "m.permission_mode, m.source_model, m.source_provider, "
                "m.source_session, m.source_agent, m.created, m.updated, "
                "m.archived_at "
                "FROM memories m WHERE " + " AND ".join(where)
            )
            await _call(cursor.execute, sql, params)
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)


class OracleJournalRepository(JournalRepository):
    """Oracle journal repo — Oracle dialect journal CRUD over the ``journal`` table."""

    async def create_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
        entry_date: Any,
        topic: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        import json as _json

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            if entry_date is not None:
                await _call(
                    cursor.execute,
                    """INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                       VALUES (:id, :owner_id, :namespace, :entry_date, :topic, :content, :metadata)""",
                    {
                        "id": entry_id,
                        "owner_id": owner_id,
                        "namespace": namespace,
                        "entry_date": entry_date,
                        "topic": topic,
                        "content": content,
                        "metadata": _json.dumps(metadata or {}),
                    },
                )
            else:
                await _call(
                    cursor.execute,
                    """INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                       VALUES (:id, :owner_id, :namespace, SYSDATE, :topic, :content, :metadata)""",
                    {
                        "id": entry_id,
                        "owner_id": owner_id,
                        "namespace": namespace,
                        "topic": topic,
                        "content": content,
                        "metadata": _json.dumps(metadata or {}),
                    },
                )
            # Oracle RETURNING is awkward with oracledb thin driver; SELECT the row back.
            await _call(
                cursor.execute,
                """SELECT id, TO_CHAR(entry_date, 'YYYY-MM-DD') AS entry_date, topic, content, metadata,
                          TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') AS created
                   FROM journal WHERE id = :id""",
                {"id": entry_id},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone)) or {}
        finally:
            await _call(cursor.close)

    async def list_journal_entries(
        self,
        tx: Transaction,
        *,
        owner_id: str,
        namespace: str,
        topic: str | None = None,
        entry_date: Any = None,
        search: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = ["owner_id = :owner_id", "namespace = :namespace", "deleted_at IS NULL"]
            params: dict[str, Any] = {"owner_id": owner_id, "namespace": namespace, "limit": limit}
            if entry_date is not None:
                where.append("entry_date = :entry_date")
                params["entry_date"] = entry_date
            elif topic is not None:
                where.append("topic = :topic")
                params["topic"] = topic
            elif search is not None:
                pattern = f"%{search}%"
                where.append("(UPPER(content) LIKE UPPER(:search) OR UPPER(topic) LIKE UPPER(:search))")
                params["search"] = pattern
            sql = (
                "SELECT id, TO_CHAR(entry_date, 'YYYY-MM-DD') AS entry_date, topic, content, "
                "metadata, TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') AS created "
                "FROM journal WHERE " + " AND ".join(where) + " "
                "ORDER BY created DESC FETCH FIRST :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_journal_entry(
        self,
        tx: Transaction,
        *,
        entry_id: str,
        owner_id: str,
        namespace: str,
    ) -> bool:
        # Soft delete: the route currently uses HARD delete. The trait contract
        # specifies soft-delete for consistency with other repos; route migration
        # follow-up will adopt this behaviour.
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """UPDATE journal SET deleted_at = SYSDATE
                   WHERE id = :id AND owner_id = :owner_id AND namespace = :namespace
                     AND deleted_at IS NULL""",
                {"id": entry_id, "owner_id": owner_id, "namespace": namespace},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)


class OracleStateRepository(StateRepository):
    """Oracle key/value state repo — full CRUD over the ``state`` table."""

    async def get(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT key, value, TO_CHAR(updated) AS updated, version, owner_id, namespace
                  FROM state
                 WHERE owner_id = :owner_id
                   AND namespace = :namespace
                   AND key = :key
                   AND deleted_at IS NULL
                """,
                {"owner_id": owner_id, "namespace": namespace, "key": key},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def set(
        self,
        tx: Transaction,
        key: str,
        value: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        expires_at: Any | None = None,
    ) -> Row | None:
        _ = expires_at  # TTL not yet modelled in the Oracle schema
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO state s
                USING (SELECT :owner_id AS owner_id, :namespace AS namespace,
                              :key AS key FROM dual) src
                   ON (s.owner_id = src.owner_id
                       AND s.namespace = src.namespace
                       AND s.key = src.key)
                WHEN MATCHED THEN UPDATE SET
                    value = :value,
                    updated = SYSTIMESTAMP,
                    version = s.version + 1,
                    deleted_at = NULL
                WHEN NOT MATCHED THEN INSERT (
                    owner_id, namespace, key, value, updated, version
                ) VALUES (
                    :owner_id, :namespace, :key, :value, SYSTIMESTAMP, 1
                )
                """,
                {
                    "owner_id": owner_id,
                    "namespace": namespace,
                    "key": key,
                    "value": value,
                },
            )
        finally:
            await _call(cursor.close)
        return await self.get(tx, key, owner_id=owner_id, namespace=namespace)

    async def delete(
        self,
        tx: Transaction,
        key: str,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE state
                   SET deleted_at = SYSTIMESTAMP
                 WHERE owner_id = :owner_id
                   AND namespace = :namespace
                   AND key = :key
                   AND deleted_at IS NULL
                """,
                {"owner_id": owner_id, "namespace": namespace, "key": key},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            if limit is None:
                await _call(
                    cursor.execute,
                    """
                    SELECT key, value, TO_CHAR(updated) AS updated, version, owner_id, namespace
                      FROM state
                     WHERE owner_id = :owner_id
                       AND namespace = :namespace
                       AND deleted_at IS NULL
                     ORDER BY key
                     OFFSET :offset ROWS
                    """,
                    {"owner_id": owner_id, "namespace": namespace, "offset": offset},
                )
            else:
                await _call(
                    cursor.execute,
                    """
                    SELECT key, value, TO_CHAR(updated) AS updated, version, owner_id, namespace
                      FROM state
                     WHERE owner_id = :owner_id
                       AND namespace = :namespace
                       AND deleted_at IS NULL
                     ORDER BY key
                     OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
                    """,
                    {
                        "owner_id": owner_id,
                        "namespace": namespace,
                        "offset": offset,
                        "limit": limit,
                    },
                )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_namespace(
        self,
        tx: Transaction,
        *,
        owner_id: str = "default",
        namespace: str = "default",
    ) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE state
                   SET deleted_at = SYSTIMESTAMP
                 WHERE owner_id = :owner_id
                   AND namespace = :namespace
                   AND deleted_at IS NULL
                """,
                {"owner_id": owner_id, "namespace": namespace},
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)


class OracleBackend(PersistenceBackend):
    """Oracle persistence facade backed by a python-oracledb async pool."""

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False

    def __init__(self, pool: Any, settings: Any):
        self._pool = pool
        self._settings = settings
        self._closed = False
        self._memories_repo = OracleMemoryRepository()
        self._kg_triples_repo = OracleKGRepository()
        self._memory_versions_repo = OracleVersionRepository()
        self._memory_branches_repo = OracleBranchRepository()
        self._compression_repo = OracleCompressionRepository()
        self._webhooks_repo = OracleWebhookRepository()
        self._consultations_audit_repo = OracleConsultationAuditRepository()
        self._federation_repo = OracleFederationRepository()
        self._state_kv_repo = OracleStateRepository()
        self._journal_repo = OracleJournalRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def pool(self) -> Any:
        return self._pool

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[Transaction]:
        async with self._pool.acquire() as conn:
            tx = _OracleTransaction(conn)
            try:
                yield tx
            except BaseException:
                if not tx.closed:
                    await tx.rollback()
                raise
            else:
                if not tx.closed:
                    await tx.commit()

    @property
    def memories(self) -> MemoryRepository:
        return self._memories_repo

    @property
    def kg_triples(self) -> KGRepository:
        return self._kg_triples_repo

    @property
    def memory_versions(self) -> VersionRepository:
        return self._memory_versions_repo

    @property
    def memory_branches(self) -> BranchRepository:
        return self._memory_branches_repo

    @property
    def compression(self) -> CompressionRepository:
        return self._compression_repo

    @property
    def webhooks(self) -> WebhookRepository:
        return self._webhooks_repo

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit_repo

    @property
    def federation(self) -> FederationRepository:
        return self._federation_repo

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv_repo

    @property
    def journal(self) -> JournalRepository:
        return self._journal_repo

    async def open(self) -> None:
        """Lifecycle hook — validates pool checkout + session callback.

        Per Oracle eng review O9 / R3: ``lifecycle._build_oracle_backend``
        and ``lifecycle._build_db2_backend`` both call ``backend.open()``
        immediately after construction so the production-posture knobs
        added in ``create_oracle_pool`` (statement_cache_size,
        session_callback, NLS pinning, optional PDB switch) are
        validated at startup rather than first-use. Idempotent. The
        Db2 subclass extends this with its own
        ``DB2_VECTOR_INDEXING`` registry probe.

        Implementation note: oracledb's async pool defers physical
        connect until ``acquire()``, so this hook performs a cheap
        round-trip (``SELECT 1 FROM DUAL``) to surface auth / NLS /
        PDB failures before the first request hits the pool.
        """
        if self._closed:
            return
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                cursor = await _call(conn.cursor)
                try:
                    await _call(cursor.execute, "SELECT 1 FROM DUAL")
                    await _call(cursor.fetchone)
                finally:
                    await _call(cursor.close)
        except Exception as exc:
            _LOG.warning(
                "OracleBackend.open probe failed (%s); backend remains open " "but first acquire() may also fail.",
                exc,
            )

    async def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._pool, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._closed = True


__all__ = [
    "OracleBackend",
    "OracleBranchRepository",
    "OracleCompressionRepository",
    "OracleConsultationAuditRepository",
    "OracleFederationRepository",
    "OracleJournalRepository",
    "OracleKGRepository",
    "OracleMemoryRepository",
    "OracleStateRepository",
    "OracleVersionRepository",
    "OracleWebhookRepository",
    "create_oracle_pool",
    "_validate_and_format_vector",
]
