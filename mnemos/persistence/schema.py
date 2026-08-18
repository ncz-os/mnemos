"""Runtime schema provisioning for MNEMOS persistence backends.

These helpers are DSN-aware: they run through the backend's configured pool
instead of the host-only installer ``sudo -u postgres psql`` path.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from pathlib import Path
from typing import Any

from mnemos.core.config import db2_vector_indexing_override, embedding_dim_env

_LOG = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_DIR = _REPO_ROOT / "mnemos" / "db_migrations"

_POSTGRES_LEGACY_MIGRATIONS: tuple[Path, ...] = (
    _DB_DIR / "migrations.sql",
    _DB_DIR / "migrations_v1_multiuser.sql",
    _DB_DIR / "migrations_v2_versioning.sql",
    _DB_DIR / "migrations_v2_sessions.sql",
    _DB_DIR / "migrations_model_registry.sql",
    _DB_DIR / "migrations_v3_dag.sql",
    _DB_DIR / "migrations_v3_graeae_unified.sql",
    _DB_DIR / "migrations_v3_webhooks.sql",
    _DB_DIR / "migrations_v3_oauth.sql",
    _DB_DIR / "migrations_v3_federation.sql",
    _DB_DIR / "migrations_v3_ownership.sql",
    _DB_DIR / "migrations_v3_1_compression.sql",
    _DB_DIR / "migrations_v3_1_versioning_fix.sql",
    _DB_DIR / "migrations_v3_1_2_kg_tenancy.sql",
    _DB_DIR / "migrations_v3_1_2_audit_log_columns.sql",
    _DB_DIR / "migrations_v3_2_user_namespace.sql",
    _DB_DIR / "migrations_v3_2_entities_namespace.sql",
    _DB_DIR / "migrations_v3_2_2_version_snapshot_new_values.sql",
    _DB_DIR / "migrations_v3_3_morpheus.sql",
    _DB_DIR / "migrations_v3_3_morpheus_namespace.sql",
    _DB_DIR / "migrations_v3_3_recall_tracking.sql",
    _DB_DIR / "migrations_charon_trigger_guard.sql",
    _DB_DIR / "migrations_v3_4_federation_compat.sql",
    _DB_DIR / "migrations_v3_5_trigger_same_memory_parent.sql",
    _DB_DIR / "migrations_v3_5_rls_group_select_unix_bits.sql",
    _DB_DIR / "migrations_v3_5_webhook_retry_terminal_state.sql",
    _DB_DIR / "migrations_v3_5_webhook_attempt_lease.sql",
    _DB_DIR / "migrations_v3_5_webhook_writer_revision.sql",
    _DB_DIR / "migrations_v3_5_webhook_status_updated_at.sql",
    _DB_DIR / "migrations_v3_5_webhook_superseded_marker.sql",
    _DB_DIR / "migrations_v3_5_webhook_attempt_unique.sql",
    _DB_DIR / "migrations_v3_5_webhook_succeeded_unique.sql",
    _DB_DIR / "migrations_v3_5_webhook_succeeded_terminal_trigger.sql",
    _DB_DIR / "migrations_v3_5_entities_namespace_unique.sql",
    _DB_DIR / "migrations_v3_5_state_journal_namespace.sql",
    _DB_DIR / "migrations_v3_5_session_compression_ratio_drop.sql",
    _DB_DIR / "migrations_v3_5_session_compression_legacy_drop.sql",
    _DB_DIR / "migrations_v3_5_sessions_consultations_namespace.sql",
    _DB_DIR / "migrations_v4_2_users_username.sql",
    _DB_DIR / "migrations_v4_2_compression_candidates_nullable_tokens.sql",
    _DB_DIR / "migrations_v4_2_state_value_text.sql",
    _DB_DIR / "migrations_v4_2_document_import_chunk_idempotency.sql",
    _DB_DIR / "migrations_v4_2_deletion_requests.sql",
    _DB_DIR / "migrations_v4_2_deletion_requests_blank_namespace_cleanup.sql",
    _DB_DIR / "migrations_v4_2_deletion_requests_soft_delete_columns.sql",
    _DB_DIR / "migrations_v4_2_deletion_requests_sweep_verifying.sql",
    _DB_DIR / "migrations_v4_2_compression_dag.sql",
    _DB_DIR / "migrations_v4_2_morpheus_consolidate.sql",
    _DB_DIR / "migrations_v4_2_morpheus_extract.sql",
    _DB_DIR / "migrations_v4_2_persephone.sql",
    _DB_DIR / "migrations_v4_2_pantheon_routing_audit.sql",
    _DB_DIR / "migrations_v5_0_consolidated_at.sql",
    _DB_DIR / "migrations_v5_0_morpheus_extract_run_memories.sql",
    _DB_DIR / "migrations_v5_0_2_artemis_dedup.sql",
    _DB_DIR / "migrations_v5_0_3_timestamp_tz_upgrade.sql",
    _DB_DIR / "migrations_v5_1_0_deletion_log.sql",
    _DB_DIR / "migrations_v5_2_0_nats_outbox_idempotency.sql",
    _DB_DIR / "migrations_v5_2_2_fts_gin_index.sql",
    _DB_DIR / "migrations_v5_3_3_deletion_log_export_index.sql",
    _DB_DIR / "migrations_v5_3_4_mcp_audit_log.sql",
    _DB_DIR / "migrations_v5_3_5_model_registry_capabilities_gin.sql",
)

_POSTGRES_NUMBERED_DIR = _DB_DIR / "migrations"
_ORACLE_MIGRATIONS_DIR = _DB_DIR / "migrations_oracle"
_DB2_MIGRATIONS_DIR = _DB_DIR / "migrations_db2"

_PG_DUPLICATE_STATES = {"42710", "42P07", "42701"}
_PG_UNDEFINED_STATES = {"42704", "42P01"}
# SQLSTATEs that are benign on (re)provisioning: object/column already exists
# (42710/42P07/42701/42711) and 01550 = SQL0605W "index not created because a
# matching index already exists" (e.g. an explicit CREATE INDEX that duplicates
# the index a UNIQUE/PK constraint already created). Warnings, not failures.
#
# 23505 (unique_violation) used to be in this set on Db2 (and previously
# on Postgres via _PG_DUPLICATE_STATES) because a static seed INSERT was
# replayed on every startup with no migration ledger. That blanket
# suppression also masked genuine uniqueness failures from CREATE INDEX
# and ALTER TABLE ADD CONSTRAINT. The fix in
# ``_is_benign_{postgres,oracle,db2}_error`` now matches 23505 ONLY
# against INSERT INTO <seed_table> statements; constraint / index
# uniqueness failures surface as hard migration failures again.
_DB2_BENIGN_STATES = {"42710", "42P07", "42701", "42711", "01550"}
_DB2_VECTOR_INDEX_BENIGN_CODES = {"42601", "56098", "SQL0104N", "SQL0270N"}


def resolve_embedding_dim(settings: Any | None = None) -> int:
    """Resolve the embedding dimension used for schema DDL."""
    try:
        raw = getattr(getattr(settings, "database", None), "embedding_dim")
    except AttributeError:
        raw = None
    if raw is None:
        return embedding_dim_env()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return embedding_dim_env()


def postgres_migration_paths() -> list[Path]:
    """Return the full Postgres runtime schema order."""
    numbered = sorted(_POSTGRES_NUMBERED_DIR.glob("*.sql"))
    paths = [path for path in _POSTGRES_LEGACY_MIGRATIONS if path.exists()]
    seen = {path.resolve() for path in paths}
    for path in numbered:
        resolved = path.resolve()
        if resolved not in seen:
            paths.append(path)
            seen.add(resolved)
    return paths


def oracle_migration_paths() -> list[Path]:
    return sorted(_ORACLE_MIGRATIONS_DIR.glob("*.sql"))


def db2_migration_paths() -> list[Path]:
    return sorted(_DB2_MIGRATIONS_DIR.glob("*.sql"))


def render_migration_sql(path: Path, *, backend: str, embedding_dim: int) -> str:
    """Read a migration and apply backend runtime substitutions."""
    sql = path.read_text(encoding="utf-8")
    replacements = {
        "{{embedding_dim}}": str(embedding_dim),
        "{{vector_pct_comp}}": "15",
        "{{vector_build_parallelism}}": str(_default_build_parallelism()),
        "{{vector_build_mem_budget}}": "4",
    }
    for needle, value in replacements.items():
        sql = sql.replace(needle, value)
    if backend == "postgres" and path.name == "migrations.sql":
        sql = re.sub(r"embedding\s+vector\(\s*\d+\s*\)", f"embedding vector({embedding_dim})", sql)
        sql = re.sub(
            r"USING\s+ivfflat\s*\(\s*embedding\s+vector_cosine_ops\s*\)",
            "USING hnsw (embedding vector_cosine_ops)",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


def split_postgres_statements(sql: str) -> list[str]:
    return _split_semicolon_statements(sql)


def split_oracle_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_plsql = False
    for raw in sql.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith(("WHENEVER ", "PROMPT ", "REM ", "SET DEFINE ")):
            continue
        if upper.startswith(("DECLARE", "BEGIN", "CREATE OR REPLACE TRIGGER", "CREATE OR REPLACE FUNCTION")):
            in_plsql = True
        if stripped == "/" and in_plsql:
            block = "\n".join(buffer).strip()
            if block and _has_executable_sql(block):
                statements.append(block)
            buffer = []
            in_plsql = False
            continue
        buffer.append(line)
        if stripped.endswith(";") and not in_plsql:
            statement = _strip_trailing_semicolon("\n".join(buffer).strip())
            if statement and _has_executable_sql(statement):
                statements.append(statement)
            buffer = []
    tail = "\n".join(buffer).strip()
    if tail and _has_executable_sql(tail):
        statements.append(_strip_trailing_semicolon(tail))
    return statements


def split_db2_statements(sql: str) -> list[str]:
    if _uses_db2_at_terminator(sql):
        return _split_db2_at_statements(sql)
    return _split_semicolon_statements(sql)


async def ensure_postgres_schema(pool: Any, settings: Any | None = None) -> None:
    """Apply the full Postgres schema on the configured asyncpg pool."""
    embedding_dim = resolve_embedding_dim(settings)
    if not 1 <= embedding_dim <= 2000:
        raise RuntimeError(
            f"MNEMOS_EMBEDDING_DIM={embedding_dim} is outside the supported pgvector HNSW vector index range [1, 2000]."
        )

    async with pool.acquire() as conn:
        for path in postgres_migration_paths():
            sql = render_migration_sql(path, backend="postgres", embedding_dim=embedding_dim)
            in_transaction = False
            for statement in split_postgres_statements(sql):
                head = _statement_head(statement).upper()
                is_tx_control = head in {"BEGIN", "COMMIT", "ROLLBACK"}
                await _execute_postgres_statement(
                    conn,
                    statement,
                    path,
                    in_transaction=in_transaction and not is_tx_control,
                )
                if head == "BEGIN":
                    in_transaction = True
                elif head in {"COMMIT", "ROLLBACK"}:
                    in_transaction = False
        await _ensure_postgres_embedding_shape(conn, embedding_dim)


async def ensure_oracle_schema(pool: Any, settings: Any | None = None) -> None:
    """Apply every Oracle migration through the configured Oracle pool."""
    embedding_dim = resolve_embedding_dim(settings)
    async with pool.acquire() as conn:
        cursor = await _maybe_await(conn.cursor)
        try:
            for path in oracle_migration_paths():
                sql = render_migration_sql(path, backend="oracle", embedding_dim=embedding_dim)
                for statement in split_oracle_statements(sql):
                    await _execute_cursor_statement(cursor, statement, path, "oracle")
            await _commit_if_available(conn)
        finally:
            await _maybe_await(cursor.close)


async def ensure_db2_schema(pool: Any, settings: Any | None = None) -> None:
    """Apply every Db2 migration through the configured Db2 pool."""
    embedding_dim = resolve_embedding_dim(settings)
    async with pool.acquire() as conn:
        for path in db2_migration_paths():
            sql = render_migration_sql(path, backend="db2", embedding_dim=embedding_dim)
            for statement in split_db2_statements(sql):
                cursor = conn.cursor()
                try:
                    await _execute_cursor_statement(cursor, statement, path, "db2")
                finally:
                    await _maybe_await(cursor.close)
            await _commit_if_available(conn)


async def _execute_postgres_statement(conn: Any, statement: str, path: Path, *, in_transaction: bool) -> None:
    if statement.lstrip().upper().startswith("GRANT "):
        _LOG.debug("Skipping runtime GRANT in %s; schema owner DSNs do not require installer role grants.", path.name)
        return
    if in_transaction:
        await conn.execute("SAVEPOINT mnemos_schema_replay")
    try:
        await conn.execute(statement)
    except Exception as exc:
        if _is_benign_postgres_error(statement, exc):
            if in_transaction:
                await conn.execute("ROLLBACK TO SAVEPOINT mnemos_schema_replay")
                await conn.execute("RELEASE SAVEPOINT mnemos_schema_replay")
            _LOG.debug("Skipping idempotent Postgres migration replay in %s: %s", path.name, _first_line(exc))
            return
        raise RuntimeError(
            f"Postgres schema migration {path.name} failed at `{_statement_head(statement)}`: {exc}"
        ) from exc
    else:
        if in_transaction:
            await conn.execute("RELEASE SAVEPOINT mnemos_schema_replay")


async def _ensure_postgres_embedding_shape(conn: Any, embedding_dim: int) -> None:
    current_type = await conn.fetchval(
        """
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = 'memories'::regclass
          AND attname = 'embedding'
          AND NOT attisdropped
        """
    )
    target_type = f"vector({embedding_dim})"
    if current_type != target_type:
        non_null_rows = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        if int(non_null_rows or 0) > 0:
            raise RuntimeError(
                f"memories.embedding is {current_type or 'missing'} but MNEMOS_EMBEDDING_DIM={embedding_dim}; "
                "refusing to resize a populated embedding column. Re-embed or clear embeddings before changing dims."
            )
        await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding")
        await conn.execute(f"ALTER TABLE memories ALTER COLUMN embedding TYPE {target_type} USING NULL")

    index_method = await conn.fetchval(
        """
        SELECT am.amname
        FROM pg_class idx
        JOIN pg_index i ON i.indexrelid = idx.oid
        JOIN pg_am am ON am.oid = idx.relam
        WHERE idx.relname = 'idx_memories_embedding'
        """
    )
    if index_method != "hnsw":
        await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops)"
        )


#: ``SQL0668N ... reason code "7" on table "SCHEMA.TABLE"`` -- Db2 has put the
#: table in reorg-pending state and refuses further operations on it until a
#: REORG runs.
_DB2_REORG_PENDING_RE = re.compile(
    r'SQL0668N.*?reason code\s*"7".*?on table\s*"([A-Za-z0-9_$#.]+)"',
    re.IGNORECASE | re.DOTALL,
)


def _db2_reorg_pending_table(exc: Exception) -> str | None:
    """Return the table Db2 wants reorganised, if that is what failed."""
    match = _DB2_REORG_PENDING_RE.search(str(exc))
    return match.group(1) if match else None


async def _execute_cursor_statement(cursor: Any, statement: str, path: Path, backend: str) -> None:
    if backend == "db2" and _should_skip_db2_vector_index(statement):
        _LOG.warning("Skipping Db2 vector index creation because DB2_VECTOR_INDEXING is not enabled.")
        return
    try:
        await _maybe_await(cursor.execute, statement)
    except Exception as exc:
        if backend == "oracle" and _is_benign_oracle_error(statement, exc):
            _LOG.debug("Skipping idempotent Oracle migration replay in %s: %s", path.name, _first_line(exc))
            return
        if backend == "db2" and _is_benign_db2_error(statement, exc):
            _LOG.debug("Skipping idempotent Db2 migration replay in %s: %s", path.name, _first_line(exc))
            return
        # Db2 puts a table into reorg-pending after REORG-recommended ALTERs
        # and then refuses everything else on it. 0050_lifecycle_workers.sql
        # issues nine consecutive ALTERs against deletion_requests and then
        # creates an index on it, so a real 6.0.1 database could not migrate
        # to 6.1 at all:
        #
        #   SQL0668N Operation not allowed for reason code "7" on table
        #   "DB2INST1.DELETION_REQUESTS"
        #
        # Reorganise and retry once. This is deliberately handled here rather
        # than by adding a REORG to the .sql: migrations carry no
        # applied-state table and are replayed on every start, so an
        # unconditional REORG would rewrite the table on every boot. Doing it
        # on the error means it runs only when Db2 actually asks for it.
        if backend == "db2":
            table = _db2_reorg_pending_table(exc)
            if table:
                _LOG.warning(
                    "Db2 reports %s in reorg-pending state during %s; reorganising and retrying.",
                    table,
                    path.name,
                )
                await _maybe_await(cursor.execute, f"CALL SYSPROC.ADMIN_CMD('REORG TABLE {table}')")
                await _maybe_await(cursor.execute, statement)
                return
        raise RuntimeError(
            f"{backend.upper()} schema migration {path.name} failed at `{_statement_head(statement)}`: {exc}"
        ) from exc


def _split_semicolon_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    dollar_tag: str | None = None
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    i = 0

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            buffer.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buffer.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buffer.append(ch)
                i += 1
            continue
        if in_single:
            buffer.append(ch)
            if ch == "'" and nxt == "'":
                buffer.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buffer.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            buffer.extend((ch, nxt))
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            buffer.extend((ch, nxt))
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            buffer.append(ch)
            in_single = True
            i += 1
            continue
        if ch == '"':
            buffer.append(ch)
            in_double = True
            i += 1
            continue
        if ch == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            statement = "".join(buffer).strip()
            if statement and _has_executable_sql(statement):
                statements.append(statement)
            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail and _has_executable_sql(tail):
        statements.append(tail)
    return statements


def _split_db2_at_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for raw in sql.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--#SET TERMINATOR"):
            continue
        if stripped == "@":
            block = "\n".join(buffer).strip()
            if block and _has_executable_sql(block):
                statements.append(block)
            buffer = []
            continue
        if stripped.endswith("@"):
            buffer.append(raw.rstrip()[:-1])
            block = "\n".join(buffer).strip()
            if block and _has_executable_sql(block):
                statements.append(block)
            buffer = []
            continue
        buffer.append(raw)
    tail = "\n".join(buffer).strip()
    if tail and _has_executable_sql(tail):
        statements.append(tail)
    return statements


def _uses_db2_at_terminator(sql: str) -> bool:
    return any(
        line.strip().startswith("--#SET TERMINATOR @") or line.strip().endswith("@") for line in sql.splitlines()
    )


def _has_executable_sql(statement: str) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in statement.splitlines())


def _strip_trailing_semicolon(statement: str) -> str:
    stripped = statement.strip()
    return stripped[:-1].rstrip() if stripped.endswith(";") else stripped


def _is_benign_postgres_error(statement: str, exc: Exception) -> bool:
    state = getattr(exc, "sqlstate", "") or getattr(exc, "pgcode", "")
    head = _sql_head(statement)
    if state in _PG_UNDEFINED_STATES and head.startswith(("GRANT ", "DROP POLICY ")):
        return True
    # 23505 (unique_violation) is ONLY benign for replay of a static
    # seed INSERT into one of the curated seed tables; never for
    # constraint or index creation. The earlier blanket suppression
    # masked real failures: a CREATE INDEX / ALTER TABLE ADD CONSTRAINT
    # whose unique-index collision was actually a logic bug now surfaces
    # as a hard migration failure instead of being silently swallowed.
    if state == "23505" and head.startswith("INSERT "):
        return _is_idempotent_seed_insert(head)
    return False


def _sql_head(statement: str) -> str:
    """Uppercased statement with leading SQL comments and blank lines removed.

    Migration files open with `--` banner comments, and the splitter hands the
    comment block to the executor along with the statement it precedes. A bare
    `statement.lstrip().upper()` therefore starts with "---", so any guard using
    `startswith("ALTER ")` / `startswith("INSERT ")` silently never matched on a
    real migration -- only in unit tests that passed a bare statement.

    That is how a benign-error guard can pass its own tests and still not fire
    in production: 0050_lifecycle_workers.sql aborted a fresh Oracle 6.1 install
    even with ORA-01451 listed as benign.
    """
    for line in statement.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.upper()
    return ""


def _is_benign_oracle_error(statement: str, exc: Exception) -> bool:
    text = str(exc)
    head = _sql_head(statement)
    if any(
        code in text
        for code in (
            "ORA-00955",
            "ORA-01408",
            "ORA-01430",
            "ORA-02260",
            "ORA-02261",
            "ORA-02275",
            "ORA-04081",
        )
    ):
        return True
    # ORA-01451 / ORA-01442: the column is ALREADY in the nullability the
    # statement asks for. Oracle rejects the no-op where Postgres accepts
    # `DROP NOT NULL` idempotently, so a forward-only migration that relaxes a
    # column aborts the moment the column is already relaxed. That is the
    # desired end state, not a failure.
    #
    # Scoped to ALTER, like ORA-00001 is scoped to INSERT below: these codes
    # only describe an already-satisfied nullability change, and blanket
    # forgiveness would hide genuine errors elsewhere.
    #
    # Found by running the 6.1 migrations against Oracle 23.26.1-ee: a FRESH
    # schema failed on 0050_lifecycle_workers.sql at
    # `ALTER TABLE deletion_requests MODIFY (memory_id NULL)`, so a clean
    # Oracle install of 6.1 could not complete provisioning at all.
    if ("ORA-01451" in text or "ORA-01442" in text) and head.startswith("ALTER "):
        return True
    # ORA-00001 (unique constraint violated) is ONLY benign for replay of
    # a static seed INSERT into a curated seed table; never for
    # constraint / index creation.
    if "ORA-00001" in text and head.startswith("INSERT "):
        return _is_idempotent_seed_insert(head)
    return False


def _is_benign_db2_error(statement: str, exc: Exception) -> bool:
    text = str(exc)
    head = _sql_head(statement)
    if any(state in text for state in _DB2_BENIGN_STATES):
        return True
    if "CREATE VECTOR INDEX" in statement.upper() and any(code in text for code in _DB2_VECTOR_INDEX_BENIGN_CODES):
        return True
    # 23505 (unique_violation) on Db2 is only benign for replay of a
    # static seed INSERT; the original blanket suppression let genuine
    # CREATE INDEX / ADD CONSTRAINT failures (e.g. a partial-then-recovered
    # prior run leaving a half-built unique index) silently pass.
    if "23505" in text and head.startswith("INSERT "):
        return _is_idempotent_seed_insert(head)
    return False


#: Seed tables whose static INSERT statements are safe to replay (a
#: unique_violation on these means "this exact row was already inserted
#: by a prior run" and is benign). Restricted to the seeded-reference
#: tables; identity / memory / state / kg rows are inserted by the
#: application, not by migrations, so they are never on this list.
_IDEMPOTENT_SEED_TABLES: frozenset[str] = frozenset(
    {
        "subscription_plans",
        "memory_category_decay",
        "model_registry",
    }
)


def _is_idempotent_seed_insert(head: str) -> bool:
    """True only when ``head`` is an ``INSERT INTO <table> ...`` against
    one of the curated seed tables. The parser is intentionally
    conservative -- it only matches the table identifier immediately
    after ``INSERT INTO`` and rejects anything else (CTEs, ``INSERT
    INTO ... SELECT``, multi-table INSERTs, etc.).
    """
    if not head.startswith("INSERT "):
        return False
    rest = head[len("INSERT ") :].lstrip()
    if not rest.upper().startswith("INTO "):
        return False
    rest = rest[len("INTO ") :].lstrip()
    # Match the bare table identifier; strip trailing characters that
    # belong to a column list or VALUES clause.
    ident_chars = []
    for ch in rest:
        if ch.isalnum() or ch == "_":
            ident_chars.append(ch)
        else:
            break
    if not ident_chars:
        return False
    table = "".join(ident_chars).lower()
    return table in _IDEMPOTENT_SEED_TABLES


def _should_skip_db2_vector_index(statement: str) -> bool:
    if "CREATE VECTOR INDEX" not in statement.upper():
        return False
    raw = db2_vector_indexing_override()
    return raw is not None and raw.strip().upper() not in {"YES", "ON", "TRUE", "1"}


async def _maybe_await(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs) if callable(value) else value
    return await result if inspect.isawaitable(result) else result


async def _commit_if_available(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if commit is not None:
        await _maybe_await(commit)


def _default_build_parallelism() -> int:
    cores = os.cpu_count() or 4
    return max(2, int(cores * 0.75))


def _statement_head(statement: str) -> str:
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return stripped[:120]
    return statement.strip()[:120]


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


__all__ = [
    "db2_migration_paths",
    "ensure_db2_schema",
    "ensure_oracle_schema",
    "ensure_postgres_schema",
    "oracle_migration_paths",
    "postgres_migration_paths",
    "render_migration_sql",
    "resolve_embedding_dim",
    "split_db2_statements",
    "split_oracle_statements",
    "split_postgres_statements",
]
