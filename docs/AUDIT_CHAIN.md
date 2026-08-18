# Audit Chain — Operator Guide

**Schema migrations**: `mnemos/db_migrations/migrations*/0029_memory_audit_chain.sql`
and `0030_memory_audit_roots.sql` (PostgreSQL, Oracle, Db2), plus
`mnemos/db_migrations/migrations_sqlite/migrations_v6_2_audit_chain_sqlite.sql`.

---

## What it does

Cryptographically-verifiable append-only audit chain over every memory write. Per-memory linear chain via `prev_entry_hash`; per-window Merkle tree across the global write log, sealed by a periodic worker.

| Component | Module / file |
|---|---|
| Crypto primitives (Ed25519, HKDF, JCS, Merkle) | `mnemos/audit/crypto.py` |
| Entry builder (signed payload, chain linkage) | `mnemos/audit/writer.py` |
| Route-handler bridge (mem_id<->bytes, write_audit_entry) | `mnemos/audit/route_helper.py` |
| Repository protocol | `mnemos/persistence/base.py::AuditChainRepository` |
| Postgres impl | `mnemos/persistence/postgres.py::PostgresAuditChainRepository` |
| SQLite impl | `mnemos/persistence/sqlite.py::SqliteAuditChainRepository` |
| Oracle impl | `mnemos/persistence/oracle.py::OracleAuditChainRepository` |
| Db2 impl (subclasses Oracle) | `mnemos/persistence/db2.py::Db2AuditChainRepository` |
| Sealer worker | `mnemos/workers/audit_sealer.py::AuditSealer` |
| Route wiring (create / update / delete) | `mnemos/api/routes/memories.py` |
| Federation replicate-op wiring | `mnemos/domain/federation.py::_store_memories` |
| HTTP endpoints | `mnemos/api/routes/audit.py` |

---

## Configuration

Two mandatory env vars when `MNEMOS_AUDIT_CHAIN=on`:

```bash
export MNEMOS_AUDIT_CHAIN=on
export MNEMOS_AUDIT_ROOT_PRIVKEY="$(python -c 'import os, base64; print(base64.b64encode(os.urandom(32)).decode())')"
```

