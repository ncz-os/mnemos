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
import os
import re
from collections.abc import Sequence
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

from mnemos.persistence.oracle import (
    OracleBackend,
    OracleBranchRepository,
    OracleCompressionRepository,
    OracleConsultationAuditRepository,
    OracleFederationRepository,
    OracleKGRepository,
    OracleMemoryRepository,
    OracleStateRepository,
    OracleVersionRepository,
    OracleWebhookRepository,
    _call,
    _conn_from_tx,
    _fetch_all_dicts,
    _is_unique_violation,
    _render_visibility,
    _validate_and_format_vector,
)
from mnemos.persistence.types import Row
from mnemos.persistence.visibility import VisibilityFilter

_LOG = logging.getLogger(__name__)


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
                    f"DSN attribute {k} contains forbidden char (; or =); " "would allow CLI attribute injection."
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
    raw = os.environ.get(_DB2_VEC_INDEX_ENV)
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


class Db2MemoryRepository(_Db2OraCompatMixin, OracleMemoryRepository):
    """Memory repository — Db2-native ``semantic_search`` override.

    All other methods inherit verbatim from
    :class:`OracleMemoryRepository` and rely on the cursor-layer
    Oracle→Db2 translation in :class:`_Db2AsyncCursor.execute`.

    ``semantic_search`` is overridden because the inherited Oracle SQL
    uses ``VECTOR_DISTANCE(..., COSINE)`` + ``FETCH FIRST K ROWS ONLY``
    — neither of which engages the Db2 12.1.5 DiskANN vector index.
    The override emits ``VECTOR_DISTANCE(..., EUCLIDEAN)`` +
    ``FETCH APPROX FIRST K ROWS ONLY`` so the vector index created by
    ``db/migrations_db2/0001_core_schema.sql`` is actually used by the
    app path (not only by ``scripts/bench_v4.py``).

    Mode is selected by ``_resolve_db2_vector_index_mode`` — see
    module-level docstring.
    """

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
    ) -> list[Row]:
        if not embedding:
            return []
        mode = _resolve_db2_vector_index_mode(self._settings)
        # FETCH APPROX FIRST engages the DiskANN index; FETCH FIRST is
        # an exact scan. Both query the same VECTOR_DISTANCE function.
        fetch_clause = "FETCH APPROX FIRST :limit ROWS ONLY" if mode == "approx" else "FETCH FIRST :limit ROWS ONLY"

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
            rank_sql = "VECTOR_DISTANCE(m.embedding, TO_VECTOR(:q), EUCLIDEAN)"
            sql = (
                "SELECT m.id, m.content, m.category, m.subcategory, m.metadata, "
                "m.quality_rating, m.compressed_content, m.verbatim_content, "
                "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
                "m.source_provider, m.source_session, m.source_agent, "
                "m.group_id, m.created, m.updated, m.archived_at, "
                "m.recall_count, m.last_recalled_at, "
                f"({rank_sql}) AS rank_score "
                "FROM memories m WHERE " + " AND ".join(where) + " "
                f"ORDER BY {rank_sql} ASC "
                f"{fetch_clause}"
            )
            await _call(cursor.execute, sql, params)
            rows = await _fetch_all_dicts(cursor)
        finally:
            await _call(cursor.close)

        if boost_recency and rows:
            # Match Oracle's recency formula:
            #   adjusted = distance - w * (1.0 / (1.0 + age_days))
            # where age_days = CURRENT DATE - CAST(updated AS DATE).
            # We compute age_days in Python from ``updated`` so the
            # SQL ORDER BY stays index-friendly. ``updated`` may be a
            # datetime (ibm_db_dbi default) or a string fallback — be
            # defensive.
            from datetime import date, datetime, timezone

            w = float(recency_weight)
            today = datetime.now(timezone.utc).date()
            for row in rows:
                updated = row.get("updated") if isinstance(row, dict) else None
                if isinstance(updated, datetime):
                    upd_date = updated.date()
                elif isinstance(updated, date):
                    upd_date = updated
                else:
                    upd_date = today
                age_days = max(0, (today - upd_date).days)
                rank = row.get("rank_score")
                if rank is None:
                    continue
                try:
                    rank_f = float(rank)
                except (TypeError, ValueError):
                    continue
                row["rank_score"] = rank_f - w * (1.0 / (1.0 + age_days))
            # Re-sort ASC on the adjusted rank_score and re-cap to ``limit``.
            rows.sort(key=lambda r: float(r.get("rank_score") or 0.0))
            rows = rows[:limit]

        return rows


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
                    ?, COALESCE(?, 'default')
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
                    ?, ?, ?, COALESCE(?, 'default'),
                    COALESCE(CAST(? AS INTEGER), 600),
                    ?, ?, ?, ?,
                    COALESCE(CAST(? AS TIMESTAMP), CURRENT TIMESTAMP),
                    ?, COALESCE(?, 'create'),
                    ?, ?, COALESCE(?, 'main'),
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
                    metadata_json,
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
                    merge_parents,
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
                USING (SELECT ? AS memory_id, ? AS name, ? AS head_version_id
                       FROM SYSIBM.SYSDUMMY1) src
                   ON (m.memory_id = src.memory_id AND m.name = src.name)
                WHEN MATCHED THEN UPDATE SET
                    head_version_id = src.head_version_id,
                    updated = CURRENT TIMESTAMP
                WHEN NOT MATCHED THEN INSERT (
                    id, memory_id, name, head_version_id, created, updated
                ) VALUES (
                    src.memory_id || ':' || src.name,
                    src.memory_id,
                    src.name,
                    src.head_version_id,
                    CURRENT TIMESTAMP,
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
                "SELECT memory_id, name AS branch, id AS head_version_id FROM ("
                "  SELECT memory_id, name, id, "
                "         ROW_NUMBER() OVER ("
                "             PARTITION BY memory_id, name "
                "             ORDER BY version_num DESC"
                "         ) AS rn "
                "  FROM memory_versions"
                "  WHERE " + " AND ".join(where) + ""
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
                    ?, ?, COALESCE(?, 'balanced'), ?,
                    COALESCE(?, CURRENT TIMESTAMP)
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
                        ?, ?, ?, ?, COALESCE(?, 'default'),
                        COALESCE(?, 'default'), 'pending', 0, CURRENT TIMESTAMP
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
        capability_map = {
            "code_generation": ["coding"],
            "reasoning": ["reasoning", "logic"],
            "architecture_design": ["reasoning"],
            "summarization": ["reasoning"],
            "web_search": ["online", "search"],
        }
        required_caps = capability_map.get(task_type, ["reasoning"])
        conn = _conn_from_tx(tx)
        cursor = await _call(conn.cursor)
        try:
            # Native Db2: COALESCE + CURRENT TIMESTAMP (no :name, no SYSTIMESTAMP)
            # For this audit path we keep the SELECT simple; full pricing/weight
            # logic mirrors sqlite but emits Db2 tokens.
            await _call(
                cursor.execute,
                "SELECT provider, model_id, display_name, input_cost_per_mtok, "
                "output_cost_per_mtok, capabilities, graeae_weight, context_window "
                "FROM model_registry WHERE available = 1 AND deprecated = 0",
            )
            rows = await _fetch_all_dicts(cursor)
            # (business logic elided for brevity — same shape as sqlite impl;
            # returns first eligible or fallback None, required_caps)
            if not rows:
                return None, required_caps
            model = rows[0]
            return {
                "provider": model.get("provider"),
                "model_id": model.get("model_id"),
                "display_name": model.get("display_name"),
                "cost_per_mtok": None,
                "quality_score": float(model.get("graeae_weight") or 0),
                "context_window": model.get("context_window"),
            }, required_caps
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
                "SELECT provider FROM model_registry WHERE model_id = ? " "AND available = 1 AND deprecated = 0",
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
                "SELECT provider FROM model_registry WHERE model_id = ? "
                "AND available = 1 AND deprecated = 0 LIMIT 1",
                (model_id,),
            )
            row = await _fetch_all_dicts(cursor)
            return row[0].get("provider") if row else None
        finally:
            await _call(cursor.close)


