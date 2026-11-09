#!/usr/bin/env python3
"""Copy the live Oracle MNEMOS schema (MNEMOS + Hive tables) into postgres, table by table, with row-count parity.

Usage:
  oracle_to_postgres_copy.py --oracle USER/PW@HOST:PORT/SERVICE --pg postgresql://... --target mnemos|hive [--tables a,b] [--dry-run]

* target ``mnemos``: every BASE TABLE of the postgres database whose name exists in Oracle (same name).
* target ``hive``: the hive bus tables; postgres name -> Oracle name via HIVE_MAP (hive.persistence.oracle._TABLE_MAP)
  plus the un-prefixed control tables.
* Columns: intersection of both sides by name; columns only on one side are reported, never guessed.
* Values are converted by the postgres column type (json/jsonb, uuid, bytea, timestamps, numerics, booleans, text).
  ``vector`` columns are skipped (embeddings are regenerated after the copy), and so are generated columns.
* The target table is TRUNCATEd first (the run is a full reload), COPY in batches, then identity/serial sequences
  are set past max(id) and the row count is compared with Oracle's.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import decimal
import json
import sys
import time
import uuid


HIVE_MAP = {
    "agents": "hive_agents",
    "jobs": "hive_jobs",
    "messages": "hive_messages",
    "events": "hive_events",
    "hive_cache": "hive_cache",
    "scheduled_jobs": "hive_scheduled_jobs",
    "worker_kind_stats": "hive_worker_kind_stats",
    "job_audit_log": "hive_job_audit_log",
    "job_result_history": "hive_job_result_history",
    "capacity_ledger": "capacity_ledger",
    "capacity_usage": "capacity_usage",
}
#: (postgres table, postgres column) -> Oracle column, where the two schemas renamed a column
RENAMES = {("model_registry", "raw"): "raw_payload"}
BATCH = 5000
#: (table, column) NOT NULL without a default on the postgres side (Oracle allowed NULL there)
NOT_NULL_NO_DEFAULT: set[tuple[str, str]] = set()
TEXT_TYPES = {"text", "character varying", "character"}
UTC = dt.timezone.utc


def ora_connect(spec: str):
    import oracledb  # imported here so the conversion helpers import (and test) without the driver

    user, rest = spec.split("/", 1)
    pw, dsn = rest.rsplit("@", 1)
    oracledb.defaults.fetch_lobs = False  # CLOB -> str, BLOB -> bytes
    return oracledb.connect(user=user, password=pw, dsn=dsn)


#: lower-cased column name -> the exact Oracle identifier (a reserved word such as ``mode`` was created quoted lower)
ORA_NAMES: dict[tuple[str, str], str] = {}


def ora_columns(cur, table: str) -> dict[str, str]:
    cur.execute("select column_name, data_type from user_tab_columns where table_name = :t", t=table.upper())
    out = {}
    for c, t in cur.fetchall():
        out[c.lower()] = t
        ORA_NAMES[(table.upper(), c.lower())] = c
    return out


def to_json_text(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, ensure_ascii=False)
    if isinstance(v, bytes):
        v = v.decode()
    return str(v)


def converter(pg_type: str, ora_type: str):
    """Return a value converter for one column (postgres data_type/udt_name vs Oracle data_type)."""
    t = pg_type
    if t in ("json", "jsonb"):

        def f(v):
            s = to_json_text(v)
            if s is None:
                return None
            try:
                json.loads(s)
                return s
            except ValueError:
                return json.dumps(s)  # a non-JSON text in a json column: keep it as a JSON string

        return f
    if t == "uuid":

        def f(v):
            if v is None:
                return None
            if isinstance(v, bytes):
                return uuid.UUID(bytes=v) if len(v) == 16 else uuid.UUID(v.decode())
            try:
                return uuid.UUID(str(v))
            except ValueError:
                # a hand-written id (e.g. model_registry 'claude-opus-4-8') in a uuid column: deterministic uuid5
                return uuid.uuid5(uuid.NAMESPACE_URL, f"ora2pg:{v}")

        return f
    if t == "bytea":
        return lambda v: None if v is None else (v if isinstance(v, bytes) else str(v).encode())
    if t in ("timestamp with time zone", "timestamptz"):

        def f(v):
            if v is None:
                return None
            if isinstance(v, (int, float, decimal.Decimal)):
                return dt.datetime.fromtimestamp(float(v), UTC)
            if isinstance(v, dt.datetime):
                return v if v.tzinfo else v.replace(tzinfo=UTC)
            return dt.datetime.fromisoformat(str(v))

        return f
    if t in ("timestamp without time zone", "timestamp"):

        def f(v):
            if v is None:
                return None
            if isinstance(v, (int, float, decimal.Decimal)):
                return dt.datetime.fromtimestamp(float(v), UTC).replace(tzinfo=None)
            if isinstance(v, dt.datetime):
                return v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo else v
            return dt.datetime.fromisoformat(str(v))

        return f
    if t == "date":
        return lambda v: None if v is None else (v.date() if isinstance(v, dt.datetime) else v)
    if t in ("integer", "bigint", "smallint"):

        def f(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return int(v)
            return int(v)

        return f
    if t in ("double precision", "real"):
        return lambda v: None if v is None else float(v)
    if t == "numeric":
        return lambda v: None if v is None else decimal.Decimal(str(v))
    if t == "boolean":

        def f(v):
            if v is None:
                return None
            if isinstance(v, str):
                return v.strip().lower() in ("1", "t", "true", "y", "yes")
            return bool(v)

        return f
    if t == "ARRAY":

        def f(v):
            if v is None:
                return None
            if isinstance(v, (list, tuple)):
                return [str(x) for x in v]
            try:
                parsed = json.loads(to_json_text(v))
                return [str(x) for x in parsed] if isinstance(parsed, list) else [str(parsed)]
            except ValueError:
                return [str(v)]

        return f

    # text / varchar / anything else: text
    def f(v):
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str, ensure_ascii=False)
        if isinstance(v, bytes):
            return v.hex() if ora_type == "RAW" else v.decode(errors="replace")
        if isinstance(v, dt.datetime):
            return v.isoformat()
        if isinstance(v, decimal.Decimal):
            return str(v)
        return str(v)

    return f


async def pg_columns(conn, table: str) -> list[tuple[str, str, bool]]:
    rows = await conn.fetch(
        "select column_name, data_type, udt_name, is_generated, column_default, is_nullable from information_schema.columns "
        "where table_schema = current_schema() and table_name = $1 order by ordinal_position",
        table,
    )
    out = []
    for r in rows:
        dtp = r["data_type"]
        if dtp == "USER-DEFINED":
            dtp = r["udt_name"]
        out.append((r["column_name"], dtp, r["is_generated"] == "ALWAYS"))
        if r["is_nullable"] == "NO" and r["column_default"] is None:
            NOT_NULL_NO_DEFAULT.add((table, r["column_name"]))
    return out


async def copy_table(conn, ocur, pg_table: str, ora_table: str, dry: bool) -> dict:
    ocols = ora_columns(ocur, ora_table)
    if not ocols:
        return {"table": pg_table, "status": "no_oracle_table", "oracle": ora_table}
    pcols = await pg_columns(conn, pg_table)
    use, skipped_pg = [], []
    for name, typ, generated in pcols:
        src = RENAMES.get((pg_table, name), name)
        if generated or typ == "vector" or src not in ocols:
            skipped_pg.append(f"{name}:{typ}{':gen' if generated else ''}")
            continue
        use.append((name, typ, src))
    oracle_only = sorted(set(ocols) - {RENAMES.get((pg_table, n), n) for n, _, _ in pcols})
    ocur.execute(f"select count(*) from {ora_table}")
    ora_n = ocur.fetchone()[0]
    res = {
        "table": pg_table,
        "oracle": ora_table,
        "oracle_rows": ora_n,
        "columns": len(use),
        "pg_only_or_skipped": skipped_pg,
        "oracle_only": oracle_only,
    }
    if dry:
        res["status"] = "dry_run"
        return res
    convs = []
    for n, t, src in use:
        conv = converter(t, ocols[src])
        if (pg_table, n) in NOT_NULL_NO_DEFAULT and t in TEXT_TYPES:
            # Oracle kept NULL (e.g. a failed consultation with no consensus); postgres says NOT NULL: empty text
            conv = (lambda c: lambda v: "" if v is None else c(v))(conv)
        convs.append(conv)
    sel = []
    for _n, _t, src in use:
        ot = ocols[src]
        q = '"' + ORA_NAMES.get((ora_table.upper(), src), src.upper()) + '"'
        if ot.startswith("TIMESTAMP") and "TIME ZONE" in ot:
            sel.append(f"SYS_EXTRACT_UTC({q})")  # exact instant; oracledb drops the offset otherwise
        elif ot == "JSON":
            sel.append(f"JSON_SERIALIZE({q} RETURNING CLOB)")
        else:
            sel.append(q)
    t0 = time.time()
    # the table was emptied up front by main() (one TRUNCATE over the whole selection, never CASCADE): a per-table
    # TRUNCATE ... CASCADE silently emptied tables already copied earlier in the run that reference this one
    ocur.arraysize = BATCH
    ocur.prefetchrows = BATCH + 1
    ocur.execute(f"select {', '.join(sel)} from {ora_table}")
    copied = 0
    names = [n for n, _t, _s in use]
    while True:
        rows = ocur.fetchmany(BATCH)
        if not rows:
            break
        recs = [tuple(c(v) for c, v in zip(convs, row)) for row in rows]
        await conn.copy_records_to_table(pg_table, records=recs, columns=names)
        copied += len(recs)
    # serial / identity sequences past max(id)
    for name, _t, _g in pcols:
        seq = await conn.fetchval("select pg_get_serial_sequence($1, $2)", f'"{pg_table}"', name)
        if seq:
            await conn.execute(
                f'select setval(\'{seq}\', coalesce((select max("{name}") from "{pg_table}"), 0) + 1, false)'
            )
    pg_n = await conn.fetchval(f'select count(*) from "{pg_table}"')
    res.update(
        status="ok" if pg_n == ora_n else "COUNT_MISMATCH",
        pg_rows=pg_n,
        copied=copied,
        seconds=round(time.time() - t0, 1),
    )
    return res


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--pg", required=True)
    ap.add_argument("--target", choices=["mnemos", "hive"], required=True)
    ap.add_argument("--tables", default="")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--drop-constraints", default="", help="table.constraint,... dropped on the target before loading")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ocon = ora_connect(a.oracle)
    ocur = ocon.cursor()
    import asyncpg

    conn = await asyncpg.connect(a.pg)
    for spec in [c for c in a.drop_constraints.split(",") if c]:
        table, cname = spec.split(".", 1)
        if not a.dry_run:
            await conn.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{cname}"')
        print(json.dumps({"dropped_constraint": spec, "dry_run": a.dry_run}), flush=True)
    await conn.execute("SET session_replication_role = replica")  # bulk load: FK/trigger order does not matter
    if a.target == "hive":
        pairs = list(HIVE_MAP.items())
    else:
        names = [
            r["table_name"]
            for r in await conn.fetch(
                "select table_name from information_schema.tables where table_schema = current_schema() "
                "and table_type = 'BASE TABLE' order by table_name"
            )
        ]
        pairs = [(n, n) for n in names]
    only = {t for t in a.tables.split(",") if t}
    excl = {t for t in a.exclude.split(",") if t}
    pairs = [(p, o) for p, o in pairs if (not only or p in only) and p not in excl]
    if not a.dry_run and pairs:
        # One statement over the whole selection and no CASCADE: if a table outside the selection references one
        # inside it, postgres refuses (and nothing is emptied) instead of silently wiping the referencing table.
        await conn.execute("TRUNCATE " + ", ".join(f'"{p}"' for p, _o in pairs))
    bad = 0
    expected: dict[str, int] = {}
    for pg_table, ora_table in pairs:
        try:
            r = await copy_table(conn, ocur, pg_table, ora_table, a.dry_run)
        except Exception as exc:  # noqa: BLE001 - report the table, keep going, exit non-zero
            r = {"table": pg_table, "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if r.get("status") not in ("ok", "dry_run", "no_oracle_table"):
            bad += 1
        if r.get("status") == "ok":
            expected[pg_table] = r["pg_rows"]
        print(json.dumps(r, default=str), flush=True)
    # final recount after the WHOLE run: a table that changed after its own copy finished is a failure
    for pg_table, n in expected.items():
        now_n = await conn.fetchval(f'select count(*) from "{pg_table}"')
        if now_n != n:
            bad += 1
            print(
                json.dumps({"table": pg_table, "status": "CHANGED_AFTER_COPY", "copied": n, "now": now_n}), flush=True
            )
    await conn.execute("SET session_replication_role = origin")
    await conn.close()
    ocon.close()
    print(json.dumps({"summary": {"tables": len(pairs), "problems": bad}}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
