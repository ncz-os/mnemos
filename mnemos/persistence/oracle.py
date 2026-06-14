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
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.core.config import oracle_pdb_env, runtime_env_value_stripped, vector_dim_max_env
from mnemos.core.visibility import ACL_READ_BIT, acl_principals
from mnemos.persistence.base import (
    AclRepository,
    AuditChainRepository,
    BranchRepository,
    CompressionQueueRepository,
    CompressionRepository,
    ConsultationAuditRepository,
    ConsultationsRepository,
    FederationRepository,
    FULL_STORAGE_CAPABILITY_DETAILS,
    KGRepository,
    MemoryRepository,
    OAuthRepository,
    SessionsRepository,
    StateRepository,
    Transaction,
    VersionRepository,
    WebhookRepository,
)
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.core.secret_detection import VAULT_NAMESPACE

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
    return vector_dim_max_env()


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


def _rank_score_sort_key(row: Row) -> float:
    rank = row.get("rank_score") if isinstance(row, dict) else None
    try:
        score = float(rank)
    except (TypeError, ValueError):
        return math.inf
    return score if math.isfinite(score) else math.inf


def _recency_date(row: Row) -> date:
    def _coerce_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                return None
        return None

    if not isinstance(row, dict):
        return date.min
    return (
        _coerce_date(row.get("last_recalled_at"))
        or _coerce_date(row.get("updated"))
        or _coerce_date(row.get("created"))
        or date.min
    )


def _boosted_rank_score_sort_key(row: Row, *, today: date, recency_weight: float) -> float:
    rank = _rank_score_sort_key(row)
    if not math.isfinite(rank):
        return math.inf
    age_days = max(0, (today - _recency_date(row)).days)
    return rank - recency_weight * (1.0 / (1.0 + age_days))