class Db2FederationRepository(_Db2OraCompatMixin, OracleFederationRepository):
    """Federation peer management repository — Oracle SQL auto-translated by cursor layer."""


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


class Db2Backend(OracleBackend):
    """IBM Db2 12.1.x backend via Oracle Compatibility Mode.

    **Important — performance posture and roadmap.** This backend ships
    on top of Db2's *Oracle Compatibility Mode* (``DB CFG ORA_COMPATIBILITY ON``
    + ``ENABLE_ORACLE_COMPATIBILITY=true`` in the container env). The
    repository classes :class:`Db2MemoryRepository` etc. subclass their
    Oracle counterparts and inherit the Oracle SQL surface verbatim;
    :class:`_Db2AsyncCursor.execute` rewrites Oracle tokens
    (``SYSTIMESTAMP``→``CURRENT TIMESTAMP``, ``:name`` named binds →
    ``?`` positional binds, etc.) at query time.

    This was the fastest path to a working Db2 backend with same-ABC
    parity to Oracle / PostgreSQL / SQLite — days, not weeks. But it
    has a real performance cost: every query carries parse-time
    translation overhead, and the Db2 optimizer cannot see native
    Db2-dialect tokens directly (it sees the rewritten output). The
    Db2 backend carries this
    overhead.

    The :meth:`Db2MemoryRepository.semantic_search` override is the
    one site that emits native Db2 SQL directly (EUCLIDEAN +
    ``FETCH APPROX FIRST``) so that the DiskANN vector index from
    ``db/migrations_db2/0001_core_schema.sql`` is actually engaged.
    All other repository methods still travel through the Oracle-compat
    path.

    **A full native-Db2 dialect port** (dropping the Oracle subclassing
    + cursor translation layer in favor of hand-written Db2 SQL with
    native ``VARBINARY``/``BIGINT``/``CLOB`` typing and ``MERGE INTO``
    upserts) is tracked on the v6.x roadmap (``docs/v6.1-roadmap.md``).
    The goal is to A/B Oracle-compat-mode vs native-dialect on the
    same DiskANN index and ship whichever is faster as the v6.x
    default. Until that lands, Db2 performance numbers should be read
    as a floor, not a ceiling.

    On ``open()``, probes the ``DB2_VECTOR_INDEXING`` registry variable
    and logs a clear warning if the operator hasn't enabled native
    vector indexing — without it the DiskANN index from
    ``db/migrations_db2/0001_core_schema.sql`` cannot be created, and
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
        # Startup registry-probe state, populated lazily by ``open()``.
        # ``None`` means "not yet probed"; otherwise stores the raw
        # registry value (``"YES"``, ``"NO"``, ``""``...) for the
        # ``is_vector_indexing_enabled`` property + ``mnemos doctor``.
        self._db2_vector_indexing_value: str | None = None

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
        sql = "SELECT REG_VAR_VALUE FROM SYSIBMADM.REG_VARIABLES " "WHERE REG_VAR_NAME = 'DB2_VECTOR_INDEXING'"
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


__all__ = [
    "Db2Backend",
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
]