The root privkey is loaded once at sealer init via `mnemos.audit.crypto.load_root_keypair`; raises `ValueError` if unset/malformed so the boot fails loud rather than silently disabling audit (same fail-loud pattern as v6.1 P3 #38 session-secret hardening).

`MNEMOS_SESSION_SECRET` is shared with the existing session-cookie path — already mandatory when auth is in use. Per-writer Ed25519 keys derive from `(session_secret, writer_id)` via HKDF-SHA256.

---

## Running the sealer

The sealer is a periodic asyncio loop. Two deployment shapes:

### Embedded in the mnemos-api process

```python
from mnemos.persistence.postgres import PostgresBackend
from mnemos.workers.audit_sealer import AuditSealer

# in app startup
sealer = AuditSealer(backend, window_seconds=60, batch_size=1000, poll_interval=60)
sealer_task = await sealer.start_background()

# on shutdown
sealer.stop()
await sealer_task
```

### Standalone (multi-replica HA)

Run N instances against the same Postgres / Oracle / Db2 backend; `FOR UPDATE SKIP LOCKED` ensures only one sealer commits any given window. Don't run multiple sealers against the SAME SQLite DB — single-writer would deadlock.

---

## HTTP endpoints (all require bearer-token auth)

### `GET /v1/audit/pubkey`

Returns the per-instance root Ed25519 public key. Optionally returns a per-writer pubkey when `?writer_id=<id>` is set:

```bash
curl -H "Authorization: Bearer $TOKEN" http://mnemos:5002/v1/audit/pubkey
# {"root_pubkey":"<base64>","algorithm":"Ed25519"}

curl -H "Authorization: Bearer $TOKEN" 'http://mnemos:5002/v1/audit/pubkey?writer_id=alice'
# {"root_pubkey":"<b64>", "writer_id":"alice", "writer_pubkey":"<b64>", "algorithm":"Ed25519"}
```

503 when `MNEMOS_AUDIT_CHAIN` is off. 400 when `writer_id` is empty.

### `GET /v1/audit/proof?memory_id_str=mem_xxx`

Returns the chain head for one memory: entry_id, op, signature, payload_hash, signed_at, prev_entry chain, plus sealed-window metadata (global_root, global_seq) when stamped:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  'http://mnemos:5002/v1/audit/proof?memory_id_str=mem_1779637500000_abc123'
```

Replicas use this to look up the containing root in `memory_audit_roots`.

### `GET /v1/audit/health`

Per-backend audit-chain health snapshot. Use for operator dashboards + alerts:

```bash
curl -H "Authorization: Bearer $TOKEN" http://mnemos:5002/v1/audit/health
# {
#   "chain_enabled": true,
#   "backend_has_audit_chain": true,
#   "total_entries": 12453,
#   "unsealed_count": 7,
#   "oldest_unsealed_signed_at": "2026-05-24T18:32:01.123456+00:00",
#   "sealed_root_count": 412,
#   "last_sealed_at": "2026-05-24T18:33:00.456789+00:00",
#   "oldest_unsealed_age_seconds": 84.3
# }
```

Returns the snapshot with `chain_enabled=False` when the env var is off but the tables exist (lets operators inspect a disabled chain). 503 when backend has no audit_chain repo (Db2 pre-live test).

**Recommended alerts:**

| Metric | Threshold | Severity |
|---|---|---|
| `oldest_unsealed_age_seconds` > 300 (5 min) | sealer wedged or sealer worker not running | warning |
| `oldest_unsealed_age_seconds` > 1800 (30 min) | sealer disk/auth failure | page |
| `unsealed_count` growth rate > sealer batch rate | sealer falling behind write traffic | warning; bump `batch_size` |
| `sealed_root_count` stops increasing for > 5 min while `unsealed_count` > 0 | sealer halted | page |
| `chain_enabled` flips false unexpectedly | env config drift / restart with missing env | warning |

### `GET /v1/audit/inclusion_proof?entry_id=<hex>`

Returns a full Merkle inclusion proof for one sealed entry — sibling-hash path from the entry's leaf up to the published global_root:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  'http://mnemos:5002/v1/audit/inclusion_proof?entry_id=08aa20ae...d92'
# {
#   "entry_id": "08aa20ae...",
#   "leaf_hash": "<hex sha256(entry_id||signature)>",
#   "leaf_index": 2,
#   "window_size": 5,
#   "global_root": "<hex>",
#   "global_seq": 3,
#   "proof": [
#     {"sibling": "<hex>", "position": "R"},
#     {"sibling": "<hex>", "position": "L"},
#     {"sibling": "<hex>", "position": "R"}
#   ]
# }
```

422 when entry is not yet sealed (sealer hasn't claimed its window). 404 when entry_id unknown.

Verifier-side using stdlib:

```python
from mnemos.audit.crypto import verify_inclusion

ok = verify_inclusion(
    leaf=bytes.fromhex(response["leaf_hash"]),
    proof=[(bytes.fromhex(p["sibling"]), p["position"]) for p in response["proof"]],
    expected_root=bytes.fromhex(response["global_root"]),
)
```

---

## What gets audited

| Operation | Write path | op = |
|---|---|---|
| Create | `POST /v1/memories` | `"create"` |
| Update | `PATCH /v1/memories/{id}` | `"update"` |
| Delete | `DELETE /v1/memories/{id}` | `"delete"` |
| Federation replicate (inbound) | `_store_memories` in federation pull | `"replicate"` |
| Archive | `POST /v1/admin/persephone/archive/{memory_id}` | `"archive"` |

Each entry signs over: `entry_id, memory_id (16-byte SHA-256-of-mem-id-str), prev_entry_id, prev_entry_hash, op, payload_hash, writer_id, writer_pubkey, signed_at`. The signature does NOT cover `global_root`, `global_seq`, or `signature` itself — the sealer stamps those columns post-sign without invalidating the entry.

`payload_hash` covers `(id, content, category, subcategory, metadata, embedding_hash)`. Embedding is included as SHA-256 of its float32 bytes, not the bytes themselves, so reshipping the same memory under v6.1 F-1 `copy_embeddings` does NOT perturb the audit hash for unchanged models.

---

## Failure modes + recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| Sealer logs `MNEMOS_AUDIT_ROOT_PRIVKEY is unset; required when MNEMOS_AUDIT_CHAIN is on` | Env not loaded into sealer process | Set env + restart; `load_root_keypair` is loud by design |
| `[AUDIT] write_audit_entry failed for op=create memory=mem_xxx` in api logs | Backend hiccup on insert_audit_entry | Audit row missed; memory row still committed. Sealer will skip the unsealed-history gap; rebuild from `git log`-style memory-table audit if forensics needed |
| 422 from `/v1/audit/inclusion_proof` | Sealer hasn't run yet for this entry | Wait one `poll_interval` cycle; entries seal in batches per `window_seconds` cadence |
| 500 from `/v1/audit/inclusion_proof` saying "computed root drift" | Sealer ↔ proof routine mismatch (bug) | File issue with the entry_id + global_root; bisect against any recent crypto-module commits |
| Federation replicas with mismatching global_root for same window | Split-brain or compromised peer | The Ed25519 root_signature on `memory_audit_roots` is the source of truth; reject peers whose pubkey doesn't match |

---

## Known limitations

1. **Schema memory_id is RAW(16)/BYTEA(16)/BLOB**, not the production string `mem_<ts>_<hex6>`. The route helper bridges via `SHA-256(memory_id_str)[:16]` (`memory_id_to_audit_bytes`). A schema refactor to a VARCHAR2(128) memory_id would let `memory_audit_chain` JOIN directly with the `memories` table without the hash bridge.

2. **JCS-lite**: production uses Python's `json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False)`. Bytewise identical to RFC 8785 for ASCII-only object keys (our case). Non-ASCII key surrogate-pair edge cases not handled. Swap in `rfc8785` PyPI when/if non-ASCII keys enter the canonical set.

3. **Cross-peer chain validation is passive.** The primary publishes `audit_latest_entry_id` + `audit_latest_entry_hash` per row in `/v1/federation/feed`. Replicas log primary's claimed chain head on inbound but **don't yet actively reject mismatched feeds** — hardening to halt-on-mismatch follows after the chain has been fielded at scale (risk: a transient peer bug could DoS a replica's pull loop if rejection is too aggressive).

4. **Archive audit entries are not atomic with the archive.** `POST /v1/admin/persephone/archive/{memory_id}` emits an `op="archive"` entry after the archive commits, in a separate transaction.

---

## Testing

```bash
.venv/bin/python -m pytest tests/test_audit_crypto.py tests/test_audit_writer.py \
  tests/test_audit_sealer.py tests/test_audit_route_helper.py \
  tests/test_audit_endpoints.py tests/test_audit_merkle_proof.py \
  tests/test_audit_repo_methods.py -v
# 73 audit tests, all pass on Python 3.11
```

For an end-to-end SQLite, sealer, and inclusion-proof example, see the usage
block in the `mnemos/audit/__init__.py` docstring.

---
