"""IBM Db2 12.1.x persistence backend for MNEMOS.

Piggybacks on the Oracle backend's SQL via Db2's Oracle Compatibility
Mode (``DB2_COMPATIBILITY_VECTOR=ORA`` or container env
``ENABLE_ORACLE_COMPATIBILITY=true``).

The ``_Db2AsyncConnectionPool`` wraps the sync ``ibm_db_dbi`` driver
with ``asyncio.to_thread`` so the connection surface matches the
async ``oracledb`` pool shape consumed by ``OracleBackend``. The
``ibm_db_dbi`` import is deferred to first ``_open()`` so this module
imports cleanly on hosts without the driver installed.

References:
- IBM Db2 12.1.x VECTOR docs: https://www.ibm.com/docs/en/db2/12.1.x?topic=list-vector-values
- VECTOR_DISTANCE function: https://www.ibm.com/docs/en/db2/12.1.x?topic=functions-vector-distance
- Oracle compat mode: https://www.ibm.com/docs/en/db2/12.1?topic=compatibility-oracle-application-development
- DiskANN vector index: https://www.ibm.com/docs/en/db2/12.1?topic=indexes-vector
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from functools import lru_cache
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.core.config import db2_text_search_override, db2_vector_index_override
from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.persistence.oracle import (
    OracleAclRepository,
    OracleAuditChainRepository,
    OracleBackend,
    OracleBranchRepository,
    OracleCompressionRepository,
    OracleConsultationAuditRepository,
    OracleConsultationsRepository,
    OracleFederationRepository,
    OracleKGRepository,
    OracleMemoryRepository,
    OracleOAuthRepository,
    OracleSessionsRepository,
    OracleStateRepository,
    OracleVersionRepository,
    OracleWebhookRepository,
    _call,
    _conn_from_tx,
    _content_hash,
    _fetch_all_dicts,
    _json_list_text,
    _is_unique_violation,
    _json_text,
    _json_value,
    _raw_to_uuid,
    _raw_token,
    _raw_token_text,
    _render_visibility,
    _row_to_dict,
    _ts_for_oracle,
    _uuid_to_raw,
    _validate_and_format_vector,
)
from mnemos.persistence.schema import ensure_db2_schema
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter

_LOG = logging.getLogger(__name__)


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


# ────────────────────────────────────────────────────────────────────────────
# Oracle → Db2 SQL translation helpers
# ────────────────────────────────────────────────────────────────────────────

# Tokens that must be rewritten for Db2 even though ORA-compat is enabled.
# Db2 12.1.x ORA-compat covers VARCHAR2, NUMBER, NVL, dual, and basic Oracle
# syntax — but NOT TIMESTAMP WITH TIME ZONE casts, NOT the SYSTIMESTAMP
# pseudocolumn, and NOT Oracle's ``:name`` named-bind style.
_ORA_TO_DB2_PAIRS: tuple[tuple[str, str], ...] = (
    ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP"),
    ("SYSTIMESTAMP", "CURRENT TIMESTAMP"),
    ("SYSDATE", "CURRENT DATE"),
)

# Word-boundary regex for TO_VECTOR (A3). Must be applied *after* masking
# so that it only matches the keyword, never inside literals or comments.
_TO_VECTOR_RE = re.compile(r"\bTO_VECTOR\b")
_BIND_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
_VECTOR_CALL_RE = re.compile(r"VECTOR\(\?\)")
_NATIVE_FORBIDDEN_ORACLE_TOKENS: tuple[tuple[str, str], ...] = (
    ("SYSTIMESTAMP", "Oracle SYSTIMESTAMP token"),
    ("SYSDATE", "Oracle SYSDATE token"),
    ("FROM DUAL", "Oracle DUAL table"),
    ("TO_VECTOR(", "Oracle TO_VECTOR call"),
)
# Db2's ``NVL(?, 'literal')`` infers the result column type from the
# literal width (e.g. VARCHAR(7) from 'default'), then truncates any
# longer bound value with CLI0109E SQLSTATE=22001. Widen the literal
# side with an explicit cast so the type unifier picks VARCHAR(4000).
_NVL_LITERAL_RE = re.compile(r"NVL\(\s*(:[a-zA-Z_][a-zA-Z0-9_]*)\s*,\s*'([^']*)'\s*\)")


def _mask_sql_literals_and_comments(
    sql: str,
) -> tuple[str, dict[str, str]]:
    """Mask literals, identifiers, and comments to protect them from rewrites.

    Returns (masked_sql, restore_map) where restore_map maps placeholder
    ``\x00N\x00`` back to original content.

    This addresses A1 (tokens inside strings/comments), A2 (false-positive
    :name binds inside literals), and A3 (TO_VECTOR substring in identifiers).

    **Safety guarantees:**
    - Single-quoted strings ('...') and double-quoted identifiers ("...")
      are fully protected (including embedded :name or SYSTIMESTAMP).
    - -- comments to end-of-line and /* ... */ block comments are protected.
    - Masking is done with NUL-delimited numeric placeholders that cannot
      appear in valid SQL, guaranteeing no false-positive matches during
      subsequent regex/substitution passes.
    - All transformations run on the masked string; unmask is the final step.
    - Existing behavior for normal SQL (no literals/comments with keywords)
      is 100% preserved.
    - Dim detection on bound parameters (not in SQL text) is untouched.
    """
    restore_map: dict[str, str] = {}
    counter = 0

    def replacer(match: re.Match[str]) -> str:
        nonlocal counter
        content = match.group(0)
        placeholder = f"\x00{counter}\x00"
        restore_map[placeholder] = content
        counter += 1
        return placeholder

    # 1. Mask block comments /* ... */
    sql = re.sub(r"/\*[\s\S]*?\*/", replacer, sql)

    # 2. Mask line comments -- ... (to end of line or \n)
    # Use non-greedy match that stops at newline or end, but preserve the newline
    sql = re.sub(r"--.*?(?=\n|$)", replacer, sql)

    # 3. Mask single-quoted string literals (handles '' escaping)
    sql = re.sub(
        r"'(?:''|[^'])*'",
        replacer,
        sql,
    )

    # 4. Mask double-quoted identifiers (standard SQL)
    sql = re.sub(
        r'"(?:[^"]|"")*"',
        replacer,
        sql,
    )

    return sql, restore_map


def _unmask_sql(masked_sql: str, restore_map: dict[str, str]) -> str:
    """Restore masked literals/comments using the map from
    :func:`_mask_sql_literals_and_comments`.

    Iteration order is irrelevant (Opus review O13). Placeholders are
    ``\x00<digits>\x00`` with NUL byte delimiters at both ends — no
    placeholder can substring-match another because the NUL bytes form
    unique boundaries. The prior reverse-sort was cargo cult; flat
    iteration is correct.
    """
    result = masked_sql
    for ph, content in restore_map.items():
        result = result.replace(ph, content)
    return result


@lru_cache(maxsize=512)
def _translate_sql_cached(sql: str) -> tuple[str, tuple[str, ...]]:
    """Pure SQL→SQL translation, cached on SQL string identity (O11).

    Returns ``(template_sql, bind_name_order)`` where ``template_sql``
    still contains the unexpanded ``VECTOR(?)`` placeholder (dim is
    per-call). MNEMOS Oracle repo SQL strings are module-level
    constants so identity caching is highly effective — same string
    passed millions of times across a long-running process.

    All A1/A2/A3 mask invariants from the unmemoized implementation
    are preserved:
    1. NVL widening on raw SQL before masking
    2. Mask literals + comments to NUL-delimited placeholders
    3. ORA_TO_DB2_PAIRS keyword swaps on masked SQL
    4. TO_VECTOR word-boundary regex on masked SQL
    5. Extract bind name order, substitute `:name` → `?` on masked SQL
    6. Unmask to get final template
    """
    pre_masked = _NVL_LITERAL_RE.sub(r"NVL(CAST(\1 AS VARCHAR(4000)), '\2')", sql)
    masked_sql, restore_map = _mask_sql_literals_and_comments(pre_masked)
    adapted = masked_sql
    for oracle_tok, db2_tok in _ORA_TO_DB2_PAIRS:
        adapted = adapted.replace(oracle_tok, db2_tok)
    adapted = _TO_VECTOR_RE.sub("VECTOR", adapted)
    names = tuple(_BIND_RE.findall(adapted))
    adapted = _BIND_RE.sub("?", adapted)
    return _unmask_sql(adapted, restore_map), names


def _adapt_oracle_to_db2(sql: str, params: dict | tuple | None) -> tuple[str, tuple]:
    """Rewrite Oracle-flavoured SQL + params to Db2-compatible forms.

    Wraps :func:`_translate_sql_cached` (which is pure on ``sql``) and
    appends per-call bind ordering + VECTOR dimension expansion.

    The NVL-before-mask invariant is preserved inside the cached
    helper. See O11 (Opus review) for caching rationale and A1/A2/A3
    for the mask architecture.
    """
    template, names = _translate_sql_cached(sql)

    if params is None:
        # NB: template still contains VECTOR(?) placeholder if any.
        # Without positional binds we leave it as-is — caller must
        # not have a TO_VECTOR call in a no-bind SQL form.
        return template, ()

    if isinstance(params, dict):
        positional = tuple(params[name] for name in names)
    else:
        positional = tuple(params)

    dim = 3
    for p in positional:
        if isinstance(p, str) and p.startswith("["):
            dim = p.count(",") + 1
            break

    final_sql = _VECTOR_CALL_RE.sub(f"VECTOR(?, {dim}, FLOAT32)", template)
    return final_sql, positional


# ────────────────────────────────────────────────────────────────────────────
# Driver wrapper — bridges sync ibm_db to the async pool shape oracledb gives
# ────────────────────────────────────────────────────────────────────────────


class _Db2AsyncCursor:
    """Async wrapper over ``ibm_db_dbi`` cursor — translates Oracle SQL to Db2.

    The ``execute`` method transparently rewrites ``TIMESTAMP WITH TIME ZONE``,
    ``SYSTIMESTAMP``, ``SYSDATE``, and converts ``:name`` named binds to ``?``
    positional binds so that the complete :class:`OracleMemoryRepository`,
    :class:`OracleStateRepository`, and other oracle.py code works against Db2
    without per-method forks.
    """

    def __init__(self, sync_cursor: Any):
        self._cur = sync_cursor
        self.description = None
        self.rowcount = -1

    async def execute(self, sql: str, params: dict | tuple | None = None) -> None:
        adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

        def _go():
            if adapted_params:
                self._cur.execute(adapted_sql, adapted_params)
            else:
                self._cur.execute(adapted_sql)
            self.description = self._cur.description
            self.rowcount = self._cur.rowcount

        await asyncio.to_thread(_go)

    async def fetchone(self) -> tuple | None:
        return await asyncio.to_thread(self._cur.fetchone)

    async def fetchall(self) -> list[tuple]:
        return await asyncio.to_thread(self._cur.fetchall)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        if not params_seq:
            return
        adapted_sql, _ = _adapt_oracle_to_db2(sql, params_seq[0])

        def _go() -> None:
            self._cur.executemany(adapted_sql, params_seq)
            self.rowcount = self._cur.rowcount

        await asyncio.to_thread(_go)

    async def close(self) -> None:
        await asyncio.to_thread(self._cur.close)


class _Db2AsyncConnection:
    """Async-flavoured ibm_db_dbi connection."""

    def __init__(self, sync_conn: Any):
        self._conn = sync_conn

    def cursor(self) -> _Db2AsyncCursor:
        return _Db2AsyncCursor(self._conn.cursor())

    async def commit(self) -> None:
        await asyncio.to_thread(self._conn.commit)

    async def rollback(self) -> None:
        await asyncio.to_thread(self._conn.rollback)

    async def close(self) -> None:
        await asyncio.to_thread(self._conn.close)


class _Db2AsyncConnectionPool:
    """Bounded pool of ``_Db2AsyncConnection`` wrappers over ``ibm_db_dbi``.

    Design notes (post-2026-05-20 Opus review O4/O5/O9/O19):

    * **Slot reservation, not lock-during-open (O4).** The pool reserves
      an "in-use slot" under ``_lock``, opens the physical connection
      OUTSIDE the lock, then re-acquires the lock to register the conn.
      Other coroutines releasing or acquiring during the slow connect
      are not blocked.
    * **Wait-don't-raise on exhaustion (O5).** When ``max_size`` is hit
      and no idle conn is available, callers wait on
      ``_not_full`` (asyncio.Condition) with ``acquire_timeout``.
      Old behaviour was an instant ``RuntimeError`` — burst-load failures
      that would have succeeded with a 100ms wait.
    * **Real warmup (O9).** ``min_size`` connections are pre-opened by
      ``warmup()`` — invoked from :func:`create_db2_pool`. Prior version
      stored ``_min_size`` but never read it.
    * **Diagnostic exhaustion error (O19).** Timeout message includes
      in_use / idle / max counters so operators see saturation state.
    """

    def __init__(
        self,
        dsn_kwargs: dict,
        *,
        min_size: int = 1,
        max_size: int = 8,
        acquire_timeout: float = 30.0,
    ):
        self._dsn_kwargs = dsn_kwargs
        self._min_size = max(0, min_size)
        self._max_size = max(1, max_size)
        self._acquire_timeout = acquire_timeout
        self._idle: list[_Db2AsyncConnection] = []
        self._in_use: set[_Db2AsyncConnection] = set()
        self._reserved: int = 0  # slots reserved but not yet opened
        self._lock = asyncio.Lock()
        self._not_full = asyncio.Condition(self._lock)
        self._closed = False

    async def _open(self) -> _Db2AsyncConnection:
        import ibm_db_dbi  # type: ignore

        # Reject characters that could inject DSN attributes (O10). The DSN
        # is built by ``;`` join + ``key=value`` segments, so a literal
        # ``;`` or ``=`` in UID/PWD/DATABASE could synthesize an
        # arbitrary CLI attribute (AUTHENTICATION=, etc).
        for k, v in self._dsn_kwargs.items():
            if not isinstance(v, str):
                continue
            if ";" in v or "=" in v and k != "PORT":
                raise ValueError(
                    f"DSN attribute {k} contains forbidden char (; or =); would allow CLI attribute injection."
                )
        dsn_string = ";".join(f"{k}={v}" for k, v in self._dsn_kwargs.items()) + ";"
        raw = await asyncio.to_thread(ibm_db_dbi.connect, dsn_string, "", "")
        return _Db2AsyncConnection(raw)

    async def warmup(self) -> None:
        """Pre-open ``min_size`` connections so first ``min_size`` acquires
        avoid full TCP+auth handshake latency. Errors are non-fatal —
        if Db2 is briefly unreachable during startup, acquire() will
        retry the open lazily.
        """
        opened: list[_Db2AsyncConnection] = []
        for _ in range(self._min_size):
            try:
                opened.append(await self._open())
            except Exception:
                break
        async with self._lock:
            self._idle.extend(opened)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Db2AsyncConnection]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._acquire_timeout
        conn: _Db2AsyncConnection | None = None
        need_open = False

        # Reserve a slot under the lock; open OUTSIDE the lock (O4).
        async with self._lock:
            while True:
                if self._closed:
                    raise RuntimeError("DB2 pool is closed")
                if self._idle:
                    conn = self._idle.pop()
                    self._in_use.add(conn)
                    break
                if len(self._in_use) + self._reserved < self._max_size:
                    self._reserved += 1
                    need_open = True
                    break
                # Pool full — wait for a release (O5).
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"DB2 pool acquire timeout after {self._acquire_timeout:.1f}s "
                        f"(in_use={len(self._in_use)} idle={len(self._idle)} "
                        f"reserved={self._reserved} max_size={self._max_size})"
                    )
                try:
                    await asyncio.wait_for(self._not_full.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"DB2 pool acquire timeout after {self._acquire_timeout:.1f}s "
                        f"(in_use={len(self._in_use)} idle={len(self._idle)} "
                        f"reserved={self._reserved} max_size={self._max_size})"
                    )

        # Heavy connect happens OUTSIDE the lock; readers/releasers progress freely.
        if need_open:
            try:
                conn = await self._open()
            except Exception:
                async with self._lock:
                    self._reserved -= 1
                    self._not_full.notify()
                raise
            async with self._lock:
                self._reserved -= 1
                self._in_use.add(conn)

        try:
            yield conn
        finally:
            async with self._lock:
                self._in_use.discard(conn)
                if self._closed:
                    await conn.close()
                else:
                    self._idle.append(conn)
                self._not_full.notify()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._not_full.notify_all()
            while self._idle:
                await self._idle.pop().close()


# ────────────────────────────────────────────────────────────────────────────
# Native-cursor pass-through (no Oracle → Db2 translation)
# ────────────────────────────────────────────────────────────────────────────


class _Db2NativeAsyncCursor:
    """Async wrapper over ``ibm_db_dbi`` cursor — NO Oracle token translation.

    This cursor passes SQL and params through to the sync cursor verbatim.
    Use when every repository method emits Db2-native SQL (``?`` positional
    binds, ``CURRENT TIMESTAMP``, ``FROM SYSIBM.SYSDUMMY1``, etc.).

    Defensive guard: if the incoming SQL contains Oracle-style ``:name``
    named binds or Oracle-only tokens the compat cursor would normally
    translate, a ``RuntimeError`` is raised to fail fast rather than silently
    sending the wrong dialect to Db2.
    """

    def __init__(self, sync_cursor: Any):
        self._cur = sync_cursor
        self.description = None
        self.rowcount = -1

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        masked_sql, _ = _mask_sql_literals_and_comments(sql)
        guard_reason = None
        if _BIND_RE.search(masked_sql):
            guard_reason = "Oracle :name bind"
        else:
            upper_sql = masked_sql.upper()
            for token, label in _NATIVE_FORBIDDEN_ORACLE_TOKENS:
                if token in upper_sql:
                    guard_reason = label
                    break
        if guard_reason is not None:
            raise RuntimeError(
                f"native cursor received {guard_reason} — "
                "the native path expects Db2-native SQL only. "
                "Switch back to Db2Backend (compat) or rewrite the "
                "caller to emit ? binds, CURRENT TIMESTAMP/DATE, "
                "SYSIBM.SYSDUMMY1, and VECTOR(...)."
            )

        def _go():
            if params:
                self._cur.execute(sql, params)
            else:
                self._cur.execute(sql)
            self.description = self._cur.description
            self.rowcount = self._cur.rowcount

        await asyncio.to_thread(_go)

    async def fetchone(self) -> tuple | None:
        return await asyncio.to_thread(self._cur.fetchone)

    async def fetchall(self) -> list[tuple]:
        return await asyncio.to_thread(self._cur.fetchall)

    async def close(self) -> None:
        await asyncio.to_thread(self._cur.close)


class _Db2NativeAsyncConnection(_Db2AsyncConnection):
    """Async connection wrapper that produces ``_Db2NativeAsyncCursor``
    cursors instead of the translation-layer ``_Db2AsyncCursor``.
    """

    def cursor(self) -> _Db2NativeAsyncCursor:
        return _Db2NativeAsyncCursor(self._conn.cursor())


class _Db2NativeAsyncConnectionPool(_Db2AsyncConnectionPool):
    """Pool that opens ``_Db2NativeAsyncConnection`` wrappers.

    Shares all acquisition/warmup/timeout logic with
    :class:`_Db2AsyncConnectionPool`; only the connection wrapper
    class differs so the pool always yields native cursors.
    """

    async def _open(self) -> _Db2NativeAsyncConnection:
        import ibm_db_dbi  # type: ignore

        for k, v in self._dsn_kwargs.items():
            if not isinstance(v, str):
                continue
            if ";" in v or "=" in v and k != "PORT":
                raise ValueError(
                    f"DSN attribute {k} contains forbidden char (; or =); would allow CLI attribute injection."
                )
        dsn_string = ";".join(f"{k}={v}" for k, v in self._dsn_kwargs.items()) + ";"
        raw = await asyncio.to_thread(ibm_db_dbi.connect, dsn_string, "", "")
        return _Db2NativeAsyncConnection(raw)


async def create_db2_native_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    acquire_timeout: float = 30.0,
) -> _Db2NativeAsyncConnectionPool:
    """Create a DB2 native-cursor async pool from a DSN.

    Mirrors :func:`create_db2_pool` but produces
    :class:`_Db2NativeAsyncConnectionPool` so every acquired connection
    yields :class:`_Db2NativeAsyncCursor` (no Oracle→Db2 translation).
    """
    kwargs = _parse_db2_dsn(dsn)
    pool = _Db2NativeAsyncConnectionPool(
        kwargs,
        min_size=min_size,
        max_size=max_size,
        acquire_timeout=acquire_timeout,
    )
    await pool.warmup()
    return pool


# Forbidden chars per O10 — used by _parse_db2_dsn validation.
_DSN_FORBIDDEN_CHARS = (";", "=")


def _parse_db2_dsn(dsn: str) -> dict[str, str]:
    """``db2://user:pass@host:port/database`` → ibm_db_dbi kwargs.

    URL-decoded UID/PWD/DATABASE values are scrubbed for the chars that
    could synthesize extra CLI attributes through the ``key=value;``
    DSN format (O10). Validation also runs at ``_open()`` time as a
    defence-in-depth check for callers that construct the kwargs dict
    directly.
    """
    if "://" not in dsn:
        return {"DSN": dsn}
    _, rest = dsn.split("://", 1)
    parsed = urlparse(f"db2://{rest}")
    return {
        "DATABASE": (parsed.path or "/").lstrip("/") or "MNEMOS",
        "HOSTNAME": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 50000),
        "PROTOCOL": "TCPIP",
        "UID": unquote(parsed.username) if parsed.username else "db2inst1",
        "PWD": unquote(parsed.password) if parsed.password else "",
    }


async def create_db2_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    acquire_timeout: float = 30.0,
) -> _Db2AsyncConnectionPool:
    """Create a DB2 async-flavoured connection pool from a DSN. Calls
    :meth:`_Db2AsyncConnectionPool.warmup` so ``min_size`` connections
    are pre-opened before the pool is returned (O9).
    """
    kwargs = _parse_db2_dsn(dsn)
    pool = _Db2AsyncConnectionPool(
        kwargs,
        min_size=min_size,
        max_size=max_size,
        acquire_timeout=acquire_timeout,
    )
    await pool.warmup()
    return pool


# ────────────────────────────────────────────────────────────────────────────
# Repositories — extend Oracle counterparts with DB2-specific overrides
# ────────────────────────────────────────────────────────────────────────────
#
# The default is to inherit the Oracle SQL verbatim. The cursor-level
# translation in :class:`_Db2AsyncCursor.execute` transparently rewrites
# Oracle tokens to Db2 equivalents at query time — no per-method fork needed
# for the standard CRUD surface.
#
# Class-level constants document the rewrite mapping so that future
# method-specific overrides (e.g. DiskANN index syntax in 12.1.5) can
# cite the semantic name rather than the bare string:
#
#   sets.append(f"updated = {self._SQL_NOW}")
#
# See ``docs/db2-port-handoff.md`` for the original 2/6 → 6/6 analysis
# and ``docs/handoff-opencode-db2-sql-overrides-2026-05-20.md`` for the
# verified per-probe error mapping.


class _Db2OraCompatMixin:
    """Shared constants for Db2 repos that inherit Oracle SQL.

    Each constant maps one Oracle token that Db2 12.1.x ORA-compat does
    NOT translate, so method overrides can cite the Db2 form by name.
    The cursor layer handles these automatically for inherited methods;
    use the constants directly when you write a new override.
    """

    _SQL_NOW: str = "CURRENT TIMESTAMP"  # Oracle: SYSTIMESTAMP
    _SQL_TSTZ_CAST: str = "TIMESTAMP"  # Oracle: TIMESTAMP WITH TIME ZONE
    _SQL_TODAY: str = "CURRENT DATE"  # Oracle: SYSDATE
    # ibm_db_dbi uses ``?`` positional binds, not ``:name`` named binds.
    # When writing a manual override, the SQL can still use ``:name``
    # binds + dict params — the cursor layer in ``_Db2AsyncCursor.execute``
    # rewrites both to the positional form via ``_adapt_oracle_to_db2``.


# ────────────────────────────────────────────────────────────────────────────
# Db2 vector-index mode (DiskANN engagement toggle)
# ────────────────────────────────────────────────────────────────────────────
#
# ``MNEMOS_DB2_VECTOR_INDEX`` (env var; settings.db2_vector_index also
# checked as a soft fallback) selects how Db2 ``semantic_search`` runs:
#
#   - ``approx`` (default): ``FETCH APPROX FIRST :limit ROWS ONLY``
#     with ``VECTOR_DISTANCE(..., EUCLIDEAN)``. Engages the DiskANN
#     vector index created by ``CREATE VECTOR INDEX ... WITH DISTANCE
#     EUCLIDEAN``. Recall@10 vs exact scan is ~0.95+ for normalized
#     embeddings.
#
#   - ``exact``: ``FETCH FIRST :limit ROWS ONLY`` with the same
#     ``EUCLIDEAN`` metric. Exact scan, no index engagement — used
#     for parity / debugging.
#
# The choice of EUCLIDEAN over COSINE is correctness-preserving for
# normalized embeddings: for unit-norm vectors ``a`` and ``b`` we have
# ``|a-b|^2 = 2 - 2 cos(a,b)`` so EUCLIDEAN and COSINE distance produce
# the same top-K ordering. MNEMOS embeddings are L2-normalized by the
# embedder layer, so this preserves recall semantics. If a future
# embedder ships un-normalized vectors, the ``exact`` mode + COSINE
# rewrite (or normalize-on-read) would be the escape hatch.
_DB2_VEC_INDEX_ENV = "MNEMOS_DB2_VECTOR_INDEX"
_DB2_VEC_INDEX_DEFAULT = "approx"
_DB2_VEC_INDEX_VALID = ("approx", "exact")


def _resolve_db2_vector_index_mode(settings: Any) -> str:
    """Return the effective Db2 vector-index mode (``"approx"`` or
    ``"exact"``). Env var ``MNEMOS_DB2_VECTOR_INDEX`` wins over the
    ``settings.db2_vector_index`` attribute; both default to
    ``"approx"`` so a fresh deployment engages the DiskANN index.
    """
    raw = db2_vector_index_override()
    if raw is None and settings is not None:
        raw = getattr(settings, "db2_vector_index", None)
    if raw is None:
        return _DB2_VEC_INDEX_DEFAULT
    val = str(raw).strip().lower()
    if val not in _DB2_VEC_INDEX_VALID:
        _LOG.warning(
            "Invalid %s=%r; expected one of %s. Falling back to %r.",
            _DB2_VEC_INDEX_ENV,
            raw,
            _DB2_VEC_INDEX_VALID,
            _DB2_VEC_INDEX_DEFAULT,
        )
        return _DB2_VEC_INDEX_DEFAULT
    return val


# ── Db2 full-text-search mode (CONTAINS engagement toggle) ────────────────────
# ``like``     (default) — ``UPPER(content) LIKE '%term%'``. Full-table scan,
#                          but works on stock Db2 with no Text Search server.
# ``contains``           — ``CONTAINS(content, ?) = 1``. Engages a Db2 Text
#                          Search index; requires the Db2 Text Search server +
#                          a text index on ``memories.content``. Opt-in only,
#                          because CONTAINS errors (SQL20424N) when no text
#                          index exists, so it must not be the default.
_DB2_TEXT_SEARCH_ENV = "MNEMOS_DB2_TEXT_SEARCH"
_DB2_TEXT_SEARCH_DEFAULT = "like"
_DB2_TEXT_SEARCH_VALID = ("like", "contains")


def _resolve_db2_text_search_mode(settings: Any) -> str:
    """Return the effective Db2 FTS mode (``"like"`` or ``"contains"``).

    Env var ``MNEMOS_DB2_TEXT_SEARCH`` wins over the ``settings.db2_text_search``
    attribute. Defaults to ``"like"`` (safe on stock Db2); ``"contains"`` opts
    into the native Db2 Text Search index.
    """
    raw = db2_text_search_override()
    if raw is None and settings is not None:
        raw = getattr(settings, "db2_text_search", None)
    if raw is None:
        return _DB2_TEXT_SEARCH_DEFAULT
    val = str(raw).strip().lower()
    if val not in _DB2_TEXT_SEARCH_VALID:
        _LOG.warning(
            "Invalid %s=%r; expected one of %s. Falling back to %r.",
            _DB2_TEXT_SEARCH_ENV,
            raw,
            _DB2_TEXT_SEARCH_VALID,
            _DB2_TEXT_SEARCH_DEFAULT,
        )
        return _DB2_TEXT_SEARCH_DEFAULT
    return val


class Db2MemoryRepository(_Db2OraCompatMixin, OracleMemoryRepository):
    """Memory repository — Db2-native overrides for core write/read paths.

    Provides Db2-native SQL for insert, embedding upsert, read, update,
    delete, and semantic-search hot paths while keeping the wider P13
    protocol surface inherited from Oracle repositories and translated
    by the cursor layer.

    ``semantic_search`` is overridden because the inherited Oracle SQL
    uses ``VECTOR_DISTANCE(..., COSINE)`` + ``FETCH FIRST K ROWS ONLY``
    — neither of which engages the Db2 12.1.5 DiskANN vector index.
    The override emits ``VECTOR_DISTANCE(..., EUCLIDEAN)`` +
    ``FETCH APPROX FIRST K ROWS ONLY`` so the vector index created by
    ``mnemos/db_migrations/migrations_db2/0001_core_schema.sql`` is actually used by the
    app path (not only by ``scripts/bench_v4.py``).

    Mode is selected by ``_resolve_db2_vector_index_mode`` — see
    module-level docstring.
    """

    # semantic_search emits ``rank_score`` = VECTOR_DISTANCE(..., EUCLIDEAN)
    # on L2-normalized embeddings. NOT a cosine distance — override the
    # Oracle-inherited cosine_distance metric so the route converts it via
    # cos = 1 - d^2/2 (ngc-review BLOCK finding 2026-06-13).
    SEMANTIC_SCORE_COLUMN = "rank_score"
    SEMANTIC_SCORE_METRIC = "euclidean_unit"

    # Settings reference, populated by ``Db2Backend.__init__`` so the
    # override can read ``settings.db2_vector_index`` without changing
    # the inherited ``OracleMemoryRepository`` constructor signature.
    _settings: Any = None

    async def semantic_search(
        self,
        tx: Any,
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
        mode = _resolve_db2_vector_index_mode(self._settings)
        # FETCH APPROX FIRST engages the DiskANN index; FETCH FIRST is
        # an exact scan. Both query the same VECTOR_DISTANCE function.
        fetch_clause = "FETCH APPROX FIRST ? ROWS ONLY" if mode == "approx" else "FETCH FIRST ? ROWS ONLY"
        # Over-fetch a wider candidate set when recency-boosting so the Python
        # re-rank below can promote a newer memory ranked just outside the
        # top-`limit` by pure vector distance. Mirrors PostgresBackend's
        # candidate_limit; the native FETCH APPROX FIRST DiskANN scan stays
        # engaged on the larger set, and we re-cap to `limit` after re-ranking.
        fetch_count = max(limit, min(limit * 4, 200)) if boost_recency else limit

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
                where.append(_BIND_RE.sub("?", clause))
            where_params = [params[m.group(1)] for m in _BIND_RE.finditer(clause)] if clause else []
            for col, val in (
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ):
                if val is not None:
                    where.append(f"m.{col} = ?")
                    where_params.append(val)

            # Db2 12.1.5 EAP: the DiskANN vector index supports EUCLIDEAN
            # distance only. For L2-normalized embeddings (MNEMOS default)
            # this preserves the COSINE top-K ordering exactly — see the
            # module-level note above _resolve_db2_vector_index_mode.
            #
            # Recency boost: the rank-score expression with
            # ``- :w * (1 / (1 + (CURRENT DATE - CAST(m.updated AS DATE))))``
            # is not pushed through the DiskANN index by the optimizer,
            # so we always emit the bare distance for ``ORDER BY`` and
            # apply the recency adjustment in Python after fetch. This
            # keeps ``FETCH APPROX FIRST`` engaging the index even when
            # ``boost_recency=True``.
            rank_sql = f"VECTOR_DISTANCE(m.embedding, VECTOR(?, {len(embedding)}, FLOAT32), EUCLIDEAN)"
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, "
                "m.group_id, m.created, m.updated, m.archived_at, "
                "m.recall_count, m.last_recalled_at, m.consolidated_into, "
                f"({rank_sql}) AS rank_score "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                f"ORDER BY {rank_sql} ASC "
                f"{fetch_clause}"
            )
            await _call(cursor.execute, sql, (vec_literal, *where_params, vec_literal, fetch_count))
            rows = await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

        if boost_recency and rows:
            # Match Oracle's recency formula:
            #   adjusted = distance - w * (1.0 / (1.0 + age_days))
            # using COALESCE(last_recalled_at, updated, created). We compute
            # age_days in Python so the SQL ORDER BY stays index-friendly.
            from datetime import timezone

            w = float(recency_weight)
            today = datetime.now(timezone.utc).date()
            # Re-sort ASC on an adjusted ordering key and re-cap to ``limit``.
            # Keep ``rank_score`` as the raw vector distance for route gates.
            rows.sort(key=lambda row: _boosted_rank_supersession_sort_key(row, today=today, recency_weight=w))
            rows = rows[:limit]

        return rows

    async def insert_memory(
        self,
        tx: Any,
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
        # Format embedding as Db2 VECTOR literal; NULL when absent.
        # Inlining it in the MERGE keeps the vector co-transactional
        # with the row — semantic_search sees it immediately.
        vec_literal: str | None = None
        vec_dim: int = 0
        if embedding:
            vec_literal = _validate_and_format_vector(embedding)
            vec_dim = len(embedding)
        conn = _conn_from_tx(tx)
        sync_conn = getattr(conn, "_conn", None)
        if sync_conn is None:
            cursor = await _call(conn.cursor)
            try:
                await _call(
                    cursor.execute,
                    f"""
                    MERGE INTO memories t USING SYSIBM.SYSDUMMY1
                    ON (t.id = ?)
                    WHEN NOT MATCHED THEN INSERT (
                        id, content, category, subcategory, metadata, content_hash,
                        quality_rating, verbatim_content, owner_id, namespace,
                        permission_mode, source_model, source_provider,
                        source_session, source_agent,
                        embedding, created, updated
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        {f"VECTOR(?, {vec_dim}, FLOAT32)" if vec_literal else "NULL"},
                        COALESCE(?, CURRENT TIMESTAMP),
                        COALESCE(?, CURRENT TIMESTAMP)
                    )
                    """,
                    (
                        memory_id,
                        memory_id,
                        content,
                        category,
                        subcategory,
                        metadata_json,
                        _content_hash(content),
                        quality_rating,
                        verbatim_content,
                        owner_id,
                        namespace,
                        permission_mode,
                        source_model,
                        source_provider,
                        source_session,
                        source_agent,
                        *((vec_literal,) if vec_literal else ()),
                        created,
                        updated,
                    ),
                )
                affected = int(getattr(cursor, "rowcount", 0) or 0)
            except Exception as exc:
                if _is_unique_violation(exc):
                    return "INSERT 0 0"
                raise
            finally:
                await _call(cursor.close)
            return "INSERT 0 1" if affected else "INSERT 0 0"
        sql = (
            "MERGE INTO memories t USING SYSIBM.SYSDUMMY1 "
            "ON (t.id = ?) "
            "WHEN NOT MATCHED THEN INSERT ("
            "id, content, category, subcategory, metadata, content_hash, "
            "quality_rating, verbatim_content, owner_id, namespace, permission_mode, "
            "source_model, source_provider, source_session, source_agent, "
            "embedding, created, updated"
            ") VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            f"{f'VECTOR(?, {vec_dim}, FLOAT32)' if vec_literal else 'NULL'}, "
            "COALESCE(?, CURRENT TIMESTAMP), COALESCE(?, CURRENT TIMESTAMP)"
            ")"
        )
        params = (
            memory_id,
            memory_id,
            content,
            category,
            subcategory,
            metadata_json,
            _content_hash(content),
            quality_rating,
            verbatim_content,
            owner_id,
            namespace,
            permission_mode,
            source_model,
            source_provider,
            source_session,
            source_agent,
            *((vec_literal,) if vec_literal else ()),
            created,
            updated,
        )
        try:

            def _go() -> int:
                cur = sync_conn.cursor()
                try:
                    cur.execute(sql, params)
                    return int(cur.rowcount or 0)
                finally:
                    cur.close()

            affected = await asyncio.to_thread(_go)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_memory_by_id(self, tx: Any, memory_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, content, category, subcategory, metadata,
                       quality_rating, compressed_content, verbatim_content,
                       owner_id, namespace, permission_mode, source_model,
                       source_provider, source_session, source_agent,
                       group_id, created, updated, archived_at, deleted_at
                  FROM memories
                 WHERE id = ? AND deleted_at IS NULL
                """,
                (memory_id,),
            )
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    async def upsert_memory_embedding(self, tx: Any, memory_id: str, embedding: Sequence[float]) -> None:
        """Db2 override: positional bind (?) vs Oracle :name.

        Same shape as Oracle's variant — VECTOR column accepts
        array.array('f', vec). No-op when embedding is empty.
        2026-05-24: added so Db2 backend create_memory inline-embed
        actually writes the vector.
        """
        if not embedding:
            return
        vec_literal = _validate_and_format_vector(embedding)
        dim = len(embedding)
        conn = _conn_from_tx(tx)
        sync_conn = getattr(conn, "_conn", None)
        sql = f"UPDATE memories SET embedding = VECTOR(?, {dim}, FLOAT32) WHERE id = ?"
        if sync_conn is None:
            cursor = await _call(conn.cursor)
            try:
                await _call(cursor.execute, sql, (vec_literal, memory_id))
            finally:
                await _call(cursor.close)
            return

        def _go() -> None:
            cur = sync_conn.cursor()
            try:
                cur.execute(sql, (vec_literal, memory_id))
            finally:
                cur.close()

        await asyncio.to_thread(_go)

    async def update_memory(
        self,
        tx: Any,
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
            sets_parts: list[str] = []
            params_list: list[Any] = []
            for key, value in fields.items():
                if key not in self._UPDATABLE_FIELDS:
                    continue
                if key == "updated":
                    continue
                sets_parts.append(f"{key} = ?")
                params_list.append(value)
            if "content" in fields and "content" in self._UPDATABLE_FIELDS:
                sets_parts.append("content_hash = ?")
                params_list.append(_content_hash(fields["content"]))
            if not sets_parts and fields.get("updated") is None:
                return await self.get_memory(tx, memory_id, visibility=visibility)
            if "updated" in fields and fields.get("updated") is not None:
                sets_parts.append("updated = CAST(? AS TIMESTAMP)")
                params_list.append(fields["updated"])
            else:
                sets_parts.append("updated = CURRENT TIMESTAMP")

            clause, vis_params = _render_visibility(visibility)
            where = ["id = ?", "deleted_at IS NULL"]
            params_list.append(memory_id)
            if clause:
                # Convert named binds to positional (?).
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)

            sql = f"UPDATE memories SET {', '.join(sets_parts)} WHERE " + " AND ".join(where)
            await _call(cursor.execute, sql, tuple(params_list))
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_memory(tx, memory_id, visibility=visibility)

    async def soft_delete_memory(
        self,
        tx: Any,
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
            where = ["id = ?", "deleted_at IS NULL"]
            params_list: list[Any] = [memory_id]
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            await _call(
                cursor.execute,
                "UPDATE memories SET deleted_at = CURRENT TIMESTAMP, updated = CURRENT TIMESTAMP WHERE "
                + " AND ".join(where),
                tuple(params_list),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return row

    async def delete_memory(
        self,
        tx: Any,
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
            where = ["id = ?", "deleted_at IS NULL"]
            params_list: list[Any] = [memory_id]
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            await _call(
                cursor.execute,
                "UPDATE memories SET deleted_at = CURRENT TIMESTAMP WHERE " + " AND ".join(where),
                tuple(params_list),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return row

    async def list_memories(
        self,
        tx: Any,
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
            clause, vis_params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if exclude_superseded:
                where.append("m.consolidated_into IS NULL")
            params_list: list[Any] = []
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            if category is not None:
                where.append("m.category = ?")
                params_list.append(category)
            if subcategory is not None:
                where.append("m.subcategory = ?")
                params_list.append(subcategory)
            where_sql = " AND ".join(where)

            await _call(
                cursor.execute,
                f"SELECT COUNT(*) FROM memories m WHERE {where_sql} WITH UR",
                tuple(params_list),
            )
            (total,) = await _call(cursor.fetchone) or (0,)

            page_params = tuple(params_list + [offset, limit])
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
                 OFFSET ? ROWS FETCH NEXT ? ROWS ONLY WITH UR
                """,
                page_params,
            )
            rows = await _fetch_all_dicts(cursor)
            return rows, int(total or 0)
        finally:
            await _call(cursor.close)

    async def count_memories(
        self,
        tx: Any,
        *,
        visibility: VisibilityFilter,
        category: str | None = None,
        subcategory: str | None = None,
        include_archived: bool = False,
    ) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            params_list: list[Any] = []
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            if category is not None:
                where.append("m.category = ?")
                params_list.append(category)
            if subcategory is not None:
                where.append("m.subcategory = ?")
                params_list.append(subcategory)
            where_sql = " AND ".join(where)
            await _call(
                cursor.execute,
                f"SELECT COUNT(*) FROM memories m WHERE {where_sql} WITH UR",
                tuple(params_list),
            )
            (total,) = await _call(cursor.fetchone) or (0,)
            return int(total or 0)
        finally:
            await _call(cursor.close)

    async def get_memory(
        self,
        tx: Any,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
        include_archived: bool = False,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility, table_alias="m")
            where = ["m.id = ?", "m.deleted_at IS NULL"]
            params_list: list[Any] = [memory_id]
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, m.group_id, "
                "m.created, m.updated, m.archived_at, m.deleted_at, "
                "m.recall_count, m.last_recalled_at, m.content_hash, "
                "m.federation_source, m.federation_remote_updated "
                "FROM memories m WHERE " + " AND ".join(where) + " WITH UR"
            )
            await _call(cursor.execute, sql, tuple(params_list))
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    async def assert_memory_readable(self, tx: Any, memory_id: str, user: Any) -> None:
        from mnemos.core.auth_context import UserContext
        from mnemos.persistence.visibility import VisibilityScope

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
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility, table_alias="m")
            where = ["m.id = ?"]
            params_list: list[Any] = [memory_id]
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            sql = "SELECT 1 FROM memories m WHERE " + " AND ".join(where)
            await _call(cursor.execute, sql, tuple(params_list))
            rows = await _fetch_all_dicts(cursor)
            if not rows:
                raise PermissionError("Memory not found")
        finally:
            await _call(cursor.close)

    async def fetch_memory_export(
        self,
        tx: Any,
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
            params_list: list[Any] = []
            for col, val in [
                ("owner_id", effective_owner),
                ("namespace", effective_ns),
                ("category", category),
            ]:
                if val is not None:
                    where.append(f"{col} = ?")
                    params_list.append(val)
            params_list.extend([offset, limit])
            sql = (
                "SELECT id, content, category, subcategory, created, updated, "
                "owner_id, group_id, namespace, permission_mode, quality_rating, "
                "source_model, source_provider, source_session, source_agent, "
                "metadata, verbatim_content, archived_at, consolidated_into, embedding "
                "FROM memories WHERE " + " AND ".join(where) + " "
                "ORDER BY created ASC, id ASC "
                "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params_list))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fts_search(
        self,
        tx: Any,
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
        # FTS predicate is mode-gated (MNEMOS_DB2_TEXT_SEARCH):
        #   like     (default) — UPPER(content) LIKE '%term%'. Full-table scan,
        #                        works on stock Db2 with no Text Search server.
        #   contains           — CONTAINS(content, ?) = 1. Engages the Db2 Text
        #                        Search index. Opt-in: CONTAINS raises SQL20424N
        #                        when no text index exists, so it is never the
        #                        default.
        text_mode = _resolve_db2_text_search_mode(self._settings)
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility, table_alias="m")
            where = ["m.deleted_at IS NULL"]
            params_list: list[Any] = []
            if not include_archived:
                where.append("m.archived_at IS NULL")
            if exclude_superseded:
                where.append("m.consolidated_into IS NULL")
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            if text_mode == "contains":
                # Native Db2 Text Search index predicate; the search argument is
                # the raw query term passed to CONTAINS.
                where.append("CONTAINS(m.content, ?) = 1")
                params_list.append(query.strip())
            else:
                search_term = query.strip().strip("%")
                where.append("UPPER(m.content) LIKE '%' || UPPER(?) || '%'")
                params_list.append(search_term)
            for col, val in [
                ("category", category),
                ("subcategory", subcategory),
                ("source_provider", source_provider),
                ("source_model", source_model),
                ("source_agent", source_agent),
            ]:
                if val is not None:
                    where.append(f"m.{col} = ?")
                    params_list.append(val)
            params_list.append(limit)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.owner_id, m.namespace, m.created, m.updated, "
                "m.consolidated_into "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                "ORDER BY m.updated DESC FETCH FIRST ? ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params_list))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def find_active_duplicate_by_content_hash(
        self,
        tx: Any,
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
                "content_hash = ?",
                "owner_id = ?",
            ]
            params_list: list[Any] = [content_hash, owner_id]
            if not cross_namespace:
                where.append("namespace = ?")
                params_list.append(namespace)
            sql = (
                "SELECT id, content, category, subcategory, owner_id, namespace, "
                "created, updated FROM memories WHERE " + " AND ".join(where) + " FETCH FIRST 1 ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params_list))
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    # ── PR #8d: version-snapshot + recall + stats + memory-log (5 methods) ──

    async def set_suppress_version_snapshot(self, tx: Any) -> None:
        # No-op — Db2 has no version-snapshot trigger to bypass.
        # Oracle parent is also a no-op; this override prevents the
        # compat-mixin from round-tripping through the cursor translator.
        return None

    async def fetch_versioned_memory_ids(self, tx: Any, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ",".join("?" for _ in memory_ids)
            await _call(
                cursor.execute,
                f"""
                SELECT DISTINCT memory_id
                  FROM memory_versions
                 WHERE memory_id IN ({placeholders})
                   AND deleted_at IS NULL
                """,
                tuple(memory_ids),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def gather_stats(self, tx: Any):
        from mnemos.persistence.base import MemoryStatsRow

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN metadata IS NULL
                                 OR LENGTH(COALESCE(metadata, '')) = 0
                                 OR LOCATE('\"federation_origin\"', COALESCE(metadata, '')) = 0
                                THEN 1 ELSE 0 END) AS native_count,
                       SUM(CASE WHEN LOCATE('\"federation_origin\"', COALESCE(metadata, '')) > 0
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

    async def bump_recall_and_get_memory(
        self,
        tx: Any,
        memory_id: str,
        *,
        visibility: VisibilityFilter,
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            clause, vis_params = _render_visibility(visibility)
            where = ["id = ?", "deleted_at IS NULL"]
            params_list: list[Any] = [memory_id]
            if clause:
                pos_clause = _BIND_RE.sub("?", clause)
                for m in _BIND_RE.finditer(clause):
                    params_list.append(vis_params[m.group(1)])
                where.append(pos_clause)
            await _call(
                cursor.execute,
                "UPDATE memories SET "
                "recall_count = COALESCE(recall_count, 0) + 1, "
                "last_recalled_at = CURRENT TIMESTAMP "
                "WHERE " + " AND ".join(where),
                tuple(params_list),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_memory(tx, memory_id, visibility=visibility)

    async def fetch_memory_log(
        self,
        tx: Any,
        memory_id: str,
        branch: str,
        limit: int,
        user: Any,
    ) -> list[Row]:
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
                 WHERE memory_id = ?
                   AND branch = ?
                   AND deleted_at IS NULL
                 ORDER BY version_num DESC
                 FETCH FIRST ? ROWS ONLY
                """,
                (memory_id, branch, limit),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    # ── PR #8e: commit-head / diff / checkout / allowlist / dedup / context
    #         (7 methods — closes out Memory repository) ──

    async def fetch_memory_head_checks(self, tx: Any, memory_ids: Sequence[str]) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ",".join("?" for _ in memory_ids)
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
                tuple(memory_ids),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_diff_commit_pair(
        self,
        tx: Any,
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
                "WHERE memory_id = ? AND commit_hash = ? "
                "AND deleted_at IS NULL"
            )
            await _call(cursor.execute, sql, (memory_id, commit_a))
            row_a = await _row_to_dict(cursor, await _call(cursor.fetchone))
            await _call(cursor.execute, sql, (memory_id, commit_b))
            row_b = await _row_to_dict(cursor, await _call(cursor.fetchone))
            return row_a, row_b
        finally:
            await _call(cursor.close)

    async def fetch_checkout_commit(
        self,
        tx: Any,
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
                 WHERE memory_id = ?
                   AND commit_hash = ?
                   AND deleted_at IS NULL
                """,
                (memory_id, commit_hash),
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def fetch_referenced_memory_allowlist(
        self,
        tx: Any,
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
            placeholders = ",".join("?" for _ in referenced_ids)
            where = [f"id IN ({placeholders})", "deleted_at IS NULL"]
            params_list: list[Any] = list(referenced_ids)
            if scope_owner is not None:
                where.append("owner_id = ?")
                params_list.append(scope_owner)
            if scope_namespace is not None:
                where.append("namespace = ?")
                params_list.append(scope_namespace)
            sql = "SELECT id, owner_id, namespace FROM memories WHERE " + " AND ".join(where)
            await _call(cursor.execute, sql, tuple(params_list))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def backfill_missing_content_hashes(
        self,
        tx: Any,
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
                row = await _row_to_dict(cursor, await _call(cursor.fetchone))
                return int((row or {}).get("cnt") or 0)
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET content_hash = LOWER(HEX(HASH_SHA256(
                           REPLACE(REPLACE(COALESCE(content, ''), CHR(13) || CHR(10), CHR(10)), CHR(13), CHR(10))
                       ))),
                       updated = CURRENT TIMESTAMP
                 WHERE id IN (
                    SELECT id
                      FROM memories
                     WHERE content_hash IS NULL
                     ORDER BY created ASC, id ASC
                     FETCH FIRST ? ROWS ONLY
                 )
                   AND content_hash IS NULL
                """,
                (int(batch_size),),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def find_duplicate_content_groups(
        self,
        tx: Any,
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
            params_list: list[Any] = []
            if namespace is not None:
                where.append("namespace = ?")
                params_list.append(namespace)
            sql = (
                "WITH ordered AS ("
                "SELECT id, owner_id, namespace, content_hash, created, quality_rating, "
                "REPLACE(REPLACE(COALESCE(content, ''), CHR(13) || CHR(10), CHR(10)), CHR(13), CHR(10)) AS normalized_content, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY owner_id, namespace, content_hash, "
                "REPLACE(REPLACE(COALESCE(content, ''), CHR(13) || CHR(10), CHR(10)), CHR(13), CHR(10)) "
                "ORDER BY created DESC, quality_rating DESC, id DESC"
                ") AS rn "
                "FROM memories WHERE " + " AND ".join(where) + " "
                "), grouped AS ("
                "SELECT owner_id, namespace, content_hash, COUNT(*) AS duplicate_count, "
                "LISTAGG(id, CHR(31)) WITHIN GROUP (ORDER BY rn ASC) AS memory_ids "
                "FROM ordered GROUP BY owner_id, namespace, content_hash, normalized_content HAVING COUNT(*) > 1"
                ") "
                "SELECT owner_id, namespace, content_hash, duplicate_count, memory_ids, "
                "SUBSTR(memory_ids, 1, LOCATE(CHR(31), memory_ids || CHR(31)) - 1) AS keep_id, "
                "SUBSTR(memory_ids, 1, LOCATE(CHR(31), memory_ids || CHR(31)) - 1) AS canonical_id "
                "FROM grouped "
                "ORDER BY duplicate_count DESC, owner_id ASC, namespace ASC, content_hash ASC"
            )
            await _call(cursor.execute, sql, tuple(params_list))
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
        tx: Any,
        *,
        canonical_id: str,
        duplicate_ids: Sequence[str],
    ) -> int:
        if not duplicate_ids:
            return 0
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ",".join("?" for _ in duplicate_ids)
            params_list = list(duplicate_ids) + [canonical_id]
            await _call(
                cursor.execute,
                f"""
                UPDATE memories
                   SET deleted_at = CURRENT TIMESTAMP
                 WHERE id IN ({placeholders})
                   AND id != ?
                   AND deleted_at IS NULL
                """,
                tuple(params_list),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    async def fetch_memory_context(
        self,
        tx: Any,
        query: str,
        user: Any,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        # Resolve an embedding via the lifecycle-owned embedder. Import
        # is local to avoid a hard top-level cycle on lifecycle.
        from mnemos.core.lifecycle import _get_embedding
        from mnemos.core.security import is_root
        from mnemos.persistence.visibility import VisibilityScope

        embedding = await _get_embedding(query)
        if not embedding:
            return []

        if hasattr(user, "user_id"):
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


class Db2KGRepository(_Db2OraCompatMixin, OracleKGRepository):
    """KG triples repository — Db2-native overrides for insert + fetch.

    All three methods emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` / ``CURRENT DATE`` (no ``SYSTIMESTAMP`` / ``SYSDATE``)
    - ``COALESCE`` (no ``NVL``)
    - ``DECFLOAT`` for confidence floats (no ``NUMBER``)
    """

    async def insert_kg_triple(
        self,
        tx: Any,
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
                    ?, ?, ?, ?, ?, ?,
                    COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP),
                    CAST(? AS TIMESTAMP),
                    ?,
                    COALESCE(CAST(? AS DECFLOAT), CAST(1.0 AS DECFLOAT)),
                    COALESCE(CAST(? AS DATE), CURRENT DATE),
                    ?, COALESCE(CAST(? AS VARCHAR(100)), 'default')
                FROM SYSIBM.SYSDUMMY1
                WHERE NOT EXISTS (SELECT 1 FROM kg_triples WHERE id = ?)
                """,
                (
                    triple_id,
                    subject,
                    predicate,
                    obj,
                    subject_type,
                    object_type,
                    valid_from,
                    valid_until,
                    memory_id,
                    confidence,
                    created,
                    owner_id,
                    namespace,
                    triple_id,
                ),
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_kg_triple_by_id(self, tx: Any, triple_id: str) -> Row | None:
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
                 WHERE id = ? AND deleted_at IS NULL
                """,
                (triple_id,),
            )
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    async def fetch_kg_triples_for_export(
        self,
        tx: Any,
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
            params: list[Any] = []
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                if include_unattached:
                    where.append(f"(memory_id IS NULL OR memory_id IN ({placeholders}))")
                else:
                    where.append(f"memory_id IN ({placeholders})")
                params.extend(memory_ids)
            elif include_unattached:
                where.append("memory_id IS NULL")
            else:
                return []
            if effective_owner:
                where.append("owner_id = ?")
                params.append(effective_owner)
            if effective_ns:
                where.append("namespace = ?")
                params.append(effective_ns)
            sql = (
                "SELECT id, subject, predicate, object, subject_type, object_type, "
                "valid_from, valid_until, memory_id, confidence, created, owner_id, "
                "namespace FROM kg_triples WHERE " + " AND ".join(where) + " "
                f"FETCH FIRST {int(hard_limit) + 1} ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class Db2VersionRepository(_Db2OraCompatMixin, OracleVersionRepository):
    """Version history repository — Db2-native overrides for insert + fetches.

    All four methods emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` (no ``SYSTIMESTAMP``)
    - ``COALESCE`` (no ``NVL``)
    - ``INTEGER`` for permission_mode (no ``NUMBER``)
    - ``SYSIBM.SYSDUMMY1`` (no ``dual``)
    """

    async def insert_memory_version(
        self,
        tx: Any,
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
        # Db2 stores version JSON fields as text-compatible JSON values;
        # normalize Python lists/dicts here so callers can use one repo
        # contract across SQLite/Postgres/Oracle/Db2/MySQL.
        merge_parents_json = _json_list_text(merge_parents)
        metadata_text = metadata_json if isinstance(metadata_json, str) else _json_text(metadata_json, {})
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
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, COALESCE(CAST(? AS VARCHAR(100)), 'default'),
                    COALESCE(CAST(? AS INTEGER), 600),
                    ?, ?, ?, ?,
                    COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP),
                    ?, COALESCE(CAST(? AS VARCHAR(40)), 'create'),
                    ?, ?, COALESCE(CAST(? AS VARCHAR(100)), 'main'),
                    ?
                FROM SYSIBM.SYSDUMMY1
                WHERE NOT EXISTS (SELECT 1 FROM memory_versions WHERE id = ?)
                """,
                (
                    version_id,
                    memory_id,
                    version_num,
                    content,
                    category,
                    subcategory,
                    metadata_text,
                    verbatim_content,
                    owner_id,
                    namespace,
                    permission_mode,
                    source_model,
                    source_provider,
                    source_session,
                    source_agent,
                    snapshot_at,
                    snapshot_by,
                    change_type,
                    commit_hash,
                    parent_version_id,
                    branch,
                    merge_parents_json,
                    version_id,  # for the NOT EXISTS subquery
                ),
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_memory_version_by_id(self, tx: Any, version_id: str) -> Row | None:
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
                 WHERE id = ? AND deleted_at IS NULL
                """,
                (version_id,),
            )
            rows = await _fetch_all_dicts(cursor)
            if rows:
                out = rows[0]
                if isinstance(out.get("merge_parents"), str):
                    try:
                        import json

                        out["merge_parents"] = json.loads(out["merge_parents"])
                    except Exception:
                        pass
                return out
            return None
        finally:
            await _call(cursor.close)

    async def fetch_memory_versions_for_export(
        self,
        tx: Any,
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
            placeholders = ", ".join("?" for _ in memory_ids)
            params: list[Any] = list(memory_ids)
            where = ["deleted_at IS NULL", f"memory_id IN ({placeholders})"]
            if effective_owner:
                where.append("owner_id = ?")
                params.append(effective_owner)
            if effective_ns:
                where.append("namespace = ?")
                params.append(effective_ns)
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
            await _call(cursor.execute, sql, tuple(params))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_memory_versions_by_ids(self, tx: Any, version_ids: Sequence[str]) -> list[Row]:
        if not version_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ", ".join("?" for _ in version_ids)
            params = tuple(version_ids)
            sql = (
                f"SELECT id, memory_id, owner_id, namespace FROM memory_versions "
                f"WHERE id IN ({placeholders}) AND deleted_at IS NULL"
            )
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class Db2BranchRepository(_Db2OraCompatMixin, OracleBranchRepository):
    """Branch management repository — Db2-native overrides (4 methods).

    Second MERGE INTO native (after State PR #3). All emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` (no ``SYSTIMESTAMP``)
    - ``SYSIBM.SYSDUMMY1`` (no ``dual``)
    - ``COALESCE`` where needed; IN-list with ``?``; FETCH FIRST + ROW_NUMBER native.
    """

    async def upsert_memory_branch_head(
        self,
        tx: Any,
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
                USING (SELECT
                    CAST(? AS VARCHAR(100)) AS memory_id,
                    CAST(? AS VARCHAR(100)) AS name,
                    CAST(? AS VARCHAR(100)) AS head_version_id
                       FROM SYSIBM.SYSDUMMY1) src
                   ON (m.memory_id = src.memory_id AND m.name = src.name)
                WHEN MATCHED THEN UPDATE SET
                    head_version_id = src.head_version_id
                WHEN NOT MATCHED THEN INSERT (
                    id, memory_id, name, head_version_id, created_at
                ) VALUES (
                    src.memory_id || ':' || src.name,
                    src.memory_id,
                    src.name,
                    src.head_version_id,
                    CURRENT TIMESTAMP
                )
                """,
                (memory_id, branch, head_version_id),
            )
        finally:
            await _call(cursor.close)

    async def fetch_memory_branch_heads(
        self,
        tx: Any,
        memory_ids: Sequence[str],
        *,
        authorized_version_uuids: Sequence[str] | None = None,
    ) -> list[Row]:
        if not memory_ids:
            return []
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ", ".join("?" for _ in memory_ids)
            params: list[Any] = list(memory_ids)
            where = [f"memory_id IN ({placeholders})", "deleted_at IS NULL"]
            if authorized_version_uuids is not None:
                if not authorized_version_uuids:
                    return []
                vid_ph = ", ".join("?" for _ in authorized_version_uuids)
                where.append(f"id IN ({vid_ph})")
                params.extend(authorized_version_uuids)
            sql = (
                "SELECT memory_id, branch, id AS head_version_id FROM ("
                "  SELECT memory_id, branch, id, "
                "         ROW_NUMBER() OVER ("
                "             PARTITION BY memory_id, branch "
                "             ORDER BY version_num DESC"
                "         ) AS rn "
                "  FROM memory_versions "
                f"  WHERE {' AND '.join(where)}"
                ") WHERE rn = 1"
            )
            await _call(cursor.execute, sql, tuple(params))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_memory_branches_for_memories(self, tx: Any, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return None
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            placeholders = ", ".join("?" for _ in memory_ids)
            await _call(
                cursor.execute,
                f"DELETE FROM memory_branches WHERE memory_id IN ({placeholders})",
                tuple(memory_ids),
            )
        finally:
            await _call(cursor.close)
        return None

    async def create_memory_branch(
        self,
        tx: Any,
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
                     WHERE memory_id = ?
                       AND commit_hash = ?
                       AND deleted_at IS NULL
                       FETCH FIRST 1 ROWS ONLY
                    """,
                    (memory_id, from_commit),
                )
                rows = await _fetch_all_dicts(cursor)
                if rows:
                    head_version_id = rows[0].get("id") or rows[0].get("ID")
            branch_id = f"{memory_id}:{name}"
            await _call(
                cursor.execute,
                """
                INSERT INTO memory_branches (id, memory_id, name, head_version_id)
                VALUES (?, ?, ?, ?)
                """,
                (branch_id, memory_id, name, head_version_id),
            )
            return {
                "id": branch_id,
                "memory_id": memory_id,
                "name": name,
                "head_version_id": head_version_id,
            }
        finally:
            await _call(cursor.close)


class Db2CompressionRepository(_Db2OraCompatMixin, OracleCompressionRepository):
    """Compression statistics repository — Db2-native overrides for candidate checks + variants.

    All five methods emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` (no ``SYSTIMESTAMP``)
    - ``COALESCE`` (no ``NVL``)
    - ``FROM SYSIBM.SYSDUMMY1`` (no ``FROM DUAL``)
    """

    async def compression_candidate_exists(
        self,
        tx: Any,
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
                 WHERE id = ?
                   AND memory_id = ?
                   AND owner_id = ?
                """,
                (candidate_id, memory_id, owner_id),
            )
            row = await _call(cursor.fetchone)
            return row is not None
        finally:
            await _call(cursor.close)

    async def insert_compressed_variant(
        self,
        tx: Any,
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
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, COALESCE(CAST(? AS VARCHAR(40)), 'balanced'), ?,
                    COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP)
                FROM SYSIBM.SYSDUMMY1
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_compressed_variants WHERE memory_id = ?
                )
                """,
                (
                    memory_id,
                    owner_id,
                    winner_candidate_id,
                    engine_id,
                    engine_version,
                    compressed_content,
                    compressed_tokens,
                    compression_ratio,
                    quality_score,
                    composite_score,
                    scoring_profile,
                    judge_model,
                    selected_at,
                    memory_id,
                ),
            )
            affected = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as exc:
            if _is_unique_violation(exc):
                return "INSERT 0 0"
            raise
        finally:
            await _call(cursor.close)
        return "INSERT 0 1" if affected else "INSERT 0 0"

    async def fetch_compressed_variant_by_memory_id(self, tx: Any, memory_id: str) -> Row | None:
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
                 WHERE memory_id = ?
                """,
                (memory_id,),
            )
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    async def gather_stats(self, tx: Any):
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
        tx: Any,
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
            placeholders = ",".join("?" for _ in memory_ids)
            where = [f"memory_id IN ({placeholders})"]
            params: list[Any] = list(memory_ids)
            if effective_owner:
                where.append("owner_id = ?")
                params.append(effective_owner)
            sql = (
                "SELECT memory_id, owner_id, winner_candidate_id, engine_id, "
                "engine_version, compressed_content, compressed_tokens, "
                "compression_ratio, quality_score, composite_score, "
                "scoring_profile, judge_model, selected_at "
                "FROM memory_compressed_variants WHERE " + " AND ".join(where) + " "
                f"OFFSET 0 ROWS FETCH NEXT {int(hard_limit) + 1} ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)


class Db2WebhookRepository(_Db2OraCompatMixin, OracleWebhookRepository):
    """Webhook dispatch repository — Db2-native ``dispatch_event`` override.

    All other methods inherit verbatim from
    :class:`OracleWebhookRepository` and rely on the cursor-layer
    Oracle→Db2 translation in :class:`_Db2AsyncCursor.execute`.
    """

    async def dispatch_event(
        self,
        tx: Any,
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
            sub_params: list[Any] = []
            if owner_id is not None:
                sub_where.append("owner_id = ?")
                sub_params.append(owner_id)
            if namespace is not None:
                sub_where.append("namespace = ?")
                sub_params.append(namespace)
            # Subscription opts in to an event by listing it in the JSON
            # array stored in ``events``. Db2-native LOCATE keeps this
            # driver-side positional and matches Oracle's quoted-token
            # behavior for compact or pretty-printed arrays.
            sub_where.append("LOCATE(?, CAST(events AS VARCHAR(32672))) > 0")
            sub_params.append(f'"{event_type}"')
            sql_sub = (
                "SELECT id, COALESCE(owner_id, 'default') AS owner_id, "
                "COALESCE(namespace, 'default') AS namespace "
                "FROM webhook_subscriptions WHERE " + " AND ".join(sub_where)
            )
            await _call(cursor.execute, sql_sub, tuple(sub_params))
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
                        ?, ?, ?, ?, COALESCE(CAST(? AS VARCHAR(100)), 'default'),
                        COALESCE(CAST(? AS VARCHAR(100)), 'default'),
                        'pending', 0, CURRENT TIMESTAMP
                    )
                    """,
                    (
                        d_id,
                        sub["id"],
                        event_type,
                        payload_json,
                        sub.get("owner_id"),
                        sub.get("namespace"),
                    ),
                )
                delivery_ids.append(d_id)
            return delivery_ids
        finally:
            await _call(cursor.close)


class Db2ConsultationAuditRepository(_Db2OraCompatMixin, OracleConsultationAuditRepository):
    """Consultation audit repository — Db2-native overrides for model_registry queries.

    All five methods use explicit native Db2 SQL (``?`` positional binds,
    ``CURRENT TIMESTAMP`` where timestamps appear, ``COALESCE``) so the
    Db2 optimizer sees canonical dialect without cursor-layer rewrite.
    """

    async def fetch_recommended_model(
        self,
        tx: Any,
        task_type: str,
        cost_budget: float,
        quality_floor: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        from mnemos.core.recommendation import choose_recommended_model

        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Native Db2: COALESCE + CURRENT TIMESTAMP (no :name, no SYSTIMESTAMP)
            await _call(
                cursor.execute,
                "SELECT provider, model_id, display_name, input_cost_per_mtok, "
                "output_cost_per_mtok, capabilities, graeae_weight, context_window "
                "FROM model_registry WHERE available = 1 AND deprecated = 0",
            )
            rows = await _fetch_all_dicts(cursor)
            return choose_recommended_model(rows, task_type, cost_budget, quality_floor)
        finally:
            await _call(cursor.close)

    async def fetch_model_recommendation(
        self,
        tx: Any,
        task_type: str,
        cost_budget: float = 10.0,
        quality_floor: float = 0.85,
    ) -> dict[str, Any] | None:
        model, _ = await self.fetch_recommended_model(tx, task_type, cost_budget, quality_floor)
        return model

    async def lookup_provider_for_model(self, tx: Any, model: str) -> str | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0",
                (model,),
            )
            rows = await _fetch_all_dicts(cursor)
            if rows:
                return rows[0].get("provider")
            return None
        finally:
            await _call(cursor.close)

    async def fetch_available_models(self, tx: Any) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT provider, model_id, display_name FROM model_registry "
                "WHERE available = 1 AND deprecated = 0 "
                "ORDER BY model_id ASC",
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def fetch_model_provider(self, tx: Any, model_id: str) -> str | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT provider FROM model_registry WHERE model_id = ? AND available = 1 AND deprecated = 0 "
                "FETCH FIRST 1 ROW ONLY",
                (model_id,),
            )
            row = await _fetch_all_dicts(cursor)
            return row[0].get("provider") if row else None
        finally:
            await _call(cursor.close)


class Db2FederationRepository(_Db2OraCompatMixin, OracleFederationRepository):
    """Federation peer management repository — Db2-native overrides (PR #9a + #9b).

    Eleven methods emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` (no ``SYSTIMESTAMP``)
    - ``SYSIBM.SYSDUMMY1`` for MERGE source (no ``DUAL``)
    - ``COALESCE`` (no ``NVL``)
    - ``CAST(? AS TIMESTAMP)`` (no ``CAST(:x AS TIMESTAMP WITH TIME ZONE)``)
    Pattern reused from PR #3 (State) and PR #6 (Branch).

    PR #9a (6 methods): list_peers, get_peer, delete_peer, list_due_peers,
    fetch_memory_page, create_peer.
    PR #9b (5 methods): fetch_federated_memory_marker, insert_federated_memory,
    update_federated_memory_if_newer, apply_consolidation_tombstone,
    delete_federated_memory.
    """

    # ── peer CRUD ──────────────────────────────────────────────────────────

    async def list_peers(self, tx: Any) -> list[Row]:
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

    async def get_peer(self, tx: Any, peer_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM federation_peers WHERE id = ?",
                (peer_id,),
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def delete_peer(self, tx: Any, peer_id: str) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "DELETE FROM federation_peers WHERE id = ?",
                (peer_id,),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_due_peers(self, tx: Any, *, limit: int = 10) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT * FROM federation_peers
                 WHERE enabled = 1
                   AND (last_sync_at IS NULL
                        OR last_sync_at < CURRENT TIMESTAMP - sync_interval_secs SECONDS)
                 ORDER BY COALESCE(last_sync_at, TIMESTAMP('1970-01-01 00:00:00'))
                 FETCH FIRST ? ROWS ONLY
                """,
                (limit,),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    # ── sync helpers ───────────────────────────────────────────────────────

    async def fetch_memory_page(
        self,
        tx: Any,
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
                    "AND (updated > ? OR (updated = ? AND id > ?)) "
                    "ORDER BY updated ASC, id ASC "
                    "FETCH FIRST ? ROWS ONLY"
                )
                params = (updated_after, updated_after, id_after, limit)
            else:
                sql = (
                    "SELECT id, content, category, subcategory, metadata, "
                    "owner_id, namespace, updated FROM memories "
                    "WHERE deleted_at IS NULL "
                    "ORDER BY updated ASC, id ASC "
                    "FETCH FIRST ? ROWS ONLY"
                )
                params = (limit,)
            await _call(cursor.execute, sql, params)
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def create_peer(
        self,
        tx: Any,
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
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    peer_id,
                    name,
                    base_url,
                    auth_token,
                    json.dumps(list(namespace_filter)) if namespace_filter is not None else None,
                    json.dumps(list(category_filter)) if category_filter is not None else None,
                    1 if enabled else 0,
                    sync_interval_secs,
                    compat_mode or "strict",
                ),
            )
        finally:
            await _call(cursor.close)
        return await self.get_peer(tx, peer_id)

    # ── consolidation + tombstone ────────────────────────────────────────

    async def fetch_federated_memory_marker(self, tx: Any, local_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT federation_remote_updated
                  FROM memories
                 WHERE id = ? AND deleted_at IS NULL
                """,
                (local_id,),
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    async def insert_federated_memory(
        self,
        tx: Any,
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
                        ?, ?, ?, ?, ?,
                        ?, ?, 'federation', ?,
                        644, ?, ?,
                        ?, ?, ?,
                        CAST(? AS TIMESTAMP),
                        CURRENT TIMESTAMP,
                        CAST(? AS TIMESTAMP)
                    )
                    """,
                    (
                        local_id,
                        content,
                        category,
                        subcategory,
                        metadata_json,
                        verbatim_content,
                        quality_rating,
                        namespace,
                        source_model,
                        source_provider,
                        source_session,
                        source_agent,
                        peer_name,
                        remote_updated,
                        remote_updated,
                    ),
                )
                return True
            except Exception as e:
                if _is_unique_violation(e):
                    return False
                raise
        finally:
            await _call(cursor.close)

    async def update_federated_memory_if_newer(
        self,
        tx: Any,
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
                    content = ?,
                    category = ?,
                    subcategory = ?,
                    metadata = ?,
                    verbatim_content = ?,
                    quality_rating = ?,
                    namespace = ?,
                    federation_remote_updated = CAST(? AS TIMESTAMP),
                    updated = CAST(? AS TIMESTAMP)
                 WHERE id = ?
                   AND deleted_at IS NULL
                   AND (
                        federation_remote_updated IS NULL
                        OR federation_remote_updated < CAST(? AS TIMESTAMP)
                   )
                """,
                (
                    content,
                    category,
                    subcategory,
                    metadata_json,
                    verbatim_content,
                    quality_rating,
                    namespace,
                    remote_updated,
                    remote_updated,
                    local_id,
                    remote_updated,
                ),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def apply_consolidation_tombstone(
        self,
        tx: Any,
        *,
        local_id: str,
        local_canonical_id: str,
        consolidated_at: Any,
        remote_id: str,
        canonical_remote_id: str,
        peer_name: str,
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                MERGE INTO federation_consolidation_tombstones t
                USING (
                    SELECT ? AS peer_name, ? AS remote_id
                      FROM SYSIBM.SYSDUMMY1
                ) s
                   ON (t.peer_name = s.peer_name AND t.remote_id = s.remote_id)
                WHEN MATCHED THEN UPDATE SET
                    local_id = ?,
                    local_canonical_id = ?,
                    canonical_remote_id = ?,
                    consolidated_at = COALESCE(
                        CAST(? AS TIMESTAMP),
                        CURRENT TIMESTAMP
                    )
                WHEN NOT MATCHED THEN INSERT (
                    peer_name, remote_id, local_id, local_canonical_id,
                    canonical_remote_id, consolidated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?,
                    COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP)
                )
                """,
                (
                    peer_name,
                    remote_id,
                    local_id,
                    local_canonical_id,
                    canonical_remote_id,
                    consolidated_at,
                    consolidated_at,
                    peer_name,
                    remote_id,
                    local_id,
                    local_canonical_id,
                    canonical_remote_id,
                    consolidated_at,
                ),
            )
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET deleted_at = CURRENT TIMESTAMP
                 WHERE id = ?
                   AND deleted_at IS NULL
                   AND EXISTS (
                       SELECT 1 FROM memories c
                        WHERE c.id = ? AND c.deleted_at IS NULL
                   )
                """,
                (local_id, local_canonical_id),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def delete_federated_memory(self, tx: Any, peer_name: str, memory_id: str) -> int:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE memories
                   SET deleted_at = CURRENT TIMESTAMP
                 WHERE id = ?
                   AND federation_source = ?
                   AND deleted_at IS NULL
                """,
                (memory_id, peer_name),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)

    # ── peer management (cont) ────────────────────────────────────────────

    async def update_peer(self, tx: Any, peer_id: str, updates: dict[str, Any]) -> Row | None:
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
            params_list: list[Any] = []
            for col, value in updates.items():
                if col == "enabled":
                    sets.append("enabled = ?")
                    params_list.append(1 if value else 0)
                elif col in ("namespace_filter", "category_filter"):
                    sets.append(f"{col} = ?")
                    params_list.append(json.dumps(list(value)) if value is not None else None)
                else:
                    sets.append(f"{col} = ?")
                    params_list.append(value)
            if not sets:
                return await self.get_peer(tx, peer_id)
            sets.append("updated = CURRENT TIMESTAMP")
            sql = f"UPDATE federation_peers SET {', '.join(sets)} WHERE id = ?"
            params_list.append(peer_id)
            await _call(cursor.execute, sql, tuple(params_list))
            if int(getattr(cursor, "rowcount", 0) or 0) == 0:
                return None
        finally:
            await _call(cursor.close)
        return await self.get_peer(tx, peer_id)

    async def upsert_peer(
        self,
        tx: Any,
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
                USING (
                    SELECT ? AS id FROM SYSIBM.SYSDUMMY1
                ) s
                   ON (p.id = s.id)
                WHEN MATCHED THEN UPDATE SET
                    base_url = ?,
                    name = COALESCE(CAST(? AS VARCHAR(200)), p.name),
                    enabled = ?,
                    updated = CURRENT TIMESTAMP
                WHEN NOT MATCHED THEN INSERT (
                    id, name, base_url, auth_token, enabled
                ) VALUES (
                    ?, COALESCE(CAST(? AS VARCHAR(200)), CAST(? AS VARCHAR(200))), ?, '', ?
                )
                """,
                (
                    peer_id,
                    base_url,
                    name,
                    1 if enabled else 0,
                    peer_id,
                    name,
                    peer_id,
                    base_url,
                    1 if enabled else 0,
                ),
            )
        finally:
            await _call(cursor.close)

    async def get_sync_peer(self, tx: Any, peer_id: str) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT * FROM federation_peers WHERE id = ?",
                (peer_id,),
            )
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)

    # ── sync log CRUD ────────────────────────────────────────────────────

    async def fetch_sync_log(self, tx: Any, peer_id: str, limit: int) -> list[Row]:
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
                 WHERE peer_id = ?
                 ORDER BY started_at DESC
                 FETCH FIRST ? ROWS ONLY
                """,
                (peer_id, limit),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def create_sync_log(self, tx: Any, peer_id: str, cursor_before: Any) -> Any:
        import uuid as _uuid

        log_id = str(_uuid.uuid4())
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO federation_sync_log (id, peer_id, cursor_before)
                VALUES (?, ?, ?)
                """,
                (log_id, peer_id, cursor_before),
            )
        finally:
            await _call(cursor.close)
        return log_id

    async def finish_sync_log(
        self,
        tx: Any,
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
                    finished_at = CURRENT TIMESTAMP,
                    memories_pulled = ?,
                    memories_new = ?,
                    memories_updated = ?,
                    error = ?,
                    cursor_after = ?
                 WHERE id = ?
                """,
                (
                    memories_pulled,
                    memories_new,
                    memories_updated,
                    error,
                    cursor_after,
                    log_id,
                ),
            )
        finally:
            await _call(cursor.close)

    async def record_sync_error(self, tx: Any, peer_id: str, error: str) -> None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                """
                UPDATE federation_peers SET
                    last_sync_at = CURRENT TIMESTAMP,
                    last_error = ?,
                    last_error_at = CURRENT TIMESTAMP
                 WHERE id = ?
                """,
                (error, peer_id),
            )
        finally:
            await _call(cursor.close)

    async def record_sync_success(
        self,
        tx: Any,
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
                    last_sync_at = CURRENT TIMESTAMP,
                    last_sync_cursor = ?,
                    last_error = NULL,
                    last_error_at = NULL,
                    total_pulled = total_pulled + ?
                 WHERE id = ?
                """,
                (cursor, total_pulled, peer_id),
            )
        finally:
            await _call(cur.close)

    async def update_peer_schema_check(
        self,
        tx: Any,
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
                    peer_mnemos_version = ?,
                    last_schema_check_at = CURRENT TIMESTAMP
                 WHERE id = ?
                """,
                (peer_version, peer_id),
            )
        finally:
            await _call(cursor.close)

    # ── feed queries ─────────────────────────────────────────────────────

    async def feed_query(
        self,
        tx: Any,
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
            from mnemos.core.config import federation_feed_include_private

            # World-read gate is opt-out on trusted feed servers; original
            # clause ORDER preserved so default-mode SQL is byte-identical.
            # Trusted fleet peers set MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=1
            # to replicate the full corpus (vault + loop-guard below always hold).
            world_read = [] if federation_feed_include_private() else ["MOD(m.permission_mode, 10) >= 4"]
            where = [
                "m.federation_source IS NULL",
                *world_read,
                "m.deleted_at IS NULL",
                "m.archived_at IS NULL",
                "m.consolidated_into IS NULL",
                # Secret vault: never federate credential-class memories,
                # even when a caller supplies an explicit namespace filter.
                "(m.namespace IS NULL OR m.namespace <> ?)",
            ]
            params_list: list[Any] = [VAULT_NAMESPACE]
            if since_updated is not None and since_id is not None:
                where.append("(m.updated > ? OR (m.updated = ? AND m.id > ?))")
                params_list.extend([since_updated, since_updated, since_id])
            if namespaces:
                ns_ph = ",".join("?" for _ in namespaces)
                where.append(f"m.namespace IN ({ns_ph})")
                params_list.extend(namespaces)
            if categories:
                cat_ph = ",".join("?" for _ in categories)
                where.append(f"m.category IN ({cat_ph})")
                params_list.extend(categories)
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
                _model_escaped = _model.replace("'", "''")
                embed_cols = f", m.embedding AS embedding, '{_model_escaped}' AS embedding_model"
            params_list.append(limit)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.verbatim_content, m.owner_id, m.namespace, "
                "m.permission_mode, m.source_model, m.source_provider, "
                "m.source_session, m.source_agent, m.created, m.updated, "
                "m.archived_at" + embed_cols + " FROM memories m WHERE " + " AND ".join(where) + " "
                "ORDER BY m.updated ASC, m.id ASC "
                "FETCH FIRST ? ROWS ONLY"
            )
            await _call(cursor.execute, sql, tuple(params_list))
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def get_feed_memory(
        self,
        tx: Any,
        memory_id: str,
        *,
        namespaces: Sequence[str],
        categories: Sequence[str],
    ) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            from mnemos.core.config import federation_feed_include_private

            # Mirrors feed_query: world-read gate opt-out, original order kept.
            world_read = [] if federation_feed_include_private() else ["MOD(m.permission_mode, 10) >= 4"]
            where = [
                "m.id = ?",
                "m.federation_source IS NULL",
                *world_read,
                "m.deleted_at IS NULL",
                "m.archived_at IS NULL",
                "m.consolidated_into IS NULL",
                # Secret vault: never serve a vaulted memory over federation.
                "(m.namespace IS NULL OR m.namespace <> ?)",
            ]
            params_list: list[Any] = [memory_id, VAULT_NAMESPACE]
            if namespaces:
                ns_ph = ",".join("?" for _ in namespaces)
                where.append(f"m.namespace IN ({ns_ph})")
                params_list.extend(namespaces)
            if categories:
                cat_ph = ",".join("?" for _ in categories)
                where.append(f"m.category IN ({cat_ph})")
                params_list.extend(categories)
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.verbatim_content, m.owner_id, m.namespace, "
                "m.permission_mode, m.source_model, m.source_provider, "
                "m.source_session, m.source_agent, m.created, m.updated, "
                "m.archived_at "
                "FROM memories m WHERE " + " AND ".join(where)
            )
            await _call(cursor.execute, sql, tuple(params_list))
            return await _row_to_dict(cursor, await _call(cursor.fetchone))
        finally:
            await _call(cursor.close)


class Db2StateRepository(_Db2OraCompatMixin, OracleStateRepository):
    """Key/value state repository — Db2-native overrides (first MERGE INTO on branch).

    All five methods emit explicit native Db2 SQL:
    - ``?`` positional binds (no ``:name``)
    - ``CURRENT TIMESTAMP`` (no ``SYSTIMESTAMP``)
    - ``SYSIBM.SYSDUMMY1`` for MERGE source (no ``DUAL``)
    - ``TO_CHAR`` kept for timestamp formatting (native in Db2 12.1.x)
    Pattern reused by PR #6 (Branch) and PR #9 (Federation).
    """

    async def get(
        self,
        tx: Any,
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
                 WHERE owner_id = ?
                   AND namespace = ?
                   AND key = ?
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace, key),
            )
            rows = await _fetch_all_dicts(cursor)
            return rows[0] if rows else None
        finally:
            await _call(cursor.close)

    async def set(
        self,
        tx: Any,
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
                USING (SELECT ? AS owner_id, ? AS namespace,
                              ? AS key, ? AS value FROM SYSIBM.SYSDUMMY1) src
                   ON (s.owner_id = src.owner_id
                       AND s.namespace = src.namespace
                       AND s.key = src.key)
                WHEN MATCHED THEN UPDATE SET
                    value = src.value,
                    updated = CURRENT TIMESTAMP,
                    version = s.version + 1,
                    deleted_at = NULL
                WHEN NOT MATCHED THEN INSERT (
                    owner_id, namespace, key, value, updated, version
                ) VALUES (
                    src.owner_id, src.namespace, src.key, src.value, CURRENT TIMESTAMP, 1
                )
                """,
                (owner_id, namespace, key, value),
            )
        finally:
            await _call(cursor.close)
        return await self.get(tx, key, owner_id=owner_id, namespace=namespace)

    async def delete(
        self,
        tx: Any,
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
                   SET deleted_at = CURRENT TIMESTAMP
                 WHERE owner_id = ?
                   AND namespace = ?
                   AND key = ?
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace, key),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_namespace(
        self,
        tx: Any,
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
                     WHERE owner_id = ?
                       AND namespace = ?
                       AND deleted_at IS NULL
                     ORDER BY key
                     OFFSET ? ROWS
                    """,
                    (owner_id, namespace, offset),
                )
            else:
                await _call(
                    cursor.execute,
                    """
                    SELECT key, value, TO_CHAR(updated) AS updated, version, owner_id, namespace
                      FROM state
                     WHERE owner_id = ?
                       AND namespace = ?
                       AND deleted_at IS NULL
                     ORDER BY key
                     OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                    """,
                    (owner_id, namespace, offset, limit),
                )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def delete_namespace(
        self,
        tx: Any,
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
                   SET deleted_at = CURRENT TIMESTAMP
                 WHERE owner_id = ?
                   AND namespace = ?
                   AND deleted_at IS NULL
                """,
                (owner_id, namespace),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            await _call(cursor.close)


class Db2OAuthRepository(OracleOAuthRepository):
    """Db2-native OAuth token/state persistence."""

    async def register_oauth_token(
        self,
        tx: Any,
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
                VALUES (?, ?, ?, ?, ?, ?, CURRENT TIMESTAMP, NULL)
                """,
                (
                    _raw_token(token),
                    user_id,
                    provider,
                    _json_text(scopes, []),
                    _ts_for_oracle(expires_at),
                    refresh_token,
                ),
            )
        finally:
            await _call(cursor.close)
        row = await self.lookup_oauth_token(tx, token=token, touch=False)
        assert row is not None
        return row

    async def lookup_oauth_token(self, tx: Any, *, token: str | bytes, touch: bool = True) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        raw = _raw_token(token)
        try:
            if touch:
                await _call(
                    cursor.execute, "UPDATE oauth_tokens SET last_used_at = CURRENT TIMESTAMP WHERE token = ?", (raw,)
                )
            await _call(
                cursor.execute,
                """
                SELECT token, user_id, provider, scopes, expires_at, refresh_token, created_at, last_used_at
                FROM oauth_tokens
                WHERE token = ?
                """,
                (raw,),
            )
            return self._normalize_oauth_token_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
        finally:
            await _call(cursor.close)

    async def revoke_oauth_token(self, tx: Any, *, token: str | bytes) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(cursor.execute, "DELETE FROM oauth_tokens WHERE token = ?", (_raw_token(token),))
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def start_oauth_flow(
        self,
        tx: Any,
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
                VALUES (?, ?, ?, ?, CURRENT TIMESTAMP, ?)
                """,
                (_raw_token(state), provider, csrf_token, return_url, _ts_for_oracle(expires_at)),
            )
        finally:
            await _call(cursor.close)
        row = await self._fetch_oauth_state(tx, state=state)
        assert row is not None
        return row

    async def redeem_oauth_state(self, tx: Any, *, state: str | bytes) -> Row | None:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        raw = _raw_token(state)
        try:
            await _call(
                cursor.execute,
                """
                SELECT state, provider, csrf_token, return_url, created_at, expires_at
                FROM oauth_state
                WHERE state = ? AND expires_at > CURRENT TIMESTAMP
                """,
                (raw,),
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            await _call(cursor.execute, "DELETE FROM oauth_state WHERE state = ?", (raw,))
            return self._normalize_oauth_state_row(row)
        finally:
            await _call(cursor.close)

    async def _fetch_oauth_state(self, tx: Any, *, state: str | bytes) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT state, provider, csrf_token, return_url, created_at, expires_at FROM oauth_state WHERE state = ?",
                (_raw_token(state),),
            )
            return self._normalize_oauth_state_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
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

    # ── Auth lookup overrides (Db2 cursor uses positional binds) ────────────
    #
    # The Oracle base class uses named binds (``:name``). Db2 cursors go
    # through ``ibm_db_dbi`` and bind positionally, so we re-implement the
    # auth-lookup methods here. Schema parity with Oracle's
    # ``migrations_db2/0003_api_keys.sql`` and ``0006_oauth_sessions.sql``
    # is what allows the same SQL shape with positional binds.

    async def lookup_api_key(
        self, tx: Any, key_hash: str
    ) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT ak.id, ak.owner_id AS user_id, "
                "       CASE WHEN ak.revoked_at IS NULL THEN 0 ELSE 1 END AS revoked, "
                "       u.role, u.namespace "
                "FROM api_keys ak JOIN users u ON u.id = ak.owner_id "
                "WHERE ak.key_hash = ?",
                (key_hash,),
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            if not row:
                return None
            await _call(
                cursor.execute,
                "SELECT group_id FROM user_groups WHERE user_id = ?",
                (row["user_id"],),
            )
            group_rows = await _fetch_all_dicts(cursor)
            row["group_ids"] = [gr["group_id"] for gr in group_rows]
            return row
        finally:
            await _call(cursor.close)

    async def touch_api_key(self, tx: Any, key_id: Any) -> None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE api_keys SET last_used_at = CURRENT TIMESTAMP WHERE id = ?",
                (key_id,),
            )
        finally:
            await _call(cursor.close)

    async def resolve_active_session(
        self, tx: Any, session_id: str, *, now: Any
    ) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT user_id, identity_id, revoked, expires_at FROM oauth_sessions "
                "WHERE session_id = ?",
                (session_id,),
            )
            row = await _row_to_dict(cursor, await _call(cursor.fetchone))
            if not row:
                return None
            if row["revoked"]:
                return None
            expires_at = row["expires_at"]
            if expires_at is not None and now is not None and expires_at <= now:
                return None
            await _call(
                cursor.execute,
                "UPDATE oauth_sessions SET last_used_at = CURRENT TIMESTAMP "
                "WHERE session_id = ?",
                (session_id,),
            )
            return row
        finally:
            await _call(cursor.close)


class Db2SessionsRepository(OracleSessionsRepository):
    """Db2-native protocol sessions persistence."""

    pass


class Db2ConsultationsRepository(OracleConsultationsRepository):
    """Db2-native user-facing consultations persistence."""

    async def create_consultation(
        self,
        tx: Any,
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
                INSERT INTO consultations (id, user_id, prompt, task_type, mode, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT TIMESTAMP, NULL)
                """,
                (_uuid_to_raw(cid), user_id, prompt, task_type, mode, status),
            )
        finally:
            await _call(cursor.close)
        row = await self.fetch_consultation(tx, consultation_id=cid)
        assert row is not None
        return row

    async def append_consultation_response(
        self,
        tx: Any,
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
            await _call(
                cursor.execute,
                """
                SELECT id, consultation_id, provider, model_id, response, final_score,
                       tokens_in, tokens_out, latency_ms, created_at
                FROM FINAL TABLE (
                    INSERT INTO consultation_responses
                      (consultation_id, provider, model_id, response, final_score,
                       tokens_in, tokens_out, latency_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT TIMESTAMP)
                )
                """,
                (
                    _uuid_to_raw(consultation_id),
                    provider,
                    model_id,
                    response,
                    final_score,
                    tokens_in,
                    tokens_out,
                    latency_ms,
                ),
            )
            return self._normalize_consultation_response_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))  # type: ignore[return-value]
        finally:
            await _call(cursor.close)

    async def fetch_consultation(self, tx: Any, *, consultation_id: str | bytes | uuid.UUID) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, user_id, prompt, task_type, mode, status, created_at, completed_at
                FROM consultations
                WHERE id = ?
                """,
                (_uuid_to_raw(consultation_id),),
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
        tx: Any,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        where: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT id, user_id, prompt, task_type, mode, status, created_at, completed_at FROM consultations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        params.extend([int(offset), int(limit)])
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(cursor.execute, sql, tuple(params))
            return [self._normalize_consultation_row(row) for row in await _fetch_all_dicts(cursor)]  # type: ignore[list-item]
        finally:
            await _call(cursor.close)

    async def _fetch_consultation_responses(self, tx: Any, *, consultation_id: str | bytes | uuid.UUID) -> list[Row]:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, consultation_id, provider, model_id, response, final_score,
                       tokens_in, tokens_out, latency_ms, created_at
                FROM consultation_responses
                WHERE consultation_id = ?
                ORDER BY id
                """,
                (_uuid_to_raw(consultation_id),),
            )
            return [self._normalize_consultation_response_row(row) for row in await _fetch_all_dicts(cursor)]  # type: ignore[list-item]
        finally:
            await _call(cursor.close)

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


class Db2AuditChainRepository(OracleAuditChainRepository):
    """Db2 12.1.x impl of v6.2 M-2.2.1 audit chain.

    Inherits the Oracle implementation verbatim — Db2's Oracle
    Compatibility Mode supports `TO_TIMESTAMP_TZ`, `NUMTODSINTERVAL`,
    `ROWNUM`, `SYSTIMESTAMP`, and `FOR UPDATE SKIP LOCKED` (Db2 11.5+).
    Cursor-level Ora→Db2 translation (`_ORA_TO_DB2_PAIRS` +
    `_BIND_RE`) handles `SYSTIMESTAMP`→`CURRENT TIMESTAMP` and
    `:name` → `?` bind conversion.

    No override needed unless Db2 diverges on a specific call;
    file a follow-up commit if/when that happens.
    """


class Db2AclRepository(OracleAclRepository):
    """Db2-native per-principal memory ACL grants.

    Native MERGE on ``SYSIBM.SYSDUMMY1`` (no ``DUAL``) with ``?``
    positional binds so the grant upsert works under both
    :class:`Db2Backend` (compat cursor) and :class:`Db2BackendNative`
    (which forbids ``FROM DUAL``). ``created_at`` is rendered with
    ``TO_CHAR`` to match the other Db2-native repos.
    """

    async def grant_acl(
        self,
        tx: Any,
        *,
        memory_id: str,
        principal: str,
        perm: int,
        granted_by: str,
    ) -> Row:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        merge_sql = """
                MERGE INTO memory_acl t
                USING (SELECT ? AS memory_id, ? AS principal,
                              ? AS perm, ? AS granted_by FROM SYSIBM.SYSDUMMY1) src
                   ON (t.memory_id = src.memory_id AND t.principal = src.principal)
                WHEN MATCHED THEN UPDATE SET
                    perm = src.perm,
                    granted_by = src.granted_by
                WHEN NOT MATCHED THEN INSERT (memory_id, principal, perm, granted_by)
                VALUES (src.memory_id, src.principal, src.perm, src.granted_by)
                """
        binds = (memory_id, principal, perm, granted_by)
        try:
            try:
                await _call(cursor.execute, merge_sql, binds)
            except Exception as exc:  # noqa: BLE001 — dialect-specific dup-key retry
                if not _is_unique_violation(exc):
                    raise
                # Concurrent first-grant race: the NOT MATCHED INSERT lost to a
                # peer. Re-run the MERGE so it takes the MATCHED UPDATE branch.
                await _call(cursor.execute, merge_sql, binds)
        finally:
            await _call(cursor.close)
        rows = await self.list_acl(tx, memory_id)
        for row in rows:
            if row.get("principal") == principal or row.get("PRINCIPAL") == principal:
                return row
        return None

    async def revoke_acl(self, tx: Any, *, memory_id: str, principal: str) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "DELETE FROM memory_acl WHERE memory_id = ? AND principal = ?",
                (memory_id, principal),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def list_acl(self, tx: Any, memory_id: str) -> list[Row]:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT memory_id, principal, perm, granted_by, "
                "TO_CHAR(created) AS created_at "
                "FROM memory_acl WHERE memory_id = ? ORDER BY principal",
                (memory_id,),
            )
            return await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

    async def is_group_admin(
        self,
        tx: Any,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            await _call(
                cursor.execute,
                "SELECT 1 FROM user_groups WHERE user_id = ? AND group_id = ? AND is_admin = 1 FETCH FIRST 1 ROW ONLY",
                (user_id, group_id),
            )
            return await _call(cursor.fetchone) is not None
        finally:
            await _call(cursor.close)


class Db2Backend(OracleBackend):
    """IBM Db2 12.1.x backend via Oracle Compatibility Mode.

    **Important — performance posture and roadmap.** This backend ships
    on top of Db2's *Oracle Compatibility Mode* (``DB CFG ORA_COMPATIBILITY ON``
    + ``ENABLE_ORACLE_COMPATIBILITY=true`` in the container env). The
    repository classes :class:`Db2MemoryRepository` etc. still subclass
    their Oracle counterparts, but most hot-path methods now emit native
    Db2 SQL directly. Remaining inherited methods are protected by
    :class:`_Db2AsyncCursor.execute`, which rewrites Oracle tokens
    (``SYSTIMESTAMP``→``CURRENT TIMESTAMP``, ``:name`` named binds →
    ``?`` positional binds, etc.) at query time.

    This hybrid was the fastest path to backend parity while allowing
    native overrides to land incrementally. It still carries a
    maintainability cost: native SQL and Oracle-compat inheritance live
    in one large module, and compat-mode deployments can hide a method
    that would fail under :class:`Db2BackendNative` unless native-cursor
    tests exercise it.

    On ``open()``, probes the ``DB2_VECTOR_INDEXING`` registry variable
    and logs a clear warning if the operator hasn't enabled native
    vector indexing — without it the DiskANN index from
    ``mnemos/db_migrations/migrations_db2/0001_core_schema.sql`` cannot be created, and
    the app-path ``semantic_search`` override silently degrades to
    exact scan even when ``MNEMOS_DB2_VECTOR_INDEX=approx`` is set.
    """

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    # Db2 12.1.2+ ships native VECTOR data type with VECTOR_DISTANCE
    # functions identical to Oracle 23ai. DiskANN-style ANN index
    # lands in 12.1.5 (GA Jun 9 2026).
    supports_db2_vector = True

    _UNSUPPORTED_CAPABILITY_MARKERS = frozenset({})

    async def ensure_schema(self) -> None:
        """Create or upgrade the configured Db2 schema idempotently."""
        if self._closed or self._pool is None:
            return
        await ensure_db2_schema(self._pool, self._settings)
        self._schema_ensured = True

    async def insert_pantheon_routing_audit(
        self,
        tx: Any,
        record: Mapping[str, Any],
    ) -> None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                INSERT INTO pantheon_routing_audit
                       (request_id, tenant_user_id, alias_or_model, resolved_to, outcome,
                        latency_ms, tokens_in, tokens_out, cost_usd, error_class, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("request_id"),
                    record.get("tenant_user_id"),
                    record.get("alias_or_model"),
                    record.get("resolved_to"),
                    record.get("outcome"),
                    record.get("latency_ms"),
                    record.get("tokens_in"),
                    record.get("tokens_out"),
                    record.get("cost_usd"),
                    record.get("error_class"),
                    record.get("payload_json"),
                ),
            )
        finally:
            await _call(cursor.close)

    def __init__(self, pool: Any, settings: Any):
        # Call OracleBackend.__init__ so any new ``_X_repo`` attribute
        # added upstream is initialized (Opus review O15). Then rebind
        # repository instances to Db2 subclasses; everything else (pool,
        # settings, _closed, any future fields) inherits unchanged.
        super().__init__(pool, settings)
        self._memories_repo = Db2MemoryRepository()
        # Settings reference flows to the override so it can read
        # ``settings.db2_vector_index`` (env var still wins).
        self._memories_repo._settings = settings
        self._kg_triples_repo = Db2KGRepository()
        self._memory_versions_repo = Db2VersionRepository()
        self._memory_branches_repo = Db2BranchRepository()
        self._compression_repo = Db2CompressionRepository()
        self._webhooks_repo = Db2WebhookRepository()
        self._consultations_audit_repo = Db2ConsultationAuditRepository()
        self._federation_repo = Db2FederationRepository()
        self._state_kv_repo = Db2StateRepository()
        # RA-0/5/6: OAuth/Sessions/Consultations repos (PYTHIA Oracle-only
        # uses these in production; Db2 needs them for cross-backend parity
        # so the same code paths exercise both backends in tests).
        self._oauth_repo = Db2OAuthRepository()
        self._sessions_repo = Db2SessionsRepository()
        self._consultations_repo = Db2ConsultationsRepository()
        self._audit_chain_repo = Db2AuditChainRepository()
        self._acl_repo = Db2AclRepository()
        # Startup registry-probe state, populated lazily by ``open()``.
        # ``None`` means "not yet probed"; otherwise stores the raw
        # registry value (``"YES"``, ``"NO"``, ``""``...) for the
        # ``is_vector_indexing_enabled`` property + ``mnemos doctor``.
        self._db2_vector_indexing_value: str | None = None

    def __getattribute__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "_UNSUPPORTED_CAPABILITY_MARKERS"):
            raise AttributeError(name)
        return super().__getattribute__(name)

    @property
    def capabilities(self) -> set[str]:
        return {"core", "oauth", "sessions", "consultations", "federation", "audit", "state", "acl"}

    async def register_oauth_token(self, tx: Any, **kwargs: Any) -> Row:
        return await self._oauth_repo.register_oauth_token(tx, **kwargs)

    async def lookup_oauth_token(self, tx: Any, **kwargs: Any) -> Row | None:
        return await self._oauth_repo.lookup_oauth_token(tx, **kwargs)

    async def revoke_oauth_token(self, tx: Any, **kwargs: Any) -> bool:
        return await self._oauth_repo.revoke_oauth_token(tx, **kwargs)

    async def start_oauth_flow(self, tx: Any, **kwargs: Any) -> Row:
        return await self._oauth_repo.start_oauth_flow(tx, **kwargs)

    async def redeem_oauth_state(self, tx: Any, **kwargs: Any) -> Row | None:
        return await self._oauth_repo.redeem_oauth_state(tx, **kwargs)

    async def create_session(
        self,
        tx: Any,
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
                VALUES (?, ?, ?, CURRENT TIMESTAMP, CURRENT TIMESTAMP, ?, ?)
                """,
                (str(uuid.UUID(bytes=sid_raw)), sid_raw, user_id, _ts_for_oracle(expires_at), _json_text(metadata, {})),
            )
        finally:
            await _call(cursor.close)
        row = await self.lookup_session(tx, session_id=sid)
        assert row is not None
        return row

    async def lookup_session(self, tx: Any, *, session_id: str | bytes | uuid.UUID) -> Row | None:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT session_id, user_id, started_at, last_active_at, expires_at, metadata
                FROM sessions
                WHERE session_id = ? AND expires_at > CURRENT TIMESTAMP
                """,
                (_uuid_to_raw(session_id),),
            )
            return self._normalize_session_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))
        finally:
            await _call(cursor.close)

    async def update_session_active(self, tx: Any, *, session_id: str | bytes | uuid.UUID) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE sessions SET last_active_at = CURRENT TIMESTAMP WHERE session_id = ?",
                (_uuid_to_raw(session_id),),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def expire_session(self, tx: Any, *, session_id: str | bytes | uuid.UUID) -> bool:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                "UPDATE sessions SET expires_at = CURRENT TIMESTAMP WHERE session_id = ?",
                (_uuid_to_raw(session_id),),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0
        finally:
            await _call(cursor.close)

    async def log_session_event(
        self,
        tx: Any,
        *,
        session_id: str | bytes | uuid.UUID,
        event_kind: str,
        payload: dict[str, Any] | None = None,
    ) -> Row:
        cursor = await _call(_conn_from_tx(tx).cursor)
        try:
            await _call(
                cursor.execute,
                """
                SELECT id, session_id, event_kind, payload, ts
                FROM FINAL TABLE (
                    INSERT INTO session_logs (session_id, event_kind, payload, ts)
                    VALUES (?, ?, ?, CURRENT TIMESTAMP)
                )
                """,
                (_uuid_to_raw(session_id), event_kind, _json_text(payload, {})),
            )
            return self._normalize_session_log_row(await _row_to_dict(cursor, await _call(cursor.fetchone)))  # type: ignore[return-value]
        finally:
            await _call(cursor.close)

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

    async def create_consultation(self, tx: Any, **kwargs: Any) -> Row:
        return await self._consultations_repo.create_consultation(tx, **kwargs)

    async def append_consultation_response(self, tx: Any, **kwargs: Any) -> Row:
        return await self._consultations_repo.append_consultation_response(tx, **kwargs)

    async def fetch_consultation(self, tx: Any, **kwargs: Any) -> Row | None:
        return await self._consultations_repo.fetch_consultation(tx, **kwargs)

    async def list_consultations(self, tx: Any, **kwargs: Any) -> list[Row]:
        return await self._consultations_repo.list_consultations(tx, **kwargs)

    async def record_usage_ledger(
        self,
        tx: Any,
        record: Any,
    ) -> Any:
        """Record model-token usage with Db2-native INSERT/IDENTITY return.

        Db2 does not support Oracle's ``RETURNING ... INTO`` through the
        ibm_db_dbi cursor wrapper, so this uses ``FINAL TABLE`` to return the
        generated identity and computed cost. Unknown registry prices still
        insert the usage row with explicit zero cost and a drift warning.
        """
        from decimal import Decimal

        from mnemos.persistence.base import UsageLedgerResult

        def _is_missing_model_registry(exc: BaseException) -> bool:
            sqlstate = str(getattr(exc, "sqlstate", "")).upper()
            sqlcode = str(getattr(exc, "sqlcode", ""))
            msg = str(exc).upper()
            return (
                sqlstate == "42704"
                or sqlcode == "-204"
                or "SQLSTATE=42704" in msg
                or "SQLSTATE 42704" in msg
                or "SQLCODE=-204" in msg
                or "SQL0204N" in msg
            )

        async def _insert_zero_cost() -> Any:
            await _call(
                cursor.execute,
                """
                SELECT id, est_cost_usd
                FROM FINAL TABLE (
                    INSERT INTO usage_ledger (
                        provider, model, task_kind, tokens_in, tokens_out,
                        tokens_reasoning, est_cost_usd, latency_ms, outcome,
                        caller_subsystem, tier, session_id, request_count,
                        plan_window_id, path_kind, subscription_amortized
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?,
                        DECIMAL(0, 12, 6),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                )
                """,
                (
                    record.provider,
                    record.model,
                    record.task_kind,
                    record.tokens_in,
                    record.tokens_out,
                    record.tokens_reasoning,
                    record.latency_ms,
                    record.outcome,
                    record.caller_subsystem,
                    record.tier,
                    record.session_id,
                    record.request_count,
                    record.plan_window_id,
                    record.path_kind or "api",
                    0,
                ),
            )
            return await _call(cursor.fetchone)

        conn = _conn_from_tx(tx)
        cursor = conn.cursor()
        try:
            auth_method = "api"
            try:
                await _call(
                    cursor.execute,
                    "SELECT auth_method FROM subscription_plans WHERE provider = ? AND plan_name = ?",
                    (record.provider, record.tier),
                )
                plan_row = await _call(cursor.fetchone)
                if plan_row:
                    auth_method = str(plan_row[0]).lower()
            except Exception as exc:
                if not _is_missing_model_registry(exc):
                    raise

            if auth_method == "subscription":
                await _call(
                    cursor.execute,
                    """
                    SELECT id, est_cost_usd
                    FROM FINAL TABLE (
                        INSERT INTO usage_ledger (
                            provider, model, task_kind, tokens_in, tokens_out,
                        tokens_reasoning, est_cost_usd, latency_ms, outcome,
                        caller_subsystem, tier, session_id, request_count,
                        plan_window_id, path_kind, subscription_amortized
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?,
                        DECIMAL(0, 12, 6),
                        ?, ?, ?, ?, ?, ?, ?, ?, SMALLINT(1)
                    )
                    )
                    """,
                    (
                        record.provider,
                        record.model,
                        record.task_kind,
                        record.tokens_in,
                        record.tokens_out,
                        record.tokens_reasoning,
                        record.latency_ms,
                        record.outcome,
                        record.caller_subsystem,
                        record.tier,
                        record.session_id,
                        record.request_count,
                        record.plan_window_id,
                        record.path_kind or "api",
                    ),
                )
                row = await _call(cursor.fetchone)
            elif record.est_cost_usd is not None:
                await _call(
                    cursor.execute,
                    """
                    SELECT id, est_cost_usd
                    FROM FINAL TABLE (
                        INSERT INTO usage_ledger (
                            provider, model, task_kind, tokens_in, tokens_out,
                            tokens_reasoning, est_cost_usd, latency_ms, outcome,
                            caller_subsystem, tier, session_id, request_count,
                            plan_window_id, path_kind, subscription_amortized
                        )
                        VALUES (
                            ?, ?, ?, ?, ?, ?,
                            CAST(? AS DECIMAL(12, 6)),
                            ?, ?, ?, ?, ?, ?, ?, ?, SMALLINT(0)
                        )
                    )
                    """,
                    (
                        record.provider,
                        record.model,
                        record.task_kind,
                        record.tokens_in,
                        record.tokens_out,
                        record.tokens_reasoning,
                        record.est_cost_usd,
                        record.latency_ms,
                        record.outcome,
                        record.caller_subsystem,
                        record.tier,
                        record.session_id,
                        record.request_count,
                        record.plan_window_id,
                        record.path_kind or "api",
                    ),
                )
                row = await _call(cursor.fetchone)
            else:
                try:
                    await _call(
                        cursor.execute,
                        "SELECT 1 FROM model_registry WHERE provider = ? AND model_id = ?",
                        (record.provider, record.model),
                    )
                    if await _call(cursor.fetchone) is None:
                        _LOG.warning(
                            "usage_ledger model_registry price missing for provider=%s model=%s; "
                            "recording est_cost_usd=0",
                            record.provider,
                            record.model,
                        )
                except Exception as exc:
                    if not _is_missing_model_registry(exc):
                        raise
                    _LOG.warning(
                        "usage_ledger model_registry table missing for provider=%s model=%s; recording est_cost_usd=0",
                        record.provider,
                        record.model,
                    )
                    row = await _insert_zero_cost()
                else:
                    try:
                        await _call(
                            cursor.execute,
                            """
                        SELECT id, est_cost_usd
                        FROM FINAL TABLE (
                            INSERT INTO usage_ledger (
                                provider, model, task_kind, tokens_in, tokens_out,
                                tokens_reasoning, est_cost_usd, latency_ms, outcome,
                                caller_subsystem, tier, session_id, request_count,
                                plan_window_id, path_kind, subscription_amortized
                            )
                            VALUES (
                                ?, ?, ?, ?, ?, ?,
                                COALESCE((
                                    SELECT DECIMAL(
                                        (? * COALESCE(input_cost_per_mtok, 0)
                                       + ? * COALESCE(output_cost_per_mtok, 0)
                                       + ? * COALESCE(output_cost_per_mtok, 0))
                                       / 1000000,
                                       12,
                                       6
                                    )
                                    FROM model_registry
                                    WHERE provider = ? AND model_id = ?
                                ), DECIMAL(0, 12, 6)),
                                ?, ?, ?, ?, ?, ?, ?, ?, SMALLINT(0)
                            )
                        )
                        """,
                            (
                                record.provider,
                                record.model,
                                record.task_kind,
                                record.tokens_in,
                                record.tokens_out,
                                record.tokens_reasoning,
                                record.tokens_in,
                                record.tokens_out,
                                record.tokens_reasoning,
                                record.provider,
                                record.model,
                                record.latency_ms,
                                record.outcome,
                                record.caller_subsystem,
                                record.tier,
                                record.session_id,
                                record.request_count,
                                record.plan_window_id,
                                record.path_kind or "api",
                            ),
                        )
                        row = await _call(cursor.fetchone)
                    except Exception as exc:
                        if not _is_missing_model_registry(exc):
                            raise
                        _LOG.warning(
                            "usage_ledger model_registry table missing for provider=%s model=%s; "
                            "recording est_cost_usd=0",
                            record.provider,
                            record.model,
                        )
                        row = await _insert_zero_cost()

        finally:
            await _call(cursor.close)

        if row is None:
            raise RuntimeError("usage_ledger insert returned no row")
        return UsageLedgerResult(
            id=int(row[0]),
            est_cost_usd=Decimal(str(row[1])) if row[1] is not None else Decimal("0"),
        )

    async def open(self) -> None:
        """Open hook — probes ``DB2_VECTOR_INDEXING`` registry var.

        Idempotent. Delegates to :meth:`OracleBackend.open` (added in
        the 2026-05-21 Oracle eng pass — runs a ``SELECT 1 FROM DUAL``
        smoke through the pool to validate auth + session callback +
        Oracle-compat ``DUAL`` translation), then runs the Db2-specific
        registry probe. Failure of either step is non-fatal: the
        Oracle parent probe + the Db2 registry probe both log
        WARNINGs rather than raising so a transiently-unreachable Db2
        instance still opens the backend cleanly.
        """
        parent_open = getattr(super(), "open", None)
        if callable(parent_open):
            result = parent_open()
            if hasattr(result, "__await__"):
                await result
        await self._probe_vector_indexing_registry()

    async def _probe_vector_indexing_registry(self) -> None:
        """Read ``DB2_VECTOR_INDEXING`` from ``SYSIBMADM.REG_VARIABLES``.

        Records the raw value on ``self._db2_vector_indexing_value`` and
        logs a clear actionable warning if it isn't ``'YES'``. Never
        raises — the backend stays open even if the probe fails.
        """
        if self._pool is None:
            return
        sql = "SELECT REG_VAR_VALUE FROM SYSIBMADM.REG_VARIABLES WHERE REG_VAR_NAME = 'DB2_VECTOR_INDEXING'"
        value: str | None = None
        try:
            async with self._pool.acquire() as conn:
                cursor = conn.cursor()
                try:
                    await cursor.execute(sql, None)
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
            if row is not None:
                # Row may be tuple-like or dict-like depending on driver.
                value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
                value = "" if value is None else str(value).strip()
        except Exception as exc:  # pragma: no cover — probe is best-effort
            _LOG.warning(
                "Db2 DB2_VECTOR_INDEXING registry probe failed (%s); "
                "operator should verify ``db2set -all | grep DB2_VECTOR_INDEXING`` "
                "is set to YES before vector-index creation.",
                exc,
            )
            return

        self._db2_vector_indexing_value = value or ""
        if (value or "").upper() != "YES":
            _LOG.warning(
                "Db2 registry var DB2_VECTOR_INDEXING is %r (expected 'YES'). "
                "Vector index creation will fail and ``semantic_search`` "
                "FETCH APPROX FIRST will degrade to a sequential scan. "
                "Operator action required: db2set DB2_VECTOR_INDEXING=YES && "
                "db2stop && db2start.",
                value or "<unset>",
            )

    @property
    def is_vector_indexing_enabled(self) -> bool:
        """``True`` when the ``DB2_VECTOR_INDEXING`` registry var was
        observed at ``'YES'`` by the startup probe. ``False`` before
        ``open()`` runs or if the probe failed — health-check / doctor
        surfaces use this to surface the operator-action warning.
        """
        return (self._db2_vector_indexing_value or "").upper() == "YES"


class Db2BackendNative(Db2Backend):
    """Db2 backend with native-cursor pass-through (no Oracle→Db2 token translation).

    Suitable for deployments where every repository method emits Db2-native
    SQL natively (i.e. uses ``?`` positional binds, ``CURRENT TIMESTAMP``,
    ``FROM SYSIBM.SYSDUMMY1``, etc.). MNEMOS as of PR #9c has every
    persistence repository natively overridden, so this is the production
    posture going forward.

    Operators on older versions OR ones who have customized repositories
    with Oracle-shape SQL should stay on :class:`Db2Backend` (the compat
    default). Toggle via env var ``MNEMOS_DB2_DIALECT={native|compat}``.
    """

    supports_listen_notify = False
    supports_advisory_locks = False
    supports_row_level_security = False
    supports_pgvector = False
    supports_db2_vector = True

    # The native cursor rejects ``FROM DUAL`` (it does no Oracle->Db2 token
    # translation), so the inherited open()/ping() liveness probe must use
    # Db2's one-row catalog view instead of Oracle's DUAL.
    _LIVENESS_PROBE_SQL = "SELECT 1 FROM SYSIBM.SYSDUMMY1"

    def __init__(self, pool: Any, settings: Any):
        # Accept a pre-built _Db2NativeAsyncConnectionPool (built by
        # create_db2_native_pool). Everything else — repo wiring,
        # settings, _closed, vector-indexing probe — inherits from
        # Db2Backend unchanged.
        super().__init__(pool, settings)


__all__ = [
    "Db2Backend",
    "Db2BackendNative",
    "Db2BranchRepository",
    "Db2CompressionRepository",
    "Db2ConsultationAuditRepository",
    "Db2FederationRepository",
    "Db2KGRepository",
    "Db2MemoryRepository",
    "Db2StateRepository",
    "Db2VersionRepository",
    "Db2WebhookRepository",
    "create_db2_pool",
    "create_db2_native_pool",
]
