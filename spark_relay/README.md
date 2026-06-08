# Spark Relay — E2EE GCS bridge for the network-isolated DGX Spark

Integrates the NVIDIA DGX Spark `spark-0c53` (GB10, host-locked NGC models) as a
hive worker even though it **cannot reach the home fleet** (192.168.207.x). Both
sides reach the internet, so a GCS bucket is the transport; payloads are
end-to-end encrypted (AES-256-GCM), so the cloud sees only ciphertext.

Full design + rationale: [`../docs/SPARK_HIVE_BRIDGE.md`](../docs/SPARK_HIVE_BRIDGE.md).

```
enqueuer (PYTHIA)   hive nvidia-job → MNEMOS context → seal → bucket pending/
spark_poller (Spark) list pending → atomic-claim → NGC exec → seal → bucket terminal/
reconciler (PYTHIA)  poll terminal/ → open → hive done/failed + ARGONAS fan-out → purge
```

The atomic claim is a GCS conditional create (`if_generation_match=0`) on
`claimed/<uuid>` — exactly-once with no lock server.

## Modules

| File | Side | Role |
|------|------|------|
| `relay_crypto.py` | both | AES-256-GCM seal/open (`SHR1` framed, header as AAD) |
| `relay_client.py` | both | GCS put/list/**claim**/get/purge (lazy `google.cloud` import) |
| `bridge_common.py` | PYTHIA | hive + MNEMOS HTTP clients, backoff |
| `enqueuer.py` | PYTHIA | drain hive nvidia jobs → bucket |
| `reconciler.py` | PYTHIA | bucket results → hive terminal status |
| `spark_poller.py` | Spark | claim + execute via pluggable `Executor` |

## Install

```bash
pip install -r spark_relay/requirements.txt   # both sides
```

## Secrets (never in the bucket, never committed)

| Env var | Meaning |
|---------|---------|
| `SPARK_HIVE_RELAY_E2EE_KEY` | base64 AES-256 key — **identical** on both sides |
| `SPARK_HIVE_RELAY_GCS_SA` | path to the `relay-rw` service-account JSON |
| `SPARK_HIVE_RELAY_BUCKET` | `spark-hive-relay-bkt` |
| `NGC_API_KEY` (Spark) | host-locked NGC inference key |
| `MNEMOS_TOKEN` (PYTHIA) | MNEMOS bearer for context retrieval |

Canonical values live in `~/.api_keys_master.json` → `spark_hive_relay`.

## Run

```bash
# PYTHIA
python -m spark_relay.enqueuer   --interval 15
python -m spark_relay.reconciler --interval 15
# Spark
python -m spark_relay.spark_poller --interval 10 --executor ngc
```

Or install the systemd units in `ops/` (see `ops/README`). `--once` does a single
sweep (useful for cron or smoke tests).

## Executor integration

`spark_poller` ships `NgcChatExecutor` (calls the NGC model, returns its output).
The full agentic loop — apply edits, `git commit`, `git push` to GitHub, return
`commit_sha`/`branch` — is the `TODO(agentic)` in `NgcChatExecutor.execute`.
Implement `Executor.execute` and wire it in `make_executor`.

## Tests

```bash
pytest tests/spark_relay/        # pure: crypto + flow, no GCS/network needed
```

The live conditional-claim primitive is validated against real GCS during
provisioning (see the bridge doc's smoke section).