def _boosted_rank_supersession_sort_key(row: Row, *, today: date, recency_weight: float) -> tuple[bool, float]:
    superseded = isinstance(row, dict) and bool(row.get("superseded_by") or row.get("consolidated_into"))
    return superseded, _boosted_rank_score_sort_key(row, today=today, recency_weight=recency_weight)


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

    On top of the tenancy predicate this also subtracts any namespaces
    in ``visibility.exclude_namespaces`` (the secret vault on the
    default path). The subtraction is rendered as a separate
    ``namespace NOT IN (...)`` term ANDed with the scope predicate, so
    it applies even to ROOT_BYPASS (which otherwise yields no tenancy
    filter and would expose vault rows to the root token).
    """
    clause, params = _render_visibility_core(visibility, table_alias=table_alias, param_prefix=param_prefix)
    excl = tuple(visibility.exclude_namespaces or ())
    if excl:
        p = f"{table_alias}." if table_alias else ""
        names = []
        for i, ns in enumerate(excl):
            key = f"{param_prefix}_xns_{i}"
            names.append(f":{key}")
            params[key] = ns
        # SQL NULL semantics: `namespace NOT IN (...)` evaluates to
        # UNKNOWN (not TRUE) for rows where namespace IS NULL, which
        # would silently DROP legitimate non-vault NULL-namespace
        # memories from default/root search. Vault rows always carry a
        # non-NULL namespace ("vault"), so NULL is never a secret —
        # preserve it explicitly. (release-blocking 2026-06-13)
        excl_clause = f"({p}namespace IS NULL OR {p}namespace NOT IN ({', '.join(names)}))"
        clause = f"({clause}) AND {excl_clause}" if clause else excl_clause
    return clause, params


def _render_visibility_core(
    visibility: VisibilityFilter,
    *,
    table_alias: str = "",
    param_prefix: str = "vis",
) -> tuple[str, dict[str, Any]]:
    """Tenancy-only render (no vault subtraction); see _render_visibility."""
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

    acl_clause = ""
    principals = acl_principals(visibility.user_id or "", group_ids)
    if principals:
        acl_placeholders, acl_params = _in_placeholders(principals, f"{param_prefix}_acl")
        params.update(acl_params)
        acl_clause = (
            f" OR EXISTS (SELECT 1 FROM memory_acl macl "
            f"WHERE macl.memory_id = {p}id "
            f"AND macl.principal IN ({acl_placeholders}) "
            f"AND BITAND(macl.perm, {ACL_READ_BIT}) > 0)"
        )

    return (
        "("
        f"{p}owner_id = :{param_prefix}_owner"
        f" OR {p}federation_source IS NOT NULL"
        f" OR MOD(NVL({p}permission_mode, 0), 10) >= 4"
        f" OR (MOD(TRUNC(NVL({p}permission_mode, 0) / 10), 10) >= 4 "
        f"AND {p}group_id IS NOT NULL AND {group_clause})"
        f"{acl_clause}"
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


def _uuid_to_raw(value: str | bytes | uuid.UUID | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) != 16:
            raise ValueError("RAW UUID values must be exactly 16 bytes")
        return value
    if isinstance(value, uuid.UUID):
        return value.bytes
    return uuid.UUID(str(value)).bytes


def _raw_to_uuid(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(uuid.UUID(bytes=bytes(value)))


def _raw_token(value: str | bytes, *, length: int = 32) -> bytes:
    if isinstance(value, bytes):
        if len(value) != length:
            raise ValueError(f"RAW token values must be exactly {length} bytes")
        return value
    text = str(value)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raw = text.encode("utf-8")
    if len(raw) != length:
        raise ValueError(f"RAW token values must be exactly {length} bytes")
    return raw


def _raw_token_text(value: Any) -> str | None:
    if value is None:
        return None
    return bytes(value).hex()


def _json_text(value: Any, default: Any = None) -> str:
    if value is None:
        value = default
    return json.dumps(value, separators=(",", ":"))


def _json_value(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _ts_for_oracle(value: Any) -> Any:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return value


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
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning("Ignoring unparsable %s=%r; using default %.1f", name, raw, default)
        return default


def _env_flag(name: str) -> bool:
    """Return True if the env var equals ``YES`` / ``1`` / ``TRUE`` (case-insensitive)."""
    return runtime_env_value_stripped(name).upper() in {"YES", "1", "TRUE", "ON"}


def _build_oracle_session_callback(settings: Any) -> Any:
    """Build a session callback that pins NLS + optionally switches PDB.

    Runs on every new physical session checkout. Both ALTER SESSION
    statements are best-effort: benign errors (e.g. ORA-65049 when
    already inside the target PDB) are logged at DEBUG and the session
    continues. This keeps the pool usable in mixed PDB/CDB topologies
    while still giving operators a defensive locale + container pin.
    """
    pdb_target = oracle_pdb_env()

    async def _session_callback(conn: Any, requested_tag: Any) -> None:
        cur = None
        try:
            cur = conn.cursor()
            # Defensive NLS pinning: prevents locale-dependent decimal
            # parsing of vector literals + numeric binds.
            try:
                await cur.execute("ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '. '")
            except Exception as exc:  # pragma: no cover - driver-dependent
                _LOG.debug("ALTER SESSION SET NLS_NUMERIC_CHARACTERS failed: %s", exc)
            if pdb_target:
                try:
                    await cur.execute(f"ALTER SESSION SET CONTAINER = {pdb_target}")
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
                    await _call(cur.close)
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
    create_pool_params = inspect.signature(oracledb.create_pool_async).parameters
    if "statement_cache_size" in create_pool_params:
        kwargs.setdefault("statement_cache_size", stmt_cache)
    else:
        kwargs.setdefault("stmtcachesize", stmt_cache)
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
        embedding: Sequence[float] | None = None,
        created: Any,
        updated: Any,
    ) -> str:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        affected = 0
        vec_literal: str | None = None
        if embedding:
            vec_literal = _validate_and_format_vector(embedding)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memories (
                    id, content, category, subcategory, metadata, content_hash,
                    quality_rating, verbatim_content, owner_id, namespace, permission_mode,
                    source_model, source_provider, source_session, source_agent,
                    embedding, created, updated
                )
                SELECT
                    :id, :content, :category, :subcategory, :metadata, :content_hash,
                    :quality_rating, :verbatim_content, :owner_id, :namespace, :permission_mode,
                    :source_model, :source_provider, :source_session, :source_agent,
                    TO_VECTOR(:embedding),
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
                    "embedding": vec_literal,
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

    async def upsert_memory_embedding(self, tx: Transaction, memory_id: str, embedding: Sequence[float]) -> None:
        """Write a precomputed embedding to memories.embedding.

        Idempotent UPDATE; no-op when embedding is empty. Used by
        create_memory inline embed-on-write path (mnemos/api/routes/
        memories.py:946) + federation F-1.4 copy_embeddings consumer.
        2026-05-24: added so Oracle backend new-write rows actually
        land with vectors instead of NULL.
        """
        if not embedding:
            return
        vec_literal = _validate_and_format_vector(embedding)
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE memories SET embedding = TO_VECTOR(:vec) WHERE id = :id",
                {"vec": vec_literal, "id": memory_id},
            )
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
        exclude_superseded: bool = False,
    ) -> tuple[list[Row], int]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if exclude_superseded:
                where.append("m.consolidated_into IS NULL")
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
                       m.group_id, m.created, m.updated, m.archived_at,
                       m.consolidated_into, m.deleted_at
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
            "namespace",
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

    async def soft_delete_memory(
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
                "UPDATE memories SET deleted_at = SYSTIMESTAMP, updated = SYSTIMESTAMP WHERE " + " AND ".join(where),
                vis_params,
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return row

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
        exclude_superseded: bool = False,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if exclude_superseded:
                where.append("m.consolidated_into IS NULL")
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
                "m.quality_rating, m.owner_id, m.namespace, m.created, m.updated, "
                "m.consolidated_into "
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

    async def backfill_missing_content_hashes(
        self,
        tx: Transaction,
        *,
        batch_size: int = 500,
        apply: bool = False,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            if not apply:
                await _call(cursor.execute, "SELECT COUNT(*) AS cnt FROM memories WHERE content_hash IS NULL")
                row = await _fetchone_dict(cursor)
                return int((row or {}).get("cnt") or 0)
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET content_hash = LOWER(RAWTOHEX(STANDARD_HASH(
                           REPLACE(REPLACE(NVL(content, ''), CHR(13) || CHR(10), CHR(10)), CHR(13), CHR(10)),
                           'SHA256'
                       ))),
                       updated = SYSTIMESTAMP
                 WHERE id IN (
                    SELECT id
                      FROM memories
                     WHERE content_hash IS NULL
                     ORDER BY created ASC, id ASC
                     FETCH FIRST :batch_size ROWS ONLY
                 )
                   AND content_hash IS NULL
                """,
                {"batch_size": int(batch_size)},
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def find_duplicate_content_groups(
        self,
        tx: Transaction,
        *,
        namespace: str | None = None,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = [
                "deleted_at IS NULL",
                "archived_at IS NULL",
                "consolidated_into IS NULL",
                "content_hash IS NOT NULL",
            ]
            params: dict[str, Any] = {}
            if namespace is not None:
                where.append("namespace = :ns")
                params["ns"] = namespace
            sql = (
                "SELECT owner_id, namespace, content_hash, COUNT(*) AS duplicate_count, "
                "LISTAGG(id, CHR(31)) WITHIN GROUP (ORDER BY created DESC, quality_rating DESC NULLS LAST, id DESC) AS memory_ids, "
                "MIN(id) KEEP (DENSE_RANK FIRST ORDER BY created DESC, quality_rating DESC NULLS LAST, id DESC) AS keep_id, "
                "MIN(id) KEEP (DENSE_RANK FIRST ORDER BY created DESC, quality_rating DESC NULLS LAST, id DESC) AS canonical_id "
                "FROM (SELECT id, owner_id, namespace, content_hash, created, quality_rating, "
                "REPLACE(REPLACE(NVL(content, ''), CHR(13) || CHR(10), CHR(10)), CHR(13), CHR(10)) AS normalized_content "
                "FROM memories WHERE " + " AND ".join(where) + ") candidates "
                "GROUP BY owner_id, namespace, content_hash, normalized_content HAVING COUNT(*) > 1 "
                "ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC"
            )
            await _call(cursor.execute, sql, params)
            rows = await _fetch_all_dicts(cursor)
            for row in rows:
                raw = row.get("memory_ids") or ""
                row["memory_ids"] = [part for part in str(raw).split("\x1f") if part]
                row["duplicate_count"] = int(row.get("duplicate_count") or 0)
            return rows
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
        exclude_superseded: bool = False,
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
            if exclude_superseded:
                where.append("m.consolidated_into IS NULL")
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
            params["limit"] = max(limit, min(limit * 4, 200)) if boost_recency else limit
            # Oracle 23ai VECTOR_DISTANCE returns 0 for identical vectors
            # and grows with dissimilarity, so ORDER BY ASC matches the
            # Postgres pgvector ``<=>`` ordering. Keep ORDER BY as the
            # bare distance so Oracle 23ai can serve top-K from the
            # native vector index; recency boost is applied after fetch.
            rank = "VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), COSINE)"
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, "
                "m.group_id, m.created, m.updated, m.archived_at, "
                "m.consolidated_into, m.recall_count, m.last_recalled_at, "
                f"({rank}) AS rank_score "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                f"ORDER BY {rank} ASC "
                "FETCH FIRST :limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            rows = await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

        if boost_recency and rows:
            w = float(recency_weight)
            today = datetime.now(timezone.utc).date()
            rows.sort(key=lambda row: _boosted_rank_supersession_sort_key(row, today=today, recency_weight=w))
            rows = rows[:limit]

        return rows

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


class OracleCompressionQueueRepository(CompressionQueueRepository):
    """Oracle 23ai impl of the v3.1 compression work queue (job 019e7049
    CHILD A). Mirrors the canonical Postgres semantics
    (mnemos/domain/compression/worker_contest.py) so the distillation
    contest behaves identically on Oracle.

    Concurrency: ``dequeue`` and ``sweep_stale`` claim rows with
    ``FOR UPDATE SKIP LOCKED`` (proven in OracleAuditChainRepository).
    Oracle cannot combine ``FOR UPDATE`` with ``FETCH FIRST`` (ORA-02014)
    and applies ``ROWNUM`` before ``ORDER BY``, so the ordered claim
    opens the cursor unbounded and fetches only ``limit`` rows — Oracle
    locks rows incrementally as the SKIP-LOCKED cursor reads them, so
    only the fetched rows are locked.
    """

    async def enqueue_compression(
        self,
        tx: Transaction,
        *,
        memory_ids: list[str],
        reason: str,
        priority: int,
        scoring_profile: str,
    ) -> list[str]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            binds = {f"id{i}": mid for i, mid in enumerate(memory_ids)}
            placeholders = ",".join(f":id{i}" for i in range(len(memory_ids)))
            await _call(
                cursor.execute,
                f"SELECT id, owner_id FROM memories WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                binds,
            )
            rows = await _call(cursor.fetchall) or []
            owner_by_id = {r[0]: r[1] for r in rows}
            enqueued: list[str] = []
            for mid in memory_ids:
                if mid not in owner_by_id:
                    continue
                # Dup-pending dedup: skip if this memory already has a
                # 'pending' queue row — avoids flooding the queue with
                # duplicate work for the same memory across multiple
                # enqueue calls (e.g. rapid on_write triggers).
                await _call(
                    cursor.execute,
                    "SELECT 1 FROM memory_compression_queue "
                    "WHERE memory_id = :mid AND status = 'pending' "
                    "FETCH FIRST 1 ROW ONLY",
                    {"mid": mid},
                )
                if await _call(cursor.fetchone):
                    continue
                await _call(
                    cursor.execute,
                    "INSERT INTO memory_compression_queue "
                    "(memory_id, owner_id, reason, priority, scoring_profile) "
                    "VALUES (:memory_id, :owner_id, :reason, :priority, :scoring_profile)",
                    {
                        "memory_id": mid,
                        "owner_id": owner_by_id[mid],
                        "reason": reason,
                        "priority": priority,
                        "scoring_profile": scoring_profile,
                    },
                )
                enqueued.append(mid)
            return enqueued
        finally:
            await _call(cursor.close)

    async def enqueue_all_compression(
        self,
        tx: Transaction,
        *,
        reason: str,
        priority: int,
        scoring_profile: str,
        category: str | None,
        only_uncompressed: bool,
        limit: int,
    ) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where_parts = ["m.deleted_at IS NULL"]
            params: dict[str, Any] = {
                "reason": reason,
                "priority": priority,
                "scoring_profile": scoring_profile,
                "row_limit": int(limit),
            }
            if only_uncompressed:
                where_parts.append("NOT EXISTS (SELECT 1 FROM memory_compressed_variants v WHERE v.memory_id = m.id)")
            if category is not None:
                where_parts.append("m.category = :category")
                params["category"] = category
            where_sql = " AND ".join(where_parts)
            sql = (
                "INSERT INTO memory_compression_queue "
                "(memory_id, owner_id, reason, priority, scoring_profile) "
                "SELECT m.id, m.owner_id, :reason, :priority, :scoring_profile "
                f"FROM memories m WHERE {where_sql} "
                "ORDER BY LENGTH(m.content) DESC "
                "FETCH FIRST :row_limit ROWS ONLY"
            )
            await _call(cursor.execute, sql, params)
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def dequeue_compression(
        self,
        tx: Transaction,
        *,
        limit: int,
    ) -> list[Row]:
        if limit <= 0:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Ordered SKIP-LOCKED claim bounded to exactly `limit`.
            # Oracle cannot combine FOR UPDATE with FETCH FIRST
            # (ORA-02014), so we use a ROWNUM subquery: the inner
            # ORDER BY runs first, ROWNUM caps the row count, and
            # the outer FOR UPDATE SKIP LOCKED locks at most `limit`
            # rows regardless of driver prefetch/arraysize behaviour.
            # This replaces the prior prefetchrows+arraysize hack
            # which was fragile across python-oracledb versions and
            # could over-lock rows, starving peer workers.
            await _call(
                cursor.execute,
                "SELECT id, memory_id, owner_id, reason, scoring_profile, attempts "
                "FROM ("
                "  SELECT id, memory_id, owner_id, reason, scoring_profile, attempts "
                "  FROM memory_compression_queue "
                "  WHERE status = 'pending' "
                "  ORDER BY priority DESC, enqueued_at"
                ") "
                "WHERE ROWNUM <= :limit "
                "FOR UPDATE SKIP LOCKED",
                {"limit": int(limit)},
            )
            raw = await _call(cursor.fetchall) or []
            if not raw:
                return []
            cols = [d[0].lower() for d in cursor.description]
            claimed = [dict(zip(cols, r)) for r in raw]
            ids = [row["id"] for row in claimed]
            binds = {f"id{i}": qid for i, qid in enumerate(ids)}
            placeholders = ",".join(f":id{i}" for i in range(len(ids)))
            await _call(
                cursor.execute,
                "UPDATE memory_compression_queue "
                "SET status = 'running', started_at = SYSTIMESTAMP, "
                "    attempts = attempts + 1 "
                f"WHERE id IN ({placeholders})",
                binds,
            )
            # Reflect the post-claim attempts count the contest worker
            # captured via PG's RETURNING.
            for row in claimed:
                row["attempts"] = int(row.get("attempts") or 0) + 1
            return claimed
        finally:
            await _call(cursor.close)

    async def mark_compression_done(
        self,
        tx: Transaction,
        *,
        queue_id: str,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE memory_compression_queue "
                "SET status = 'done', finished_at = SYSTIMESTAMP, error = NULL "
                "WHERE id = :id",
                {"id": queue_id},
            )
        finally:
            await _call(cursor.close)

    async def mark_compression_failed(
        self,
        tx: Transaction,
        *,
        queue_id: str,
        error: str,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE memory_compression_queue "
                "SET status = 'failed', finished_at = SYSTIMESTAMP, error = :error "
                "WHERE id = :id",
                {"id": queue_id, "error": error},
            )
        finally:
            await _call(cursor.close)

    async def sweep_stale_compression(
        self,
        tx: Transaction,
        *,
        stale_threshold_secs: int,
        max_attempts: int,
    ) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Claim stale 'running' rows under SKIP-LOCKED. Oracle has no
            # UPDATE..FROM/RETURNING, so classify in Python over the
            # locked set + UPDATE per row — identical terminalization
            # rules to the PG _SWEEP_STALE_SQL. Sweep touches few rows
            # per batch, so the per-row UPDATE is acceptable.
            await _call(
                cursor.execute,
                "SELECT id, attempts, error FROM memory_compression_queue "
                "WHERE status = 'running' "
                "  AND (started_at IS NULL "
                "       OR started_at < SYSTIMESTAMP "
                "           - NUMTODSINTERVAL(:secs, 'SECOND')) "
                "FOR UPDATE SKIP LOCKED",
                {"secs": int(stale_threshold_secs)},
            )
            cols = [d[0].lower() for d in cursor.description]
            stale = [dict(zip(cols, r)) for r in (await _call(cursor.fetchall) or [])]
            swept = 0
            for row in stale:
                qid = row["id"]
                attempts = int(row.get("attempts") or 0)
                err = row.get("error")
                terminalize = attempts >= max_attempts and err is not None and not str(err).startswith("infra_retry:")
                if terminalize:
                    await _call(
                        cursor.execute,
                        "UPDATE memory_compression_queue "
                        "SET status = 'failed', finished_at = SYSTIMESTAMP, "
                        "    error = :error WHERE id = :id",
                        {
                            "id": qid,
                            "error": (f"stranded_running: exceeded stale threshold after {attempts} attempts"),
                        },
                    )
                elif attempts >= max_attempts:
                    # infra-stranded: reset + decrement so genuine
                    # retries still observe the attempts budget.
                    await _call(
                        cursor.execute,
                        "UPDATE memory_compression_queue "
                        "SET status = 'pending', started_at = NULL, "
                        "    finished_at = NULL, attempts = GREATEST(attempts - 1, 0), "
                        "    error = 'infra_retry: stale-recovered without "
                        "content-failure breadcrumb' WHERE id = :id",
                        {"id": qid},
                    )
                else:
                    await _call(
                        cursor.execute,
                        "UPDATE memory_compression_queue "
                        "SET status = 'pending', started_at = NULL, "
                        "    finished_at = NULL, error = NULL WHERE id = :id",
                        {"id": qid},
                    )
                swept += 1
            return swept
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

    async def _registry_rows(self, tx: Transaction) -> list[dict[str, Any]]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT model_id, display_name, family, input_cost_per_mtok, "
                "output_cost_per_mtok, capabilities, arena_score, arena_rank, "
                "graeae_weight, context_window "
                "FROM model_registry "
                "WHERE available = 1 AND NVL(deprecated, 0) = 0",
            )
            raw_rows = await _fetch_all_dicts(cursor)
        except Exception:
            return []
        finally:
            await _call(cursor.close)

        # Schema lacks explicit `provider` column — derive from model_id prefix
        # (e.g. "nvidia/qwen3-coder-480b" -> provider="nvidia", model_id="qwen3-coder-480b")
        # or from family for bare model_ids like "gemini-2.5-flash-lite".
        normalized: list[dict[str, Any]] = []
        for row in raw_rows:
            mid = str(row.get("model_id") or "")
            family = str(row.get("family") or "")
            if "/" in mid:
                provider, model_local = mid.split("/", 1)
            elif "/" in family:
                provider = family.split("/", 1)[0]
                model_local = mid
            else:
                # Heuristic: try gemini/openai/anthropic family prefixes
                low = mid.lower()
                if low.startswith("gemini-"):
                    provider = "gemini"
                elif low.startswith("gpt-") or low.startswith("o3") or low.startswith("o4"):
                    provider = "openai"
                elif low.startswith("claude-"):
                    provider = "anthropic"
                elif low.startswith("grok-"):
                    provider = "xai"
                elif low.startswith("llama-") or low.startswith("kimi-"):
                    provider = "groq"
                else:
                    provider = family or "unknown"
                model_local = mid
            row["provider"] = provider
            row["model_id"] = model_local if model_local else mid
            normalized.append(row)
        return normalized

    async def fetch_recommended_model(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        from mnemos.core.recommendation import choose_recommended_model

        rows = await self._registry_rows(tx)
        if not rows:
            return None, []
        return choose_recommended_model(rows, task_type, cost_budget, quality_floor)

    async def fetch_model_recommendation(
        self,
        tx: Transaction,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        model, _ = await self.fetch_recommended_model(tx, task_type, cost_budget, quality_floor)
        return model

    async def lookup_provider_for_model(self, tx: Transaction, model: str) -> str | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT provider FROM model_registry WHERE model_id = :m AND ROWNUM = 1",
                {"m": model},
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            return row.get("provider") if row else None
        except Exception:
            return None
        finally:
            await _call(cursor.close)

    async def fetch_available_models(self, tx: Transaction) -> list[Row]:
        return await self._registry_rows(tx)

    async def fetch_model_provider(self, tx: Transaction, model_id: str) -> str | None:
        _ = (tx, model_id)
        return None

    # ── model-registry WRITES (Oracle MERGE; daily provider sync) ──────────────
    async def upsert_model(self, tx: Transaction, model: dict[str, Any]) -> bool:
        """Insert-or-update one model_registry row. Returns True if inserted."""
        import json as _json

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT id FROM model_registry WHERE provider = :p AND model_id = :m",
                {"p": model["provider"], "m": model["model_id"]},
            )
            existing = await _call(cursor.fetchone)
            binds = {
                "p": model["provider"],
                "m": model["model_id"],
                "dn": (model.get("display_name") or model["model_id"])[:400],
                "fam": (model.get("family") or "")[:200] or None,
                "cw": model.get("context_window"),
                "mot": model.get("max_output_tokens"),
                "caps": _json.dumps(model.get("capabilities", [])),
                "ic": model.get("input_cost_per_mtok", 0) or 0,
                "oc": model.get("output_cost_per_mtok", 0) or 0,
                "cr": model.get("cache_read_per_mtok", 0) or 0,
                "cwr": model.get("cache_write_per_mtok", 0) or 0,
                "rawp": _json.dumps(model.get("raw", {})),
            }
            if existing is None:
                await _call(
                    cursor.execute,
                    "INSERT INTO model_registry (id, provider, model_id, display_name, "
                    "family, context_window, max_output_tokens, capabilities, "
                    "input_cost_per_mtok, output_cost_per_mtok, cache_read_per_mtok, "
                    "cache_write_per_mtok, available, deprecated, first_seen, last_seen, "
                    "last_synced, raw_payload) VALUES (:id, :p, :m, :dn, :fam, :cw, :mot, "
                    ":caps, :ic, :oc, :cr, :cwr, 1, 0, SYSTIMESTAMP, SYSTIMESTAMP, "
                    "SYSTIMESTAMP, :rawp)",
                    {**binds, "id": f"{model['provider']}:{model['model_id']}"[:100]},
                )
                return True
            await _call(
                cursor.execute,
                "UPDATE model_registry SET display_name = :dn, family = :fam, "
                "context_window = NVL(:cw, context_window), "
                "max_output_tokens = NVL(:mot, max_output_tokens), "
                "capabilities = :caps, input_cost_per_mtok = :ic, "
                "output_cost_per_mtok = :oc, cache_read_per_mtok = :cr, "
                "cache_write_per_mtok = :cwr, available = 1, deprecated = 0, "
                "last_seen = SYSTIMESTAMP, last_synced = SYSTIMESTAMP, raw_payload = :rawp "
                "WHERE provider = :p AND model_id = :m",
                binds,
            )
            return False
        finally:
            await _call(cursor.close)

    async def mark_models_unavailable(self, tx: Transaction, provider: str, seen_model_ids: Sequence[str]) -> int:
        """Mark this provider's models NOT seen in the latest sync as unavailable."""
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            binds: dict[str, Any] = {"p": provider}
            if seen_model_ids:
                placeholders = ", ".join(f":s{i}" for i in range(len(seen_model_ids)))
                for i, mid in enumerate(seen_model_ids):
                    binds[f"s{i}"] = mid
                sql = (
                    "UPDATE model_registry SET available = 0, deprecated = 1, "
                    "last_synced = SYSTIMESTAMP WHERE provider = :p AND available = 1 "
                    f"AND model_id NOT IN ({placeholders})"
                )
            else:
                sql = (
                    "UPDATE model_registry SET available = 0, deprecated = 1, "
                    "last_synced = SYSTIMESTAMP WHERE provider = :p AND available = 1"
                )
            await _call(cursor.execute, sql, binds)
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def write_model_sync_log(
        self,
        tx: Transaction,
        *,
        provider: str,
        models_found: int,
        added: int,
        updated: int,
        deprecated: int,
        error: str | None,
        duration_ms: int,
    ) -> None:
        _ = duration_ms  # Oracle model_registry_sync_log has no duration column
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "INSERT INTO model_registry_sync_log (id, provider, synced_at, "
                "models_found, models_added, models_updated, models_deprecated, error) "
                "VALUES (:id, :p, SYSTIMESTAMP, :f, :a, :u, :d, :e)",
                {
                    "id": uuid.uuid4().bytes,
                    "p": provider,
                    "f": models_found,
                    "a": added,
                    "u": updated,
                    "d": deprecated,
                    "e": (error or None) and str(error)[:4000],
                },
            )
        finally:
            await _call(cursor.close)

    async def update_arena_score(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        family: str,
        arena_score: float,
        arena_rank: int,
        graeae_weight: float,
    ) -> int:
        """Apply arena score by exact model_id, else by family. Returns rows updated."""
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            score_binds = {"sc": arena_score, "rk": arena_rank, "w": graeae_weight}
            await _call(
                cursor.execute,
                "UPDATE model_registry SET arena_score = :sc, arena_rank = :rk, "
                "graeae_weight = :w WHERE provider = :p AND model_id = :m",
                {**score_binds, "p": provider, "m": model_id},
            )
            n = int(getattr(cursor, "rowcount", 0) or 0)
            if n == 0:
                await _call(
                    cursor.execute,
                    "UPDATE model_registry SET arena_score = :sc, arena_rank = :rk, "
                    "graeae_weight = :w WHERE provider = :p AND family = :fam",
                    {**score_binds, "p": provider, "fam": family},
                )
                n = int(getattr(cursor, "rowcount", 0) or 0)
            return n
        finally:
            await _call(cursor.close)

    # ── KNEMON Step 2: pricing ingest from llm_provider_registry.json ──────────

    async def upsert_model_pricing(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        price_in: float,
        price_out: float,
        price_cached: float,
    ) -> tuple[int, dict | None]:
        """Upsert price columns into model_registry. Returns (rows_updated, old_prices_or_None)."""
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Read current prices to detect change
            await _call(
                cursor.execute,
                "SELECT price_in, price_out, price_cached FROM model_registry WHERE provider = :p AND model_id = :m",
                {"p": provider, "m": model_id},
            )
            row = await _call(cursor.fetchone)
            if row is None:
                return 0, None

            old = {
                "price_in": float(row[0] or 0),
                "price_out": float(row[1] or 0),
                "price_cached": float(row[2] or 0),
            }
            # Only update if pricing actually changed
            if (
                abs(old["price_in"] - price_in) < 0.000001
                and abs(old["price_out"] - price_out) < 0.000001
                and abs(old["price_cached"] - price_cached) < 0.000001
            ):
                return 0, None

            await _call(
                cursor.execute,
                "UPDATE model_registry SET price_in = :pi, price_out = :po, "
                "price_cached = :pc, price_updated_at = SYSTIMESTAMP "
                "WHERE provider = :p AND model_id = :m",
                {
                    "pi": price_in,
                    "po": price_out,
                    "pc": price_cached,
                    "p": provider,
                    "m": model_id,
                },
            )
            n = int(getattr(cursor, "rowcount", 0) or 0)
            return n, old
        finally:
            await _call(cursor.close)

    async def write_price_history(
        self,
        tx: Transaction,
        *,
        provider: str,
        model_id: str,
        price_in: float,
        price_out: float,
        price_cached: float,
        prices: dict | None = None,
    ) -> None:
        """Write a price_history row."""
        import json as _json

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            prices_json = _json.dumps(prices or {})
            await _call(
                cursor.execute,
                "INSERT INTO price_history (id, provider, model_id, price_in, "
                "price_out, price_cached, prices, recorded_at) "
                "VALUES (:id, :p, :m, :pi, :po, :pc, :pj, SYSTIMESTAMP)",
                {
                    "id": uuid.uuid4().bytes,
                    "p": provider,
                    "m": model_id,
                    "pi": price_in,
                    "po": price_out,
                    "pc": price_cached,
                    "pj": prices_json,
                },
            )
        finally:
            await _call(cursor.close)


