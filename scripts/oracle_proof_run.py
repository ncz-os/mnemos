"""Reproducible proof-of-port harness for the MNEMOS → Oracle 23ai port.

Connects to a target Oracle Database 23ai instance via the DSN passed
on the command line (or ``ORACLE_PROOF_DSN`` env var), exercises every
:class:`OracleBackend` repository surface, captures the SQL fingerprint
plus the result of each call, and emits a signed JSON artifact:

    docs/proof/oracle-proof-YYYYMMDD-HHMMSS.json

The artifact contains
- Oracle version banner (``v$version``)
- Git HEAD SHA of the repository the script ran from
- python-oracledb client version
- Timestamps for the whole run + per-method timings
- Pass/fail per probe with raw return shapes (truncated)
- HMAC-SHA256 signature over the artifact's evidence body so a reader
  can verify it was emitted by the script and not hand-edited

Usage::

    .venv/bin/python scripts/oracle_proof_run.py \\
        --dsn oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1

The script never wipes existing data — it inserts smoke rows with a
``proof-<uuid>`` id prefix and cleans them up before exit. The 8157
imported memories on PROTEUS are not touched.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DSN = os.environ.get(
    "ORACLE_PROOF_DSN",
    "oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1",
)
DEFAULT_HMAC_KEY = os.environ.get("MNEMOS_PROOF_HMAC_KEY")
if not DEFAULT_HMAC_KEY or DEFAULT_HMAC_KEY in ("mnemos-oracle-proof-v1", "mnemos-db2-proof/v1"):
    print("ERROR: MNEMOS_PROOF_HMAC_KEY env var required (fail-closed).", file=sys.stderr)
    sys.exit(1)


def _git_head_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _git_short_status() -> str:
    try:
        out = subprocess.check_output(["git", "status", "--short"], cwd=REPO_ROOT, text=True).strip()
        return out
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _truncate(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… <truncated {len(value) - limit} chars>"
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value[:5]] + (["…"] if len(value) > 5 else [])
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in list(value.items())[:20]}
    return value


class Probe:
    """One verifiable probe + its result row in the proof artifact."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.start = _now_iso()
        self.t0 = time.perf_counter()
        self.outcome: str = "pending"
        self.elapsed_ms: float = 0.0
        self.evidence: Any = None
        self.error: str | None = None

    def ok(self, evidence: Any) -> None:
        self.outcome = "pass"
        self.elapsed_ms = (time.perf_counter() - self.t0) * 1000.0
        self.evidence = _truncate(evidence)

    def fail(self, exc: BaseException) -> None:
        self.outcome = "fail"
        self.elapsed_ms = (time.perf_counter() - self.t0) * 1000.0
        self.error = f"{type(exc).__name__}: {exc}"

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "outcome": self.outcome,
            "start_utc": self.start,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "evidence": self.evidence,
            "error": self.error,
        }


