# node-relay bridge — network-isolated remote-worker integration

**Status:** Generalized 2026-06-17 from the original single-vendor design.
**Canonical code:** [`ncz-os/worker-hive-relay`](https://gitlab.com/ncz-os/worker-hive-relay)
(Python package `node_relay`; mirrored to GitHub + Codeberg). This document is
the design/decision record; the runnable code, install, and ops live in that
repo.

## What it is

node-relay integrates **any remote or airgapped node** as a hive worker even
when it **cannot reach the home network** — the hive bus, the NAS, and MNEMOS —
over LAN or VPN. The only requirement is that both the node and
the home fleet can reach a **public-cloud object store** — Google Cloud Storage
or Amazon S3 (or any S3-compatible endpoint). That bucket is the transport;
payloads are client-side AES-256-GCM sealed, so the cloud sees only ciphertext
and private MNEMOS context is never exposed. No specific hardware vendor,
inference vendor, or GPU is required.

> **History.** This bridge began (2026-06-02) as `spark_relay`, built to
> integrate one vendor's network-isolated box and its host-locked cloud models.
> Everything vendor-specific has since been removed: routing, transport, crypto,
> and executors are generic, and the package is `node_relay`. The sections below
> are the original architecture decisions, retained and de-branded.

## Transport: cloud-object relay with E2EE

Both the node and home reach the internet; only the home LAN is unreachable from
the node. So the bridge routes through a **cloud object store** with **client-
side AES-GCM E2EE** (one shared symmetric key; the cloud sees only ciphertext).
This beats an SSH-spool bridge on reliability (no single on-prem bridge host; the
bucket is highly available and decouples both fleets — jobs queue safely if
either side is down) while matching its security (E2EE) and atomicity
(conditional writes give exactly-once claim).

* **GCS** — conditional create via `ifGenerationMatch=0`, CAS via object
  generation.
* **S3** — conditional create via `If-None-Match: *`, CAS via `If-Match: <etag>`
  (both GA on AWS S3 PutObject; works on S3-compatible stores too).

Alternatives rejected at design time: Google Drive (eventual consistency / weak
locking); an inference+registry service (no queue — message-broker-on-registry
is an anti-pattern); an SSH-spool bridge host (single point of failure).

## Topology — two stateless pollers, one bucket, no bridge host

```
home enqueuer      hive job [eligible_hosts=<node>] → MNEMOS context → seal → bucket pending/<uuid>
node poller        list pending/ → atomic-claim claimed/<uuid> → local/cloud LLM exec → seal → bucket terminal/<uuid>
home reconciler    poll terminal/ → open → LAND patch as hive/node-<id> → PATCH hive done/failed → purge
```

The home side, which reaches the internet, writes encrypted job objects to
`pending/<uuid>.json.enc` directly; the node polls `pending/`, **claims via an
atomic conditional write** of `claimed/<uuid>` (loser backs off), runs a local
or cloud OpenAI-compatible model, and writes the single create-only
`terminal/<uuid>.json.enc`. Home polls `terminal/`, decrypts, lands any
review patch, reconciles to the hive, and purges.

### Atomic claim — lease semantics

The claim is a conditional create on `claimed/<uuid>` (exactly-once). A claim is
taken over only when older than the lease (`DEFAULT_LEASE_SECONDS`, the prior
worker presumed dead) via a compare-and-swap on the object version, so two
reclaimers can't both win. Idempotency key = hive job UUID = object name.

### Failure / recovery

- **Node offline** → home keeps enqueuing; the node drains the backlog on
  return. Jobs queue safely in the bucket.
- **Home offline** → the hive lease expires and the job reverts to queued; the
  enqueuer re-claims on recovery. Re-enqueue is harmless (same uuid; the node
  claim is create-only).
- **Crash mid-job** → the lease expiry lets another worker reclaim; the terminal
  object is create-only so done/failed can never both be recorded.
- **Undecryptable / mismatched payload** → quarantined as a durable terminal
  failure so the reconciler closes it out instead of blocking forever.

## MNEMOS context — retrieval happens at home

The node is treated as **stateless**: before queuing, the home fleet queries
MNEMOS (semantic search) for relevant context and injects snippets into the job
payload's `context` array. The node feeds the pre-packaged context straight into
its model prompt — it never needs its own retrieval store or fleet reachability.

## Patch landing — node work lands as reviewable git

The node holds no fleet git credentials, so it never pushes; it ships a
`git format-patch` through the bucket. The home-side **lander** applies the patch
onto a fresh checkout of the canonical repo and pushes it as a
`hive/node-<jobid>` review branch. Landing is idempotent (an existing branch for
the same full job id is reused; push races resolve to the winner's sha) and
credential-safe (owner-allowlisted tokens via `GIT_ASKPASS`, never in argv or
`.git/config`).

## Security model

- **E2EE**: AES-256-GCM, shared key never in the bucket or git; the GCM tag
  covers the framing header + the object's prefix/uuid (AAD), defeating
  cross-prefix / cross-job replay.
- **SSRF guard**: every job-supplied repo target resolves through an allowlist
  only; raw URLs and bare `owner/repo` hints are rejected.
- **Credential scoping**: Git tokens are attached only for allowlisted owner
  orgs, only over HTTPS, and only when the URL carries no embedded credentials.

## Migration & idempotency

Every `NODE_RELAY_*` environment variable falls back to its legacy `SPARK_*` /
`SPARK_HIVE_RELAY_*` name, and the crypto reader accepts blobs sealed with the
legacy magic. For a phased rollout where the home side upgrades before a remote
node, set `NODE_RELAY_WRITE_LEGACY_MAGIC=1` on the home side so an un-migrated
node (which only reads the old magic) keeps decrypting; drop the flag once every
participant is on `node_relay`. The `setup_relay.sh` bootstrap is idempotent
(never clobbers an existing key/env; reinstalls a unit only when changed).

See the canonical repo's [`node_relay/README.md`] and `node_relay/ops/README.md`
for install, backend selection, and the EnvironmentFile reference.