class OracleOAuthRepository(OAuthRepository):
    async def list_enabled_providers(self, tx: Transaction) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT name, display_name, kind, enabled FROM oauth_providers WHERE enabled = 1 ORDER BY display_name",
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def get_provider(self, tx: Transaction, name: str) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT name, kind, issuer_url, client_id, client_secret, scope, "
                "authorize_url, token_url, userinfo_url, enabled "
                "FROM oauth_providers WHERE name = :name",
                {"name": name},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def provision_or_link_user(self, tx: Transaction, **kwargs: Any) -> tuple[str, str]:
        raise NotImplementedError("Oracle OAuth identity provisioning repository is not implemented")

    async def create_session(self, tx: Transaction, **kwargs: Any) -> str:
        raise NotImplementedError("Oracle OAuth session creation repository is not implemented")

    async def revoke_session(self, tx: Transaction, session_id: str) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE oauth_sessions SET revoked = 1, revoked_at = SYSTIMESTAMP "
                "WHERE session_id = :session_id AND revoked = 0",
                {"session_id": session_id},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def revoke_all_sessions(self, tx: Transaction, user_id: str) -> int:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE oauth_sessions SET revoked = 1, revoked_at = SYSTIMESTAMP "
                "WHERE user_id = :user_id AND revoked = 0",
                {"user_id": user_id},
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def get_identity_for_session(self, tx: Transaction, session_id: str) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT i.id, i.user_id, i.provider, i.external_id, i.email, i.display_name, "
                "i.last_login_at, i.created FROM oauth_sessions s "
                "JOIN oauth_identities i ON i.id = s.identity_id "
                "WHERE s.session_id = :session_id AND s.revoked = 0",
                {"session_id": session_id},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)


