"""Apply db/migrations_oracle/0001_core_schema.sql to a target Oracle DB.

Parses the SQL file by splitting on sqlplus-style PL/SQL terminators
(``/`` on its own line) and statement-end ``;``. Idempotent — the
migration is PL/SQL-guarded and safe to replay.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get("ORACLE_PROOF_DSN", "oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1")
DEFAULT_FILE = REPO_ROOT / "mnemos" / "db_migrations" / "migrations_oracle" / "0001_core_schema.sql"


def _split_statements(sql: str) -> list[str]:
    """Split sqlplus-style script into individual statements.

    PL/SQL blocks end with ``/`` on its own line (sqlplus convention).
    Plain DDL/DML statements end with ``;``. We honour both: scan
    line-by-line, buffer until a terminator, then flush.
    """
    stmts: list[str] = []
    buf: list[str] = []
    in_plsql = False
    for raw in sql.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # Skip comment-only lines
        if not stripped or stripped.startswith("--"):
            buf.append(line)
            continue
        # PL/SQL block start
        if stripped.upper().startswith(
            (
                "DECLARE",
                "BEGIN",
                "CREATE OR REPLACE PROCEDURE",
                "CREATE OR REPLACE FUNCTION",
                "CREATE OR REPLACE PACKAGE",
            )
        ):
            in_plsql = True
        if stripped == "/" and in_plsql:
            block = "\n".join(buf).strip()
            if block:
                stmts.append(block)
            buf = []
            in_plsql = False
            continue
        buf.append(line)
        if stripped.endswith(";") and not in_plsql:
            joined = "\n".join(buf).strip()
            if joined.endswith(";"):
                joined = joined[:-1]
            if joined.strip():
                stmts.append(joined)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


async def _apply(dsn: str, path: Path, embedding_dim: int = 768) -> None:
    from mnemos.persistence.oracle import create_oracle_pool

    sql = path.read_text()
    sql = sql.replace("{{embedding_dim}}", str(embedding_dim))
    statements = _split_statements(sql)
    print(f"[migrate] {len(statements)} statements from {path.name}")

    pool = await create_oracle_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()
            for i, stmt in enumerate(statements, 1):
                head = stmt.splitlines()[0].strip()[:80]
                try:
                    await cur.execute(stmt)
                    print(f"  [{i:2d}] OK  {head}")
                except Exception as e:
                    msg = str(e).splitlines()[0]
                    # ORA-00955 (already exists) / ORA-02275 (FK already
                    # defined) / ORA-01430 (column already exists) /
                    # ORA-04081 (trigger already exists) are all idempotency
                    # signals — log but don't fail.
                    benign = any(code in msg for code in ("ORA-00955", "ORA-02275", "ORA-01430", "ORA-04081"))
                    tag = "skip" if benign else "FAIL"
                    print(f"  [{i:2d}] {tag} {head} -- {msg}")
                    if not benign:
                        raise
            await conn.commit()
            cur.close()
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument(
        "--dim",
        type=int,
        default=int(os.environ.get("MNEMOS_EMBEDDING_DIM", "768")),
        help="Embedding dimension for VECTOR(dim, FLOAT32). Defaults to MNEMOS_EMBEDDING_DIM or 768.",
    )
    args = ap.parse_args()
    asyncio.run(_apply(args.dsn, Path(args.file), embedding_dim=args.dim))
    return 0


if __name__ == "__main__":
    sys.exit(main())
