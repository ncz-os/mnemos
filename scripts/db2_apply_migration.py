"""Apply db/migrations_db2 SQL to a target Db2 instance.

Db2 migration files use one of THREE statement terminators with no
``--#SET TERMINATOR`` directive to declare it:

* ``@`` — Db2 CLP compound-SQL convention (e.g. 0001, 0038)
* ``%`` — compound ``BEGIN ... EXECUTE IMMEDIATE ... END%`` wrappers
  (e.g. 0021-0024, 0036, 0040)
* ``;`` — plain DDL (the remaining files)

The applier auto-detects the terminator per file and splits the script
with a single-quote-string- and comment-aware scanner so a terminator
inside a literal or comment never splits a statement. Each statement is
executed independently; SQLSTATEs that indicate idempotent replay
(object/column already exists, or a registry-var warning) are logged but
not fatal. ``ibm_db`` success-with-warning is treated as success.

Usage::

    .venv/bin/python scripts/db2_apply_migration.py --all \\
        --dsn 'db2://db2inst1:pwd@host:50000/MNEMOS'
    .venv/bin/python scripts/db2_apply_migration.py --file db/migrations_db2/0004_oauth_providers.sql --dsn '...'
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get("DB2_PROOF_DSN", "db2://db2inst1:mnemos_dev@192.168.207.67:50000/MNEMOS")
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations_db2"
DEFAULT_FILE = MIGRATIONS_DIR / "0001_core_schema.sql"

# SQLSTATEs treated as idempotent-replay signals (logged, not fatal):
#   42710 — object name already exists
#   42P07 — duplicate_table (libpq compat code some drivers surface)
#   42711 — duplicate column name (idempotent ALTER ... ADD COLUMN replay)
#   56098 — operation needs a registry var (e.g. DB2_VECTOR_INDEXING)
# NOTE: 42601 / SQL0104N / SQL0270N are NOT benign — they previously masked
# real multi-statement mis-splits. With per-file terminator detection a true
# syntax error must surface.
BENIGN_SQLSTATES = {"42710", "42P07", "42711", "56098"}


def _detect_terminator(sql: str) -> str:
    """Return the statement terminator used by this script: ``@``, ``%`` or ``;``.

    Scans for a bare ``@`` or ``%`` occurring OUTSIDE single-quoted string
    literals and comments (those chars are only ever terminators in these
    migrations; a ``'foo@'`` literal must not trigger detection). Defaults
    to ``;``. Assumes one terminator per file (true for db/migrations_db2).
    """
    i, n, in_s = 0, len(sql), False
    saw_at = saw_pct = False
    while i < n:
        c = sql[i]
        if in_s:
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                in_s = False
            i += 1
            continue
        if sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql[i:i + 2] == "/*":
            j = sql.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "'":
            in_s = True
            i += 1
            continue
        if c in ("@", "%"):
            # terminator only if the rest of the physical line is whitespace
            j = i + 1
            while j < n and sql[j] in " \t\r":
                j += 1
            if j >= n or sql[j] == "\n":
                if c == "@":
                    saw_at = True
                else:
                    saw_pct = True
        i += 1
    if saw_at:
        return "@"
    if saw_pct:
        return "%"
    return ";"


def _is_executable(block: str) -> bool:
    import re

    body = re.sub(r"/\*[\s\S]*?\*/", "", block)
    return any(line.strip() and not line.strip().startswith("--") for line in body.splitlines())


def _split_statements(sql: str) -> list[str]:
    """Split a Db2 migration script on its auto-detected terminator.

    Single-quoted string literals (with ``''`` escaping), ``--`` line
    comments and ``/* */`` block comments are scanned so a terminator
    character inside them does not split a statement.
    """
    term = _detect_terminator(sql)
    stmts: list[str] = []
    buf: list[str] = []
    i, n, in_s = 0, len(sql), False
    while i < n:
        c = sql[i]
        if in_s:
            buf.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_s = False
            i += 1
            continue
        if sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            buf.append(sql[i:j])
            i = j
            continue
        if sql[i:i + 2] == "/*":
            j = sql.find("*/", i)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        if c == "'":
            in_s = True
            buf.append(c)
            i += 1
            continue
        if c == term:
            block = "".join(buf).strip()
            if block and _is_executable(block):
                stmts.append(block)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail and _is_executable(tail):
        stmts.append(tail)
    return stmts


async def _apply_one(conn, path: Path, embedding_dim: int) -> int:
    import ibm_db_dbi

    sql = path.read_text().replace("{{embedding_dim}}", str(embedding_dim))
    statements = _split_statements(sql)
    failures = 0
    for i, stmt in enumerate(statements, 1):
        head = " ".join(stmt.split())[:70]
        cur = conn.cursor()
        try:
            await cur.execute(stmt)
        except ibm_db_dbi.Warning:
            pass  # success with warning
        except Exception as exc:
            import re

            msg = str(exc).splitlines()[0]
            m = re.search(r"SQLSTATE=([0-9A-Za-z]{5})", msg)
            sqlstate = m.group(1) if m else None
            benign = sqlstate in BENIGN_SQLSTATES if sqlstate else any(code in msg for code in BENIGN_SQLSTATES)
            if not benign:
                failures += 1
                print(f"  [{path.name} {i:2d}] FAIL {head} -- {msg[:90]}")
        finally:
            await cur.close()
    await conn.commit()
    print(f"  [{path.name}] {len(statements)} stmts applied ({failures} failures)")
    return failures


async def _apply(dsn: str, paths: list[Path], embedding_dim: int = 768) -> int:
    from mnemos.persistence.db2 import create_db2_pool

    pool = await create_db2_pool(dsn, min_size=1, max_size=2)
    total_fail = 0
    try:
        async with pool.acquire() as conn:
            for p in paths:
                total_fail += await _apply_one(conn, p, embedding_dim)
    finally:
        await pool.close()
    return total_fail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--file", default=None, help="single migration file")
    ap.add_argument("--all", action="store_true", help="apply every db/migrations_db2/*.sql in order")
    ap.add_argument("--dim", type=int, default=768, help="VECTOR embedding dim for {{embedding_dim}}. Default 768.")
    args = ap.parse_args()

    if args.all:
        paths = [Path(p) for p in sorted(glob.glob(str(MIGRATIONS_DIR / "*.sql")))]
    elif args.file:
        paths = [Path(args.file)]
    else:
        paths = [DEFAULT_FILE]

    fail = asyncio.run(_apply(args.dsn, paths, embedding_dim=args.dim))
    print(f"=== total failures: {fail} ===")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