class OracleAclRepository(AclRepository):
    """Oracle per-principal memory ACL grants.

    Upsert via MERGE on the ``(memory_id, principal)`` natural key so a
    repeated grant updates ``perm``/``granted_by`` rather than violating
    the unique constraint.
    """

    async def grant_acl(
        self,
        tx: Transaction,
        *,
        memory_id: str,
        principal: str,
        perm: int,
        granted_by: str,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            binds = {
                "memory_id": memory_id,
                "principal": principal,
                "perm": perm,
                "granted_by": granted_by,
            }
            merge_sql = (
                "MERGE INTO memory_acl t "
                "USING (SELECT :memory_id AS memory_id, :principal AS principal FROM dual) s "
                "ON (t.memory_id = s.memory_id AND t.principal = s.principal) "
                "WHEN MATCHED THEN UPDATE SET t.perm = :perm, t.granted_by = :granted_by "
                "WHEN NOT MATCHED THEN INSERT (memory_id, principal, perm, granted_by) "
                "VALUES (:memory_id, :principal, :perm, :granted_by)"
            )
            # MERGE is not atomic against a concurrent first INSERT of the
            # same (memory_id, principal): both sessions can take the
            # NOT MATCHED branch and one hits ORA-00001. The ABC contract
            # (mirroring Postgres ON CONFLICT) is that a repeat grant must
            # never duplicate-key — so on a unique violation we retry once,
            # where the row now exists and the MATCHED/UPDATE branch wins.
            try:
                await _call(cursor.execute, merge_sql, binds)
            except Exception as exc:  # noqa: BLE001 — re-raised unless it's the dup race
                if not _is_unique_violation(exc):
                    raise
                await _call(cursor.execute, merge_sql, binds)
            await _call(
                cursor.execute,
                "SELECT memory_id, principal, perm, granted_by, created AS created_at "
                "FROM memory_acl WHERE memory_id = :memory_id AND principal = :principal",
                {"memory_id": memory_id, "principal": principal},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def revoke_acl(self, tx: Transaction, *, memory_id: str, principal: str) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "DELETE FROM memory_acl WHERE memory_id = :memory_id AND principal = :principal",
                {"memory_id": memory_id, "principal": principal},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_acl(self, tx: Transaction, memory_id: str) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT memory_id, principal, perm, granted_by, created AS created_at "
                "FROM memory_acl WHERE memory_id = :memory_id ORDER BY principal",
                {"memory_id": memory_id},
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def is_group_admin(
        self,
        tx: Transaction,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT 1 FROM user_groups WHERE user_id = :user_id AND group_id = :group_id AND is_admin = 1",
                {"user_id": user_id, "group_id": group_id},
            )
            return await _call(cursor.fetchone) is not None
        finally:
            await _call(cursor.close)


class OracleSessionsRepository(SessionsRepository):
    async def create_session(self, tx: Transaction, **kwargs: Any) -> Row:
        raise NotImplementedError("Oracle chat sessions repository is not implemented")

    async def get_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM sessions WHERE id = :id AND user_id = :user_id "
                "AND namespace = :namespace AND deleted_at IS NULL",
                {"id": session_id, "user_id": user_id, "namespace": namespace},
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def list_injected_memory_ids(self, tx: Transaction, session_id: str, limit: int = 10) -> list[str]:
        _ = (tx, session_id, limit)
        return []

    async def add_message(self, tx: Transaction, **kwargs: Any) -> Any:
        raise NotImplementedError("Oracle chat session messages repository is not implemented")

    async def fetch_provider_history(self, tx: Transaction, session_id: str) -> list[Row]:
        _ = (tx, session_id)
        return []

    async def add_memory_injections(self, tx: Transaction, **kwargs: Any) -> None:
        _ = (tx, kwargs)

    async def update_metrics(self, tx: Transaction, **kwargs: Any) -> None:
        _ = (tx, kwargs)

    async def fetch_history(self, tx: Transaction, session_id: str, limit: int, offset: int) -> tuple[list[Row], int]:
        _ = (tx, session_id, limit, offset)
        return [], 0

    async def delete_session(self, tx: Transaction, session_id: str, user_id: str, namespace: str) -> bool:
        _ = (tx, session_id, user_id, namespace)
        return False


class OracleConsultationsRepository(ConsultationsRepository):
    async def resolve_tier_lineup(self, tx: Transaction, tier: str) -> list[Row]:
        _ = (tx, tier)
        return []

    async def resolve_models(self, tx: Transaction, model_ids: Sequence[str]) -> list[Row]:
        _ = (tx, model_ids)
        return []

    async def create_consultation_with_audit(self, tx: Transaction, **kwargs: Any) -> Any:
        """Insert a consultation + audit-chain link + memory refs in one tx.

        Mirrors SqliteConsultationsRepository.create_consultation_with_audit
        (mnemos/persistence/sqlite.py); Oracle 23ai schema at
        db/migrations_oracle/0002_graeae.sql defines graeae_consultations,
        graeae_audit_log, consultation_memory_refs with VARCHAR2(36) PKs and
        app-side UUIDs.
        """
        conn = _conn_from_tx(tx)
        consultation_id = uuid.uuid4().hex
        cursor = await _call(conn.cursor)
        try:
            # graeae_consultations insert. Note: "mode" is a reserved word
            # in Oracle; the DDL quotes the column, so the SQL must too.
            # NOTE: bind var names cannot be Oracle reserved words (ORA-01745).
            # The "mode" column is reserved → rename the bind to mode_val while
            # keeping the column name "mode" (quoted) on the INSERT.
            await _call(
                cursor.execute,
                "INSERT INTO graeae_consultations "
                "(id, prompt, task_type, consensus_response, consensus_score, "
                'winning_muse, cost, latency_ms, "mode", owner_id, namespace) '
                "VALUES (:id, :prompt, :task_type, :consensus_response, :consensus_score, "
                ":winning_muse, :cost, :latency_ms, :mode_val, :owner_id, :namespace)",
                {
                    "id": consultation_id,
                    "prompt": kwargs["prompt"],
                    "task_type": kwargs["task_type"],
                    "consensus_response": kwargs["consensus_response"][:500],
                    "consensus_score": kwargs["consensus_score"],
                    "winning_muse": kwargs["winning_muse"],
                    "cost": kwargs["cost"],
                    "latency_ms": kwargs["latency_ms"],
                    "mode_val": kwargs["mode"],
                    "owner_id": kwargs["owner_id"],
                    "namespace": kwargs["namespace"],
                },
            )

            # Audit chain — fetch latest link, hash forward, insert next.
            prompt_hash = hashlib.sha256(kwargs["prompt"].encode()).hexdigest()
            response_hash = hashlib.sha256(kwargs["consensus_response"].encode()).hexdigest()
            await _call(
                cursor.execute,
                "SELECT id, chain_hash FROM graeae_audit_log ORDER BY sequence_num DESC FETCH FIRST 1 ROWS ONLY",
            )
            prev_row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            prev_chain = prev_row["chain_hash"] if prev_row else kwargs["genesis_hash"]
            chain_hash = hashlib.sha256((prev_chain + prompt_hash + response_hash).encode()).hexdigest()
            audit_id = uuid.uuid4().hex
            # provider is NOT NULL on Oracle but winning_muse can be None on
            # all-muses-failed consensus path (route at consultations.py L361
            # documents this safe-degradation). Coerce to sentinel "[none]"
            # so the audit chain still records the event.
            provider_val = kwargs["winning_muse"] or "[none]"
            # response_text + consensus_response can be "" → Oracle treats as
            # NULL; that's now allowed (we ALTERed CONSENSUS_RESPONSE NULL),
            # but RESPONSE_TEXT on audit_log might still be NOT NULL. The
            # MODIFY ... NULL migration applied to consensus_response only;
            # if audit_log.response_text fails NOT NULL, coerce "" to None
            # so the SQL drops the bind and Oracle stores NULL (which is
            # itself a violation if NOT NULL set, but the table allows it).
            await _call(
                cursor.execute,
                "INSERT INTO graeae_audit_log "
                "(id, consultation_id, prompt, prompt_hash, provider, response_text, "
                "response_hash, chain_hash, prev_id, prev_chain_hash, task_type, quality_score) "
                "VALUES (:id, :consultation_id, :prompt, :prompt_hash, :provider, :response_text, "
                ":response_hash, :chain_hash, :prev_id, :prev_chain_hash, :task_type, :quality_score)",
                {
                    "id": audit_id,
                    "consultation_id": consultation_id,
                    "prompt": kwargs["prompt"],
                    "prompt_hash": prompt_hash,
                    "provider": provider_val,
                    "response_text": kwargs["consensus_response"] or "[empty]",
                    "response_hash": response_hash,
                    "chain_hash": chain_hash,
                    "prev_id": prev_row["id"] if prev_row else None,
                    "prev_chain_hash": prev_chain,
                    "task_type": kwargs["task_type"] or "reasoning",
                    "quality_score": kwargs["consensus_score"],
                },
            )

            # Memory refs — Oracle has no INSERT OR IGNORE; the UNIQUE
            # constraint unique_consultation_memory raises ORA-00001 on
            # duplicate, so we swallow that one error code only.
            for memory_id in kwargs["memory_ids"]:
                ref_id = uuid.uuid4().hex
                try:
                    await _call(
                        cursor.execute,
                        "INSERT INTO consultation_memory_refs "
                        "(id, consultation_id, memory_id) "
                        "VALUES (:id, :consultation_id, :memory_id)",
                        {
                            "id": ref_id,
                            "consultation_id": consultation_id,
                            "memory_id": memory_id,
                        },
                    )
                except Exception as exc:
                    # ORA-00001 unique violation — duplicate ref, safe to skip.
                    msg = str(exc)
                    if "ORA-00001" not in msg:
                        raise
        finally:
            await _call(cursor.close)
        return consultation_id

    async def list_audit_log(self, tx: Transaction, **kwargs: Any) -> list[Row]:
        _ = (tx, kwargs)
        return []

    async def fetch_audit_chain(self, tx: Transaction, **kwargs: Any) -> list[Row]:
        _ = (tx, kwargs)
        return []

    async def get_consultation(self, tx: Transaction, **kwargs: Any) -> Row | None:
        _ = (tx, kwargs)
        return None

    async def get_consultation_artifacts(self, tx: Transaction, **kwargs: Any) -> tuple[Row | None, list[Row]]:
        _ = (tx, kwargs)
        return None, []


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
        include_embedding: bool = False,
    ) -> list[Row]:
        _ = prefer_compressed
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            where = [
                "m.federation_source IS NULL",
                "MOD(m.permission_mode, 10) >= 4",
                "m.deleted_at IS NULL",
                "m.archived_at IS NULL",
                "m.consolidated_into IS NULL",
                "(m.namespace IS NULL OR m.namespace <> :vault_ns)",
            ]
            params: dict[str, Any] = {"limit": limit, "vault_ns": VAULT_NAMESPACE}
            if since_updated is not None and since_id is not None:
                where.append("(m.updated > :upd OR (m.updated = :upd AND m.id > :since_id))")
                # Explicit TIMESTAMP_TZ bind to avoid thin-mode coercion to VARCHAR
                # which was causing infinite-loop pulls on ACHILLES (id < since_id).
                import oracledb

                upd_var = cursor.var(oracledb.DB_TYPE_TIMESTAMP_TZ)
                upd_var.setvalue(0, since_updated)
                params["upd"] = upd_var
                params["since_id"] = since_id
            if namespaces:
                ns_ph, ns_params = _in_placeholders(namespaces, "ns")
                where.append(f"m.namespace IN ({ns_ph})")
                params.update(ns_params)
            if categories:
                cat_ph, cat_params = _in_placeholders(categories, "cat")
                where.append(f"m.category IN ({cat_ph})")
                params.update(cat_params)
            # v6.1 F-1.2: optional embedding + embedding_model literal columns.
            # See docs/v6.1-federation-embeddings-copy.md.
            embed_cols = ""
            if include_embedding:
                from mnemos.core.config import embed_http_model_override, get_settings as _gs

                try:
                    _http_model = embed_http_model_override()
                    _model = _http_model or (_gs().providers.inference_embed_model or "").strip() or "unknown"
                except Exception:
                    _model = "unknown"
                # Single-quote escape for embedded literal.
                _model_escaped = _model.replace("'", "''")
                embed_cols = f", m.embedding AS embedding, '{_model_escaped}' AS embedding_model"
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.verbatim_content, m.owner_id, m.namespace, "
                "m.permission_mode, m.source_model, m.source_provider, "
                "m.source_session, m.source_agent, m.created, m.updated, "
                "m.archived_at" + embed_cols + " FROM memories m WHERE " + " AND ".join(where) + " "
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
                "m.federation_source IS NULL",
                "MOD(m.permission_mode, 10) >= 4",
                "m.deleted_at IS NULL",
                "m.archived_at IS NULL",
                "m.consolidated_into IS NULL",
                "(m.namespace IS NULL OR m.namespace <> :vault_ns)",
            ]
            params: dict[str, Any] = {"id": memory_id, "vault_ns": VAULT_NAMESPACE}
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