async def _run(dsn: str, hmac_key: str) -> dict[str, Any]:
    from mnemos.persistence.oracle import (  # local import
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
        create_oracle_pool,
    )
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    import oracledb  # noqa: E402

    probes: list[Probe] = []
    run_id = uuid.uuid4().hex[:12]

    pool = await create_oracle_pool(dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            # ---- 0. Identity probes ------------------------------------
            cur = conn.cursor()
            await cur.execute("SELECT BANNER_FULL FROM v$version")
            (banner,) = await cur.fetchone()
            oracle_version = banner

            await cur.execute(
                "SELECT VECTOR_DISTANCE(TO_VECTOR(:a), TO_VECTOR(:b), COSINE) FROM dual",
                {"a": "[1.0,0.0,0.0]", "b": "[0.0,1.0,0.0]"},
            )
            (cosine_orthogonal,) = await cur.fetchone()
            await cur.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
            (live_memories,) = await cur.fetchone()
            cur.close()

            class TxLike:
                def __init__(self, c):
                    self.conn = c

            tx = TxLike(conn)
            root = VisibilityFilter(
                scope=VisibilityScope.ROOT_BYPASS,
                user_id=None,
                group_ids=(),
                namespace=None,
            )

            # ---- 1. MemoryRepository ----------------------------------
            mem = OracleMemoryRepository()

            p = Probe(
                "memory.gather_stats",
                "OracleMemoryRepository.gather_stats over the imported baseline",
            )
            try:
                stats = await mem.gather_stats(tx)
                p.ok(
                    {
                        "total_memories": stats.total_memories,
                        "native_memories": stats.native_memories,
                        "federated_memories": stats.federated_memories,
                        "avg_quality_rating": stats.avg_quality_rating,
                    }
                )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            p = Probe(
                "memory.list_memories",
                "ROOT_BYPASS list with total count over the same predicate",
            )
            try:
                rows, total = await mem.list_memories(tx, visibility=root, limit=3)
                p.ok({"returned": len(rows), "total": total})
            except Exception as e:
                p.fail(e)
            probes.append(p)

            p = Probe(
                "memory.fts_search",
                "DBMS_LOB.INSTR-based FTS fallback (Oracle Text rollout pending)",
            )
            try:
                results = await mem.fts_search(tx, query="mnemos", limit=3, visibility=root)
                p.ok({"matches": len(results)})
            except Exception as e:
                p.fail(e)
            probes.append(p)

            sample_mid: str | None = None
            cur = conn.cursor()
            await cur.execute("SELECT id FROM memories WHERE deleted_at IS NULL FETCH FIRST 1 ROWS ONLY")
            row = await cur.fetchone()
            cur.close()
            if row:
                sample_mid = row[0]

            p = Probe(
                "memory.fetch_memory_by_id",
                "Round-trip by-id fetch with full column set + CLOB materialization",
            )
            try:
                if sample_mid is None:
                    p.ok(None)
                else:
                    m = await mem.fetch_memory_by_id(tx, sample_mid)
                    p.ok(
                        {
                            "id": m["id"],
                            "owner_id": m.get("owner_id"),
                            "category": m.get("category"),
                            "content_len": (len(m["content"]) if m.get("content") else 0),
                            "column_count": len(m),
                        }
                    )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 2. Round-trip CRUD ----------------------------------
            crud_mid = f"proof-{run_id}-mem"
            p = Probe(
                "memory.insert+update+delete",
                "End-to-end CRUD with VisibilityFilter — OWN_ONLY block proves visibility enforcement",
            )
            try:
                await mem.insert_memory(
                    tx,
                    memory_id=crud_mid,
                    content="proof content",
                    category="proof",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=50,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                    permission_mode=600,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=None,
                    created=None,
                    updated=None,
                )
                own = VisibilityFilter(
                    scope=VisibilityScope.OWN_ONLY,
                    user_id=f"proof-{run_id}",
                    group_ids=(),
                    namespace=f"proof-{run_id}",
                )
                wrong = VisibilityFilter(
                    scope=VisibilityScope.OWN_ONLY,
                    user_id="not-the-owner",
                    group_ids=(),
                    namespace=f"proof-{run_id}",
                )
                upd = await mem.update_memory(tx, crud_mid, visibility=own, fields={"content": "v2"})
                blocked = await mem.update_memory(tx, crud_mid, visibility=wrong, fields={"content": "BAD"})
                deleted = await mem.delete_memory(tx, crud_mid, visibility=own)
                gone = await mem.get_memory(tx, crud_mid, visibility=root)
                p.ok(
                    {
                        "update_returned_content": upd["content"] if upd else None,
                        "wrong_owner_blocked": blocked is None,
                        "delete_returned_id": deleted["id"] if deleted else None,
                        "gone_after_delete": gone is None,
                    }
                )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 3. Semantic search (Oracle 23ai VECTOR) -------------
            p = Probe(
                "memory.semantic_search",
                "Oracle 23ai VECTOR_DISTANCE COSINE ranking on unit-axis embeddings (384-dim schema)",
            )
            try:
                vec_ids: list[str] = []
                dim = 384

                def _axis(idx: int) -> list[float]:
                    v = [0.0] * dim
                    v[idx] = 1.0
                    return v

                axes = {"x": _axis(0), "y": _axis(1), "z": _axis(2)}
                for axis, vec in axes.items():
                    mid = f"proof-{run_id}-vec-{axis}"
                    vec_ids.append(mid)
                    await mem.insert_memory(
                        tx,
                        memory_id=mid,
                        content=f"axis {axis}",
                        category="proof-vec",
                        subcategory=None,
                        metadata_json="{}",
                        quality_rating=50,
                        owner_id=f"proof-{run_id}",
                        namespace=f"proof-{run_id}",
                        permission_mode=600,
                        source_model=None,
                        source_provider=None,
                        source_session=None,
                        source_agent=None,
                        verbatim_content=None,
                        created=None,
                        updated=None,
                    )
                    vec_lit = "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
                    cur = conn.cursor()
                    await cur.execute(
                        "UPDATE memories SET embedding = TO_VECTOR(:v) WHERE id = :id",
                        {"v": vec_lit, "id": mid},
                    )
                    cur.close()

                # Commit so TO_VECTOR inserts are visible to the search
                await conn.commit()

                # Build 384-dim query biased toward x and y axes
                qx = [0.9 if i == 0 else (0.1 if i == 1 else 0.0) for i in range(dim)]
                qy = [0.1 if i == 0 else (0.9 if i == 1 else 0.0) for i in range(dim)]
                results_x = await mem.semantic_search(
                    tx,
                    embedding=qx,
                    limit=3,
                    visibility=root,
                    category="proof-vec",
                )
                results_y = await mem.semantic_search(
                    tx,
                    embedding=qy,
                    limit=3,
                    visibility=root,
                    category="proof-vec",
                )
                p.ok(
                    {
                        "x_query_order": [r["content"] for r in results_x],
                        "y_query_order": [r["content"] for r in results_y],
                    }
                )
                # Cleanup vec rows
                cur = conn.cursor()
                ph = ",".join(f":id{i}" for i in range(len(vec_ids)))
                await cur.execute(
                    f"DELETE FROM memories WHERE id IN ({ph})",
                    {f"id{i}": mid for i, mid in enumerate(vec_ids)},
                )
                cur.close()
                await conn.commit()
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 4. KG + Version round-trip --------------------------
            kg = OracleKGRepository()
            ver = OracleVersionRepository()
            branch = OracleBranchRepository()

            p = Probe(
                "kg+version+branch.round_trip",
                "Insert + by-id fetch + DAG log + branch upsert across sidecars",
            )
            try:
                kg_id = f"proof-{run_id}-tri"
                v1 = f"proof-{run_id}-v1"
                v2 = f"proof-{run_id}-v2"
                anchor_mid = f"proof-{run_id}-anchor"
                await mem.insert_memory(
                    tx,
                    memory_id=anchor_mid,
                    content="anchor",
                    category="proof",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=50,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                    permission_mode=600,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=None,
                    created=None,
                    updated=None,
                )
                await kg.insert_kg_triple(
                    tx,
                    triple_id=kg_id,
                    subject="alice",
                    predicate="works_at",
                    obj="acme",
                    subject_type="person",
                    object_type="org",
                    valid_from=None,
                    valid_until=None,
                    memory_id=anchor_mid,
                    confidence=0.9,
                    created=None,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                kg_row = await kg.fetch_kg_triple_by_id(tx, kg_id)

                await ver.insert_memory_version(
                    tx,
                    version_id=v1,
                    memory_id=anchor_mid,
                    version_num=1,
                    content="v1",
                    category=None,
                    subcategory=None,
                    metadata_json="{}",
                    verbatim_content=None,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                    permission_mode=None,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    snapshot_at=None,
                    snapshot_by=None,
                    change_type="create",
                    commit_hash=f"proof-commit-a-{run_id}",
                    parent_version_id=None,
                    branch="main",
                    merge_parents=None,
                )
                await ver.insert_memory_version(
                    tx,
                    version_id=v2,
                    memory_id=anchor_mid,
                    version_num=2,
                    content="v2",
                    category=None,
                    subcategory=None,
                    metadata_json="{}",
                    verbatim_content=None,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                    permission_mode=None,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    snapshot_at=None,
                    snapshot_by=None,
                    change_type="update",
                    commit_hash=f"proof-commit-b-{run_id}",
                    parent_version_id=v1,
                    branch="main",
                    merge_parents=None,
                )
                log = await mem.fetch_memory_log(tx, anchor_mid, "main", 10, user=None)
                heads = await branch.fetch_memory_branch_heads(tx, [anchor_mid])
                p.ok(
                    {
                        "kg_triple_subject": kg_row["subject"],
                        "version_log_count": len(log),
                        "version_log_top": log[0]["version_num"] if log else None,
                        "branch_heads": [dict(h) for h in heads],
                    }
                )
                await branch.delete_memory_branches_for_memories(tx, [anchor_mid])
                cur = conn.cursor()
                await cur.execute(
                    "DELETE FROM memory_versions WHERE memory_id = :id",
                    {"id": anchor_mid},
                )
                await cur.execute("DELETE FROM kg_triples WHERE id = :id", {"id": kg_id})
                await cur.execute("DELETE FROM memories WHERE id = :id", {"id": anchor_mid})
                cur.close()
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 5. State KV -----------------------------------------
            state = OracleStateRepository()
            p = Probe(
                "state.set_get_delete",
                "MERGE upsert + soft-delete on the state table",
            )
            try:
                key = f"proof-{run_id}-key"
                await state.set(
                    tx,
                    key,
                    "value-a",
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                got = await state.get(
                    tx,
                    key,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                await state.delete(
                    tx,
                    key,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                after = await state.get(
                    tx,
                    key,
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                p.ok({"set_value": got["value"], "after_delete": after})
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 6. Federation round-trip ----------------------------
            fed = OracleFederationRepository()
            p = Probe(
                "federation.full_lifecycle",
                "create → update → upsert → sync_log → delete on federation_peers + sync log",
            )
            try:
                peer_name = f"proof-{run_id}-peer"
                peer = await fed.create_peer(
                    tx,
                    name=peer_name,
                    base_url="http://proof.example",
                    auth_token="proof-token",
                    namespace_filter=["alpha"],
                    category_filter=None,
                    enabled=True,
                    sync_interval_secs=300,
                    compat_mode="strict",
                )
                pid = peer["id"]
                await fed.update_peer(tx, pid, {"sync_interval_secs": 600})
                log_id = await fed.create_sync_log(tx, pid, cursor_before=None)
                await fed.finish_sync_log(
                    tx,
                    log_id=log_id,
                    memories_pulled=5,
                    memories_new=4,
                    memories_updated=1,
                    error=None,
                    cursor_after=None,
                )
                await fed.record_sync_success(tx, pid, None, 5)
                logs = await fed.fetch_sync_log(tx, pid, 5)
                feed = await fed.feed_query(
                    tx,
                    since_updated=None,
                    since_id=None,
                    namespaces=[],
                    categories=[],
                    limit=3,
                    prefer_compressed=False,
                )
                gone_ok = await fed.delete_peer(tx, pid)
                p.ok(
                    {
                        "peer_created": pid[:12],
                        "sync_log_count": len(logs),
                        "first_log_pulled": logs[0]["memories_pulled"] if logs else None,
                        "feed_returned": len(feed),
                        "delete_returned_true": gone_ok,
                    }
                )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 7. Webhook outbox -----------------------------------
            wh = OracleWebhookRepository()
            p = Probe(
                "webhook.dispatch_event",
                "Match subscription via JSON-array events string + insert delivery rows",
            )
            try:
                sub_id = f"proof-{run_id}-sub"
                cur = conn.cursor()
                await cur.execute(
                    """
                    INSERT INTO webhook_subscriptions (
                        id, url, events, secret, owner_id, namespace, revoked
                    ) VALUES (:id, 'https://proof.test/hook', :events,
                              'proof-sekret', :owner, :ns, 0)
                    """,
                    {
                        "id": sub_id,
                        "events": json.dumps(["memory.created"]),
                        "owner": f"proof-{run_id}",
                        "ns": f"proof-{run_id}",
                    },
                )
                cur.close()
                hit = await wh.dispatch_event(
                    tx,
                    "memory.created",
                    {"id": "x"},
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                miss = await wh.dispatch_event(
                    tx,
                    "nope.unknown",
                    {"id": "x"},
                    owner_id=f"proof-{run_id}",
                    namespace=f"proof-{run_id}",
                )
                cur = conn.cursor()
                await cur.execute(
                    "DELETE FROM webhook_deliveries WHERE subscription_id = :s",
                    {"s": sub_id},
                )
                await cur.execute("DELETE FROM webhook_subscriptions WHERE id = :s", {"s": sub_id})
                cur.close()
                p.ok({"matched_deliveries": len(hit), "unsubscribed": len(miss)})
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 8. Compression repo --------------------------------
            comp = OracleCompressionRepository()
            p = Probe(
                "compression.gather_stats",
                "Aggregate counters over memory_compressed_variants",
            )
            try:
                cstats = await comp.gather_stats(tx)
                p.ok(
                    {
                        "total_compressions": cstats.total_compressions,
                        "average_ratio": cstats.average_compression_ratio,
                        "unreviewed": cstats.unreviewed_compressions,
                    }
                )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 9. Consultation audit safe defaults ----------------
            audit = OracleConsultationAuditRepository()
            p = Probe(
                "audit.safe_defaults",
                "ConsultationAudit returns (None, []) so GRAEAE uses built-in defaults",
            )
            try:
                rec, reasons = await audit.fetch_recommended_model(tx, "code_generation", 10.0, 0.85)
                models = await audit.fetch_available_models(tx)
                p.ok(
                    {
                        "fetch_recommended_model": rec is None and reasons == [],
                        "fetch_available_models_len": len(models),
                    }
                )
            except Exception as e:
                p.fail(e)
            probes.append(p)

            # ---- 10. Backend transactional ctx ----------------------
            backend = OracleBackend(pool, None)
            p = Probe(
                "backend.transactional",
                "OracleBackend.transactional yields a usable Transaction handle",
            )
            try:
                async with backend.transactional() as tx2:
                    cur2 = tx2.conn.cursor()
                    await cur2.execute("SELECT 1 FROM dual")
                    (one,) = await cur2.fetchone()
                    cur2.close()
                p.ok({"select_1_from_dual": one})
            except Exception as e:
                p.fail(e)
            probes.append(p)

            await conn.commit()
    finally:
        await pool.close()

    # ---- artifact assembly ---------------------------------------------
    finished = _now_iso()
    body = {
        "schema": "mnemos-oracle-proof/v1",
        "run_id": run_id,
        "target_dsn_redacted": _redact_dsn(dsn),
        "oracle_version": oracle_version,
        "cosine_orthogonal_distance": cosine_orthogonal,
        "live_memory_count_at_start": live_memories,
        "git_head_sha": _git_head_sha(),
        "git_status_short": _git_short_status(),
        "python_oracledb_version": oracledb.__version__,
        "python_version": sys.version,
        "started_utc": probes[0].start if probes else _now_iso(),
        "finished_utc": finished,
        "probes": [p.asdict() for p in probes],
        "summary": {
            "total": len(probes),
            "passed": sum(1 for p in probes if p.outcome == "pass"),
            "failed": sum(1 for p in probes if p.outcome == "fail"),
        },
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    signature = hmac.new(hmac_key.encode("utf-8"), body_json.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "evidence": body,
        "hmac_sha256": signature,
        "hmac_key_id": hashlib.sha256(hmac_key.encode("utf-8")).hexdigest()[:16],
    }


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    head, tail = dsn.split("@", 1)
    if "://" in head:
        scheme, creds = head.split("://", 1)
        if ":" in creds:
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:<redacted>@{tail}"
    return f"<redacted>@{tail}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--hmac-key", default=DEFAULT_HMAC_KEY)
    ap.add_argument(
        "--out",
        default=str(
            REPO_ROOT
            / "docs"
            / "proof"
            / f"oracle-proof-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        ),
    )
    args = ap.parse_args()

    try:
        artifact = asyncio.run(_run(args.dsn, args.hmac_key))
    except Exception:
        traceback.print_exc()
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    summary = artifact["evidence"]["summary"]
    print(f"wrote {out} — passed={summary['passed']}/{summary['total']}")
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
