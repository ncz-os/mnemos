"""KNEMON: seed Oracle model_registry from a JSON dump of Postgres.

Idempotent: MERGE on (provider, model_id).
"""
import json
import oracledb


def upsert_oracle(rows):
    con = oracledb.connect(
        user="mnemos", password="mnemos_dev", dsn="127.0.0.1:1521/ORCLPDB1"
    )
    # Bind CLOB-bearing string columns as DB_TYPE_CLOB so >4000-byte payloads
    # don't blow VARCHAR2(4000) bind limit (which silently drops the conn).
    cur = con.cursor()
    cur.setinputsizes(
        caps=oracledb.DB_TYPE_CLOB,
        raw_payload=oracledb.DB_TYPE_CLOB,
    )
    inserted = updated = errors = 0
    for r in rows:
        caps = r.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = json.loads(caps)
            except Exception:
                caps = [caps]
        caps_json = json.dumps(list(caps))
        raw = r.get("raw")
        if raw is None:
            raw_json = None
        elif isinstance(raw, (dict, list)):
            raw_json = json.dumps(raw)
        else:
            try:
                json.loads(str(raw))
                raw_json = str(raw)
            except Exception:
                raw_json = json.dumps({"_orig": str(raw)})
        try:
            cur.execute(
                """
                MERGE INTO model_registry t
                USING (SELECT :provider AS provider, :model_id AS model_id FROM dual) s
                ON (t.provider = s.provider AND t.model_id = s.model_id)
                WHEN MATCHED THEN UPDATE SET
                    display_name = :display_name,
                    family = :family,
                    context_window = :context_window,
                    max_output_tokens = :max_output_tokens,
                    capabilities = :caps,
                    input_cost_per_mtok = :input_cost,
                    output_cost_per_mtok = :output_cost,
                    cache_read_per_mtok = :cache_read,
                    cache_write_per_mtok = :cache_write,
                    available = :available,
                    deprecated = :deprecated,
                    arena_score = :arena_score,
                    arena_rank = :arena_rank,
                    graeae_weight = :graeae_weight,
                    last_seen = SYSTIMESTAMP,
                    last_synced = SYSTIMESTAMP,
                    raw_payload = :raw_payload
                WHEN NOT MATCHED THEN INSERT (
                    provider, model_id, display_name, family,
                    context_window, max_output_tokens, capabilities,
                    input_cost_per_mtok, output_cost_per_mtok,
                    cache_read_per_mtok, cache_write_per_mtok,
                    available, deprecated, arena_score, arena_rank,
                    graeae_weight, raw_payload
                ) VALUES (
                    :provider, :model_id, :display_name, :family,
                    :context_window, :max_output_tokens, :caps,
                    :input_cost, :output_cost,
                    :cache_read, :cache_write,
                    :available, :deprecated, :arena_score, :arena_rank,
                    :graeae_weight, :raw_payload
                )
                """,
                {
                    "provider": r["provider"],
                    "model_id": r["model_id"],
                    "display_name": r.get("display_name"),
                    "family": r.get("family"),
                    "context_window": r.get("context_window"),
                    "max_output_tokens": r.get("max_output_tokens"),
                    "caps": caps_json,
                    "input_cost": float(r["input_cost_per_mtok"]) if r.get("input_cost_per_mtok") is not None else 0.0,
                    "output_cost": float(r["output_cost_per_mtok"]) if r.get("output_cost_per_mtok") is not None else 0.0,
                    "cache_read": float(r["cache_read_per_mtok"]) if r.get("cache_read_per_mtok") is not None else 0.0,
                    "cache_write": float(r["cache_write_per_mtok"]) if r.get("cache_write_per_mtok") is not None else 0.0,
                    "available": 1 if r.get("available") else 0,
                    "deprecated": 1 if r.get("deprecated") else 0,
                    "arena_score": float(r["arena_score"]) if r.get("arena_score") is not None else None,
                    "arena_rank": r.get("arena_rank"),
                    "graeae_weight": float(r["graeae_weight"]) if r.get("graeae_weight") is not None else None,
                    "raw_payload": raw_json,
                },
            )
            rowcount = cur.rowcount or 0
            if rowcount == 1:
                inserted += 1
            else:
                updated += 1
            con.commit()
        except Exception as e:
            errors += 1
            print(f"  ERR {r['provider']}/{r['model_id']}: {str(e)[:140]}")
            try:
                con.rollback()
            except Exception:
                pass
    cur.execute("SELECT COUNT(*) FROM model_registry")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM model_registry WHERE input_cost_per_mtok > 0")
    priced = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM model_registry WHERE available = 1 AND deprecated = 0")
    avail = cur.fetchone()[0]
    print(f"upsert: errors={errors}, oracle total={total}, oracle priced(>0)={priced}, oracle avail+notdeprecated={avail}")


def main():
    with open("/tmp/model_registry_dump.json") as f:
        text = f.read().strip()
    if not text or text == "null":
        print("empty dump; nothing to seed")
        return
    rows = json.loads(text)
    print(f"loaded {len(rows)} rows from postgres dump")
    upsert_oracle(rows)


if __name__ == "__main__":
    main()