class OracleAuditChainRepository(AuditChainRepository):
    """Oracle impl of v6.2 M-2.2.1 audit chain.

    Tables: ``memory_audit_chain`` + ``memory_audit_roots``
    (migrations 0029 + 0030; shipped at 614d483 for Oracle).

    Bytes columns are RAW(16/32/64) — bind via plain ``bytes``
    through python-oracledb (driver coerces). Timestamps are
    TIMESTAMP WITH TIME ZONE — bind ISO 8601 strings via
    ``CAST(:ts AS TIMESTAMP WITH TIME ZONE)``.

    Concurrent sealer instances coexist via Oracle's
    ``FOR UPDATE SKIP LOCKED`` (11g+).
    """

    async def get_latest_audit_entry(
        self,
        tx: Transaction,
        memory_id: bytes,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT * FROM (
                    SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                           op, payload_hash, writer_id, writer_pubkey,
                           signature, signed_at, global_root, global_seq
                    FROM memory_audit_chain
                    WHERE memory_id = :memory_id
                    ORDER BY signed_at DESC
                ) WHERE ROWNUM = 1
                """,
                {"memory_id": memory_id},
            )
            row = await _call(cursor.fetchone)
            if row is None:
                return None
            cols = [d[0].lower() for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            await _call(cursor.close)

    async def insert_audit_entry(
        self,
        tx: Transaction,
        *,
        entry_id: bytes,
        memory_id: bytes,
        prev_entry_id: bytes | None,
        prev_entry_hash: bytes | None,
        op: str,
        payload_hash: bytes,
        writer_id: str,
        writer_pubkey: bytes,
        signature: bytes,
        signed_at: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_audit_chain (
                    entry_id, memory_id, prev_entry_id, prev_entry_hash,
                    op, payload_hash, writer_id, writer_pubkey,
                    signature, signed_at
                )
                VALUES (
                    :entry_id, :memory_id, :prev_entry_id, :prev_entry_hash,
                    :op, :payload_hash, :writer_id, :writer_pubkey,
                    :signature, TO_TIMESTAMP_TZ(:signed_at, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM')
                )
                """,
                {
                    "entry_id": entry_id,
                    "memory_id": memory_id,
                    "prev_entry_id": prev_entry_id,
                    "prev_entry_hash": prev_entry_hash,
                    "op": op,
                    "payload_hash": payload_hash,
                    "writer_id": writer_id,
                    "writer_pubkey": writer_pubkey,
                    "signature": signature,
                    "signed_at": signed_at,
                },
            )
        finally:
            await _call(cursor.close)

    async def claim_unsealed_window(
        self,
        tx: Transaction,
        *,
        max_window_seconds: int,
        limit: int,
    ) -> list[Row]:
        """Claim oldest unsealed entries older than the cutoff using
        ``FOR UPDATE SKIP LOCKED``. Oracle's NUMTODSINTERVAL is the
        equivalent of PG's ``interval '<n> seconds'``.
        """
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, signature, signed_at
                FROM memory_audit_chain
                WHERE global_root IS NULL
                  AND signed_at <= SYSTIMESTAMP - NUMTODSINTERVAL(:secs, 'SECOND')
                  AND ROWNUM <= :max_rows
                ORDER BY signed_at ASC, entry_id ASC
                FOR UPDATE SKIP LOCKED
                """,
                {"secs": int(max_window_seconds), "max_rows": int(limit)},
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return []
            cols = [d[0].lower() for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            await _call(cursor.close)

    async def stamp_window_with_root(
        self,
        tx: Transaction,
        *,
        entry_ids: list[bytes],
        global_root: bytes,
        starting_seq: int,
    ) -> None:
        if not entry_ids:
            return
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Oracle has no array-unnest; loop UPDATE preserving
            # caller-supplied seq order. Hot-path correctness > batch
            # microseconds here — sealer runs at 60s cadence.
            for offset, eid in enumerate(entry_ids):
                await _call(
                    cursor.execute,
                    """
                    UPDATE memory_audit_chain
                    SET global_root = :root, global_seq = :seq
                    WHERE entry_id = :eid
                    """,
                    {
                        "root": global_root,
                        "seq": starting_seq + offset,
                        "eid": eid,
                    },
                )
        finally:
            await _call(cursor.close)

    async def insert_audit_root(
        self,
        tx: Transaction,
        *,
        global_root: bytes,
        window_start: Any,
        window_end: Any,
        entry_count: int,
        root_signature: bytes,
        signer_pubkey: bytes,
        sealed_at: Any,
    ) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_audit_roots (
                    global_root, window_start, window_end, entry_count,
                    root_signature, signer_pubkey, sealed_at
                )
                VALUES (
                    :global_root,
                    TO_TIMESTAMP_TZ(:window_start, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'),
                    TO_TIMESTAMP_TZ(:window_end, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'),
                    :entry_count,
                    :root_signature, :signer_pubkey,
                    TO_TIMESTAMP_TZ(:sealed_at, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM')
                )
                """,
                {
                    "global_root": global_root,
                    "window_start": window_start,
                    "window_end": window_end,
                    "entry_count": int(entry_count),
                    "root_signature": root_signature,
                    "signer_pubkey": signer_pubkey,
                    "sealed_at": sealed_at,
                },
            )
        finally:
            await _call(cursor.close)

    async def list_window_entries(
        self,
        tx: Transaction,
        global_root: bytes,
    ) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, memory_id, signature, signed_at,
                       global_seq, payload_hash, op
                FROM memory_audit_chain
                WHERE global_root = :root
                ORDER BY signed_at ASC, entry_id ASC
                """,
                {"root": global_root},
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return []
            cols = [d[0].lower() for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            await _call(cursor.close)

    async def get_audit_entry_by_id(
        self,
        tx: Transaction,
        entry_id: bytes,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                FROM memory_audit_chain
                WHERE entry_id = :eid
                """,
                {"eid": entry_id},
            )
            row = await _call(cursor.fetchone)
            if row is None:
                return None
            cols = [d[0].lower() for d in cursor.description]
            return dict(zip(cols, row))
        finally:
            await _call(cursor.close)

    async def get_chain_stats(self, tx: Transaction) -> dict:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT
                    COUNT(*),
                    COUNT(CASE WHEN global_root IS NULL THEN 1 END),
                    MIN(CASE WHEN global_root IS NULL THEN signed_at END)
                FROM memory_audit_chain
                """,
            )
            crow = await _call(cursor.fetchone)
            total = int(crow[0] or 0)
            unsealed = int(crow[1] or 0)
            oldest = crow[2]
            await _call(
                cursor.execute,
                """
                SELECT COUNT(*), MAX(sealed_at)
                FROM memory_audit_roots
                """,
            )
            rrow = await _call(cursor.fetchone)
            root_count = int(rrow[0] or 0)
            last_sealed = rrow[1]
        finally:
            await _call(cursor.close)
        return {
            "total_entries": total,
            "unsealed_count": unsealed,
            "oldest_unsealed_signed_at": (
                oldest.isoformat() if hasattr(oldest, "isoformat") else (str(oldest) if oldest else None)
            ),
            "sealed_root_count": root_count,
            "last_sealed_at": (
                last_sealed.isoformat()
                if hasattr(last_sealed, "isoformat")
                else (str(last_sealed) if last_sealed else None)
            ),
        }

    async def get_latest_audit_entries_batch(
        self,
        tx: Transaction,
        memory_ids: list[bytes],
    ) -> dict[bytes, Row]:
        """Oracle 12c+ ROW_NUMBER() OVER PARTITION BY. Each memory_id
        binds individually since python-oracledb doesn't natively
        accept a list-binding for RAW types in an IN clause without
        an array-type registration step.
        """
        if not memory_ids:
            return {}
        placeholders = ",".join(f":m{i}" for i in range(len(memory_ids)))
        params = {f"m{i}": mid for i, mid in enumerate(memory_ids)}
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                f"""
                SELECT entry_id, memory_id, prev_entry_id, prev_entry_hash,
                       op, payload_hash, writer_id, writer_pubkey,
                       signature, signed_at, global_root, global_seq
                FROM (
                  SELECT m.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY memory_id
                           ORDER BY signed_at DESC, entry_id DESC
                         ) AS rn
                  FROM memory_audit_chain m
                  WHERE memory_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                params,
            )
            rows = await _call(cursor.fetchall)
            if not rows:
                return {}
            cols = [d[0].lower() for d in cursor.description]
            out: dict[bytes, Row] = {}
            for r in rows:
                d = dict(zip(cols, r))
                out[d["memory_id"]] = d
            return out
        finally:
            await _call(cursor.close)


