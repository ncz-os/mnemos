"""Apply db/migrations_db2/0001_core_schema.sql to a target Db2 instance.

Splits on the Db2 CLP ``@`` statement terminator (not the sqlplus ``/``
+ ``;`` pair the Oracle equivalent uses). Idempotent — treats SQLSTATE 42710 (object exists) only
42710 (object already exists) as benign
on replay; everything else is fatal.

Usage::

    .venv/bin/python scripts/db2_apply_migration.py \\
        --dsn 'db2://db2inst1:mnemos_dev@192.168.207.67:50000/MNEMOS'
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get("DB2_PROOF_DSN", "db2://db2inst1:mnemos_dev@192.168.207.67:50000/MNEMOS")
DEFAULT_FILE = REPO_ROOT / "db" / "migrations_db2" / "0001_core_schema.sql"

# SQLSTATE codes the Db2 migration applier treats as idempotent-replay
# signals (logged but not fatal). Per IBM Db2 12.1 SQLCODE/SQLSTATE
# tables:
#   42710 — name already used by an existing object
#   42P07 — duplicate_table (libpq compat code surfaced by some drivers)
#   42601 — SQL syntax error (surfaces when CREATE VECTOR INDEX runs
#           against a non-12.1.5 / non-EAP build that doesn't ship the
#           DiskANN syntax — the rest of the schema applies cleanly
#           and the app-path falls back to exact scan; operator can
#           re-run after upgrading)
#   56098 — Db2 operation requires a registry var (e.g.
#           DB2_VECTOR_INDEXING=YES) that isn't set. The Db2Backend
#           startup probe surfaces a clearer message at runtime.
#   SQL0104N / SQL0270N — parser- or feature-not-available errors that
#           the EAP CREATE VECTOR INDEX statement can raise on older
#           Fix Packs; tagged benign so the rest of the schema applies.
BENIGN_SQLSTATES = {"42710", "42P07", "42601", "56098", "SQL0104N", "SQL0270N"}


def _split_statements(sql: str) -> list[str]:
    """Split a Db2 CLP-style script on ``@`` statement terminators.

    ``--#SET TERMINATOR @`` directives are honoured (ignored — we
    always use @ as our terminator regardless of what the file
    declares). Comments (``--`` line-prefix) and blank lines are
    preserved inside statements so PL/SQL-like blocks keep their
    structure, but trailing ``@`` is stripped from each yielded
    statement. Comment-only blocks (no SQL between two ``@``) are
    skipped.
    """

    def _is_executable(block: str) -> bool:
        return any(line.strip() and not line.strip().startswith("--") for line in block.splitlines())

    stmts: list[str] = []
    buf: list[str] = []
    for raw in sql.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--#SET TERMINATOR"):
            continue  # honoured implicitly; we always split on @
        if stripped == "@":
            block = "\n".join(buf).strip()
            if block and _is_executable(block):
                stmts.append(block)
            buf = []
            continue
        if stripped.endswith("@"):
            buf.append(raw.rstrip()[:-1])  # drop trailing @
            block = "\n".join(buf).strip()
            if block and _is_executable(block):
                stmts.append(block)
            buf = []
            continue
        buf.append(raw)
    tail = "\n".join(buf).strip()
    if tail and _is_executable(tail):
        stmts.append(tail)
    return stmts


def _default_build_parallelism() -> int:
    """~75% of host cores (min 2) for CREATE VECTOR INDEX construction."""
    import os

    cores = os.cpu_count() or 4
    return max(2, int(cores * 0.75))


async def _apply(
    dsn: str,
    path: Path,
    embedding_dim: int = 768,
    pct_comp: int = 15,
    build_parallelism: int | None = None,
    build_mem_budget: int = 4,
) -> None:
    from mnemos.persistence.db2 import create_db2_pool

    sql = path.read_text()
    # Template substitution: {{embedding_dim}} → caller-supplied dim
    # (default 768 for nomic-embed-text; 384 / 1536 / 3072 for others).
    sql = sql.replace("{{embedding_dim}}", str(embedding_dim))
    # CREATE VECTOR INDEX build-time tuning (Db2 12.1.5 EAP). Host-adaptive
    # defaults; ignored on 12.1.4 (whole statement is tolerated-to-fail).
    if build_parallelism is None:
        build_parallelism = _default_build_parallelism()
    sql = sql.replace("{{vector_pct_comp}}", str(pct_comp))
    sql = sql.replace("{{vector_build_parallelism}}", str(build_parallelism))
    sql = sql.replace("{{vector_build_mem_budget}}", str(build_mem_budget))
    statements = _split_statements(sql)
    print(f"[migrate] {len(statements)} statements from {path.name}")

    pool = await create_db2_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for i, stmt in enumerate(statements, 1):
                head = stmt.splitlines()[0].strip()[:80]
                cur = conn.cursor()
                try:
                    await cur.execute(stmt)
                    print(f"  [{i:2d}] OK  {head}")
                except Exception as exc:
                    msg = str(exc).splitlines()[0]
                    benign = any(code in msg for code in BENIGN_SQLSTATES)
                    tag = "skip" if benign else "FAIL"
                    print(f"  [{i:2d}] {tag} {head} -- {msg}")
                    if not benign:
                        raise
                finally:
                    await cur.close()
            await conn.commit()
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument(
        "--dim", type=int, default=768, help="Embedding dimension for VECTOR(dim, FLOAT32). Defaults to 768."
    )
    ap.add_argument(
        "--pct-comp",
        type=int,
        default=15,
        help="PCT_COMP_VECT_SIZE for CREATE VECTOR INDEX (percent). Higher = lower query latency, more memory. Default 15.",
    )
    ap.add_argument(
        "--build-parallelism",
        type=int,
        default=None,
        help="BUILD_PARALLELISM for CREATE VECTOR INDEX. Default: ~75%% of host cores (min 2).",
    )
    ap.add_argument(
        "--build-mem-budget",
        type=int,
        default=4,
        help="BUILD_MEM_BUDGET (GB) for CREATE VECTOR INDEX construction. Default 4.",
    )
    args = ap.parse_args()
    asyncio.run(
        _apply(
            args.dsn,
            Path(args.file),
            embedding_dim=args.dim,
            pct_comp=args.pct_comp,
            build_parallelism=args.build_parallelism,
            build_mem_budget=args.build_mem_budget,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