class OracleBackend:
    """Oracle persistence facade backed by a python-oracledb async pool."""

    _supports_core_persistence = True
    _supports_oauth_persistence = True
    _supports_sessions_persistence = True
    _supports_consultations_persistence = True
    _supports_federation_persistence = True
    _supports_audit_persistence = True
    _supports_state_persistence = True
    supports_webhooks = True  # Oracle has a real OracleWebhookRepository (see .webhooks)

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
        self._compression_queue_repo = OracleCompressionQueueRepository()
        self._webhooks_repo = OracleWebhookRepository()
        self._consultations_audit_repo = OracleConsultationAuditRepository()
        self._oauth_repo = OracleOAuthRepository()
        self._sessions_repo = OracleSessionsRepository()
        self._consultations_repo = OracleConsultationsRepository()
        self._federation_repo = OracleFederationRepository()
        self._state_kv_repo = OracleStateRepository()
        self._audit_chain_repo = OracleAuditChainRepository()
        self._acl_repo = OracleAclRepository()

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def capabilities(self) -> set[str]:
        return {"core", "oauth", "sessions", "consultations", "federation", "audit", "state", "acl"}

    @property
    def capability_details(self) -> set[str]:
        return set(FULL_STORAGE_CAPABILITY_DETAILS)

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

    async def record_usage_ledger(
        self,
        tx: Transaction,
        record: Any,
    ) -> Any:
        """Record model-token usage (KNEMON MVP step 5 — Oracle backend).

        Mirrors the Postgres recorder. est_cost_usd is computed server-side
        from ``model_registry`` (reasoning tokens fall back to the output
        rate, as Oracle ``model_registry`` has no reasoning-cost column).
        When the model is absent from the registry, log price drift and
        default the cost to 0 so the usage row is still recorded (fail-open
        on price, never lose the usage record). Oracle has no
        ``INSERT ... SELECT ... RETURNING`` so the cost is a scalar subquery
        in ``VALUES`` and id/cost come back via ``RETURNING ... INTO``.
        """
        import oracledb
        from decimal import Decimal

        from mnemos.persistence.base import UsageLedgerResult

        def _is_missing_table(exc: BaseException) -> bool:
            return "ORA-00942" in str(exc)

        def _scalar(var: Any) -> Any:
            value = var.getvalue()
            if isinstance(value, (list, tuple)):
                return value[0] if value else None
            return value

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            auth_method = "api"
            try:
                await _call(
                    cursor.execute,
                    """
                    SELECT auth_method
                    FROM subscription_plans
                    WHERE provider = :provider
                      AND plan_name = :plan_name
                      AND effective_from <= TRUNC(SYSTIMESTAMP)
                      AND (effective_until IS NULL OR effective_until >= TRUNC(SYSTIMESTAMP))
                    """,
                    {"provider": record.provider, "plan_name": record.tier},
                )
                row = await _call(cursor.fetchone)
                if row:
                    auth_method = str(row[0]).lower()
            except Exception as exc:
                if not _is_missing_table(exc):
                    raise

            rid = cursor.var(oracledb.DB_TYPE_NUMBER)
            rcost = cursor.var(oracledb.DB_TYPE_NUMBER)
            params = {
                "provider": record.provider,
                "model": record.model,
                "task_kind": record.task_kind,
                "tokens_in": record.tokens_in,
                "tokens_out": record.tokens_out,
                "tokens_reasoning": record.tokens_reasoning,
                "latency_ms": record.latency_ms,
                "outcome": record.outcome,
                "caller_subsystem": record.caller_subsystem,
                "tier": record.tier,
                "session_id": record.session_id,
                "request_count": record.request_count,
                "plan_window_id": record.plan_window_id,
                "path_kind": record.path_kind or "api",
                "subscription_amortized": 1 if auth_method == "subscription" else 0,
                "rid": rid,
                "rcost": rcost,
            }
            if auth_method == "subscription":
                await _call(
                    cursor.execute,
                    """
                INSERT INTO usage_ledger (
                    provider, model, task_kind, tokens_in, tokens_out,
                    tokens_reasoning, est_cost_usd, latency_ms, outcome,
                    caller_subsystem, tier, session_id, request_count,
                    plan_window_id, path_kind, subscription_amortized
                )
                VALUES (
                    :provider, :model, :task_kind, :tokens_in, :tokens_out,
                    :tokens_reasoning, 0, :latency_ms, :outcome,
                    :caller_subsystem, :tier, :session_id, :request_count,
                    :plan_window_id, :path_kind, :subscription_amortized
                )
                RETURNING id, est_cost_usd INTO :rid, :rcost
                """,
                    params,
                )
            else:
                try:
                    await _call(
                        cursor.execute,
                        """
                    INSERT INTO usage_ledger (
                        provider, model, task_kind, tokens_in, tokens_out,
                        tokens_reasoning, est_cost_usd, latency_ms, outcome,
                        caller_subsystem, tier, session_id, request_count,
                        plan_window_id, path_kind, subscription_amortized
                    )
                    VALUES (
                        :provider, :model, :task_kind, :tokens_in, :tokens_out,
                        :tokens_reasoning,
                        NVL((
                            SELECT (:tokens_in * NVL(input_cost_per_mtok, 0)
                                  + :tokens_out * NVL(output_cost_per_mtok, 0)
                                  + :tokens_reasoning * NVL(output_cost_per_mtok, 0))
                                  / 1000000
                            FROM model_registry
                            WHERE provider = :provider AND model_id = :model
                        ), 0),
                        :latency_ms, :outcome, :caller_subsystem, :tier,
                        :session_id, :request_count, :plan_window_id,
                        :path_kind, :subscription_amortized
                    )
                    RETURNING id, est_cost_usd INTO :rid, :rcost
                    """,
                        params,
                    )
                except Exception as exc:
                    if not _is_missing_table(exc):
                        raise
                    _LOG.warning(
                        "usage_ledger model_registry table missing for provider=%s model=%s; recording est_cost_usd=0",
                        record.provider,
                        record.model,
                    )
                    await _call(
                        cursor.execute,
                        """
                    INSERT INTO usage_ledger (
                        provider, model, task_kind, tokens_in, tokens_out,
                        tokens_reasoning, est_cost_usd, latency_ms, outcome,
                        caller_subsystem, tier, session_id, request_count,
                        plan_window_id, path_kind, subscription_amortized
                    )
                    VALUES (
                        :provider, :model, :task_kind, :tokens_in, :tokens_out,
                        :tokens_reasoning, 0, :latency_ms, :outcome,
                        :caller_subsystem, :tier, :session_id, :request_count,
                        :plan_window_id, :path_kind, :subscription_amortized
                    )
                    RETURNING id, est_cost_usd INTO :rid, :rcost
                    """,
                        params,
                    )
            if auth_method != "subscription" and _scalar(rcost) == 0:
                _LOG.warning(
                    "usage_ledger model_registry price missing for provider=%s model=%s; recording est_cost_usd=0",
                    record.provider,
                    record.model,
                )
        finally:
            await _call(cursor.close)

        new_id = _scalar(rid)
        cost = _scalar(rcost)
        return UsageLedgerResult(
            id=int(new_id),
            est_cost_usd=Decimal(str(cost)) if cost is not None else Decimal("0"),
        )

    async def register_oauth_token(
        self,
        tx: Transaction,
        *,
        token: str | bytes,
        user_id: str,
        provider: str,
        scopes: Sequence[str] | dict[str, Any] | None = None,
        expires_at: Any = None,
        refresh_token: str | None = None,
    ) -> Row:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO oauth_tokens
                  (token, user_id, provider, scopes, expires_at, refresh_token, created_at, last_used_at)
                VALUES (:token, :user_id, :provider, :scopes, :expires_at, :refresh_token, SYSTIMESTAMP, NULL)
                """,
                {
                    "token": _raw_token(token),
                    "user_id": user_id,
                    "provider": provider,
                    "scopes": _json_text(scopes, []),
                    "expires_at": _ts_for_oracle(expires_at),
                    "refresh_token": refresh_token,
                },
            )
        finally:
            await _call(cursor.close)
        row = await self.lookup_oauth_token(tx, token=token, touch=False)
        assert row is not None
        return row

    async def lookup_oauth_token(self, tx: Transaction, *, token: str | bytes, touch: bool = True) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        raw = _raw_token(token)
        try:
            if touch:
                await _call(
                    cursor.execute,
                    "UPDATE oauth_tokens SET last_used_at = SYSTIMESTAMP WHERE token = :token",
                    {"token": raw},
                )
            await _call(
                cursor.execute,
                """
                SELECT token, user_id, provider, scopes, expires_at, refresh_token, created_at, last_used_at
                FROM oauth_tokens
                WHERE token = :token
                """,
                {"token": raw},
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            return self._normalize_oauth_token_row(row)
        finally:
            await _call(cursor.close)

    async def revoke_oauth_token(self, tx: Transaction, *, token: str | bytes) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(cursor.execute, "DELETE FROM oauth_tokens WHERE token = :token", {"token": _raw_token(token)})
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def start_oauth_flow(
        self,
        tx: Transaction,
        *,
        state: str | bytes,
        provider: str,
        csrf_token: str,
        return_url: str | None,
        expires_at: Any,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO oauth_state (state, provider, csrf_token, return_url, created_at, expires_at)
                VALUES (:state, :provider, :csrf_token, :return_url, SYSTIMESTAMP, :expires_at)
                """,
                {
                    "state": _raw_token(state),
                    "provider": provider,
                    "csrf_token": csrf_token,
                    "return_url": return_url,
                    "expires_at": _ts_for_oracle(expires_at),
                },
            )
        finally:
            await _call(cursor.close)
        row = await self._fetch_oauth_state(tx, state=state)
        assert row is not None
        return row

    async def redeem_oauth_state(self, tx: Transaction, *, state: str | bytes) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        raw = _raw_token(state)
        try:
            await _call(
                cursor.execute,
                """
                SELECT state, provider, csrf_token, return_url, created_at, expires_at
                FROM oauth_state
                WHERE state = :state AND expires_at > SYSTIMESTAMP
                """,
                {"state": raw},
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            await _call(cursor.execute, "DELETE FROM oauth_state WHERE state = :state", {"state": raw})
            return self._normalize_oauth_state_row(row)
        finally:
            await _call(cursor.close)

    async def create_session(
        self,
        tx: Transaction,
        *,
        session_id: str | bytes | uuid.UUID | None = None,
        user_id: str,
        expires_at: Any,
        metadata: dict[str, Any] | None = None,
    ) -> Row:
        sid = session_id or uuid.uuid4()
        sid_raw = _uuid_to_raw(sid)
        assert sid_raw is not None
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO sessions (id, session_id, user_id, started_at, last_active_at, expires_at, metadata)
                VALUES (:id, :session_id, :user_id, SYSTIMESTAMP, SYSTIMESTAMP, :expires_at, :metadata)
                """,
                {
                    "id": str(uuid.UUID(bytes=sid_raw)),
                    "session_id": sid_raw,
                    "user_id": user_id,
                    "expires_at": _ts_for_oracle(expires_at),
                    "metadata": _json_text(metadata, {}),
                },
            )
        finally:
            await _call(cursor.close)
        row = await self.lookup_session(tx, session_id=sid)
        assert row is not None
        return row

    async def lookup_session(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT session_id, user_id, started_at, last_active_at, expires_at, metadata
                FROM sessions
                WHERE session_id = :session_id AND expires_at > SYSTIMESTAMP
                """,
                {"session_id": _uuid_to_raw(session_id)},
            )
            return self._normalize_session_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
        finally:
            await _call(cursor.close)

    async def update_session_active(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE sessions SET last_active_at = SYSTIMESTAMP WHERE session_id = :session_id",
                {"session_id": _uuid_to_raw(session_id)},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def expire_session(self, tx: Transaction, *, session_id: str | bytes | uuid.UUID) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE sessions SET expires_at = SYSTIMESTAMP WHERE session_id = :session_id",
                {"session_id": _uuid_to_raw(session_id)},
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def log_session_event(
        self,
        tx: Transaction,
        *,
        session_id: str | bytes | uuid.UUID,
        event_kind: str,
        payload: dict[str, Any] | None = None,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            rid = cursor.var(int)
            await _call(
                cursor.execute,
                """
                INSERT INTO session_logs (session_id, event_kind, payload, ts)
                VALUES (:session_id, :event_kind, :payload, SYSTIMESTAMP)
                RETURNING id INTO :id
                """,
                {
                    "session_id": _uuid_to_raw(session_id),
                    "event_kind": event_kind,
                    "payload": _json_text(payload, {}),
                    "id": rid,
                },
            )
            new_id = rid.getvalue()
            if isinstance(new_id, (list, tuple)):
                new_id = new_id[0]
            await _call(
                cursor.execute,
                "SELECT id, session_id, event_kind, payload, ts FROM session_logs WHERE id = :id",
                {"id": new_id},
            )
            return self._normalize_session_log_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))  # type: ignore[return-value]
        finally:
            await _call(cursor.close)

    async def create_consultation(
        self,
        tx: Transaction,
        *,
        consultation_id: str | bytes | uuid.UUID | None = None,
        user_id: str,
        prompt: str,
        task_type: str | None,
        mode: str | None,
        status: str = "pending",
    ) -> Row:
        cid = consultation_id or uuid.uuid4()
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO consultations (id, user_id, prompt, task_type, "mode", status, created_at, completed_at)
                VALUES (:id, :user_id, :prompt, :task_type, :mode_val, :status, SYSTIMESTAMP, NULL)
                """,
                {
                    "id": _uuid_to_raw(cid),
                    "user_id": user_id,
                    "prompt": prompt,
                    "task_type": task_type,
                    "mode_val": mode,
                    "status": status,
                },
            )
        finally:
            await _call(cursor.close)
        row = await self.fetch_consultation(tx, consultation_id=cid)
        assert row is not None
        return row

    async def append_consultation_response(
        self,
        tx: Transaction,
        *,
        consultation_id: str | bytes | uuid.UUID,
        provider: str,
        model_id: str,
        response: str,
        final_score: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            rid = cursor.var(int)
            await _call(
                cursor.execute,
                """
                INSERT INTO consultation_responses
                  (consultation_id, provider, model_id, response, final_score,
                   tokens_in, tokens_out, latency_ms, created_at)
                VALUES (:consultation_id, :provider, :model_id, :response, :final_score,
                        :tokens_in, :tokens_out, :latency_ms, SYSTIMESTAMP)
                RETURNING id INTO :id
                """,
                {
                    "consultation_id": _uuid_to_raw(consultation_id),
                    "provider": provider,
                    "model_id": model_id,
                    "response": response,
                    "final_score": final_score,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "latency_ms": latency_ms,
                    "id": rid,
                },
            )
            new_id = rid.getvalue()
            if isinstance(new_id, (list, tuple)):
                new_id = new_id[0]
            await _call(cursor.execute, "SELECT * FROM consultation_responses WHERE id = :id", {"id": new_id})
            return self._normalize_consultation_response_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))  # type: ignore[return-value]
        finally:
            await _call(cursor.close)

    async def fetch_consultation(self, tx: Transaction, *, consultation_id: str | bytes | uuid.UUID) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, user_id, prompt, task_type, "mode", status, created_at, completed_at
                FROM consultations
                WHERE id = :id
                """,
                {"id": _uuid_to_raw(consultation_id)},
            )
            row = self._normalize_consultation_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
            if row is None:
                return None
            row["responses"] = await self._fetch_consultation_responses(tx, consultation_id=consultation_id)
            return row
        finally:
            await _call(cursor.close)

    async def list_consultations(
        self,
        tx: Transaction,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        where: list[str] = []
        params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if user_id is not None:
            where.append("user_id = :user_id")
            params["user_id"] = user_id
        if status is not None:
            where.append("status = :status")
            params["status"] = status
        sql = 'SELECT id, user_id, prompt, task_type, "mode", status, created_at, completed_at FROM consultations'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(cursor.execute, sql, params)
            return [self._normalize_consultation_row(row) for row in await _fetch_all_dicts(cursor)]  # type: ignore[list-item]
        finally:
            await _call(cursor.close)

    async def _fetch_oauth_state(self, tx: Transaction, *, state: str | bytes) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT state, provider, csrf_token, return_url, created_at, expires_at FROM oauth_state WHERE state = :state",
                {"state": _raw_token(state)},
            )
            return self._normalize_oauth_state_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
        finally:
            await _call(cursor.close)

    async def _fetch_consultation_responses(
        self, tx: Transaction, *, consultation_id: str | bytes | uuid.UUID
    ) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, consultation_id, provider, model_id, response, final_score,
                       tokens_in, tokens_out, latency_ms, created_at
                FROM consultation_responses
                WHERE consultation_id = :consultation_id
                ORDER BY id
                """,
                {"consultation_id": _uuid_to_raw(consultation_id)},
            )
            return [self._normalize_consultation_response_row(row) for row in await _fetch_all_dicts(cursor)]  # type: ignore[list-item]
        finally:
            await _call(cursor.close)

    @staticmethod
    def _normalize_oauth_token_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["token"] = _raw_token_text(out.get("token"))
        out["scopes"] = _json_value(out.get("scopes"), [])
        return out

    @staticmethod
    def _normalize_oauth_state_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["state"] = _raw_token_text(out.get("state"))
        return out

    @staticmethod
    def _normalize_session_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["session_id"] = _raw_to_uuid(out.get("session_id"))
        out["metadata"] = _json_value(out.get("metadata"), {})
        return out

    @staticmethod
    def _normalize_session_log_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["session_id"] = _raw_to_uuid(out.get("session_id"))
        out["payload"] = _json_value(out.get("payload"), {})
        return out

    @staticmethod
    def _normalize_consultation_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["id"] = _raw_to_uuid(out.get("id"))
        return out

    @staticmethod
    def _normalize_consultation_response_row(row: Row | None) -> Row | None:
        if row is None:
            return None
        out = dict(row)
        out["consultation_id"] = _raw_to_uuid(out.get("consultation_id"))
        return out

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
    def compression_queue(self) -> CompressionQueueRepository:
        return self._compression_queue_repo

    @property
    def webhooks(self) -> WebhookRepository:
        return self._webhooks_repo

    @property
    def consultations_audit(self) -> ConsultationAuditRepository:
        return self._consultations_audit_repo

    @property
    def oauth(self) -> OAuthRepository:
        return self._oauth_repo

    @property
    def sessions(self) -> SessionsRepository:
        return self._sessions_repo

    @property
    def consultations(self) -> ConsultationsRepository:
        return self._consultations_repo

    @property
    def federation(self) -> FederationRepository:
        return self._federation_repo

    @property
    def state_kv(self) -> StateRepository:
        return self._state_kv_repo

    @property
    def audit_chain(self) -> AuditChainRepository:
        return self._audit_chain_repo

    @property
    def acl(self) -> AclRepository:
        return self._acl_repo

    async def ping(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                cursor = await _call(conn.cursor)
                try:
                    await _call(cursor.execute, "SELECT 1 FROM DUAL")
                    await _call(cursor.fetchone)
                finally:
                    await _call(cursor.close)
            return True
        except Exception:
            return False

    async def fetch_category_decay_rows(self, tx: Transaction) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT category, half_life_days, decay_kind, floor FROM memory_category_decay",
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def upsert_category_decay(
        self,
        tx: Transaction,
        *,
        category: str,
        half_life_days: float,
        decay_kind: str,
        floor: float,
    ) -> None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO memory_category_decay tgt
                USING (SELECT :category AS category FROM dual) src
                ON (tgt.category = src.category)
                WHEN MATCHED THEN UPDATE SET
                    half_life_days = :half_life_days,
                    decay_kind = :decay_kind,
                    floor = :floor
                WHEN NOT MATCHED THEN
                    INSERT (category, half_life_days, decay_kind, floor)
                    VALUES (:category, :half_life_days, :decay_kind, :floor)
                """,
                {
                    "category": category,
                    "half_life_days": half_life_days,
                    "decay_kind": decay_kind,
                    "floor": floor,
                },
            )
        finally:
            await _call(cursor.close)

    async def create_journal_entry(self, tx: Transaction, **kwargs: Any) -> Row:
        raise NotImplementedError("journal API persistence is not implemented for Oracle schema 0015")

    async def list_journal_entries(self, tx: Transaction, **kwargs: Any) -> list[Row]:
        raise NotImplementedError("journal API persistence is not implemented for Oracle schema 0015")

    async def delete_journal_entry(self, tx: Transaction, **kwargs: Any) -> bool:
        raise NotImplementedError("journal API persistence is not implemented for Oracle schema 0015")

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
                "OracleBackend.open probe failed (%s); backend remains open but first acquire() may also fail.",
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
    "OracleCompressionQueueRepository",
    "OracleConsultationAuditRepository",
    "OracleFederationRepository",
    "OracleKGRepository",
    "OracleMemoryRepository",
    "OracleStateRepository",
    "OracleVersionRepository",
    "OracleWebhookRepository",
    "create_oracle_pool",
    "_validate_and_format_vector",
]
