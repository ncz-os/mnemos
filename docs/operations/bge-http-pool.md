# BGE-M3 HTTP endpoint pool

The opt-in HTTP pool is intended for PYTHIA's MNEMOS service and a set of at
least two verified BGE-M3 endpoints. The current staged configuration uses two
nodes. It keeps the existing stored 1024D embedding space; it does not re-embed
memories, migrate schemas, or alter stored vectors.

## Configuration

```
MNEMOS_EMBED_BACKEND=http
MNEMOS_EMBED_HTTP_MODEL=bge-m3
MNEMOS_EMBEDDING_DIM=1024
MNEMOS_EMBED_HTTP_POOL_URLS=http://192.168.207.11:8081/v1/embeddings,http://192.168.207.78:8081/v1/embeddings
```

The pool replaces the primary/fallback chain only when explicitly configured.
It must never fall through to the legacy remote URLs or a local model with a
different vector space. With the pool unset, existing HTTP behavior remains.
The usual HTTP timeout and concurrency settings still bound requests. The
pool requires at least two distinct endpoint URLs; a trailing slash after
`/v1/embeddings` is normalized before requests are sent.

Both endpoints must serve verified BGE-M3 weights with matching preprocessing,
1024 finite dimensions and normalized results. Dimension checks cannot prove
semantic model identity; deployment verification must establish that separately.
Malformed responses, bad indices, nonfinite/zero vectors and dimension mismatch
must not enter storage. Pool failures yield unavailable embeddings, not zero-risk
or synthetic vectors.

## Deployment and acceptance

PYTHIA currently runs the rootful `mnemos-api` Podman container, managed by
`mnemos-api.service`, using `ghcr.io/ncz-os/mnemos-enterprise:7.0.5`. Its deployed
`/app/mnemos/runtime/embedder.py` matches canonical base `58cfea7` byte-for-byte
(SHA256 `928d156f6fe05fe7e04892ed4fb44e20e98aab01c04e1aa83c4ca5cada317bf0`).
The separate dirty `/opt/mnemos` checkout is not the deployment source.

1. Preserve the current container image digest, generated service and relevant
   environment file. Settings come from `/etc/mnemos/mnemos-api.env`; preserve
   separate secret files without printing or rewriting them.
2. Build a reviewed canonical image containing the pool commit on a build host.
   Never build from or patch the dirty `/opt/mnemos` checkout.
3. Confirm both endpoint identities and normalized 1024D parity with known inputs.
   Verify ACHILLES and HYDRA service persistence before moving CERBERUS BGE.
4. Install the reviewed image/config through the existing container lifecycle;
   set the pool environment above. No schema migration or re-encoding is needed.
5. Exercise sequential and concurrent requests through the actual MNEMOS runtime;
   prove both hosts receive calls. Check failure failover, cooldown/recovery,
   cancellation cleanup and batch ordering without modifying stored memories.
6. Verify service restart and boot persistence. Coordinate CERBERUS changes with the host owner and keep the verified
   HYDRA/ACHILLES primary/fallback path available until pool acceptance passes. Source tests alone do not activate
   the pool or prove production traffic uses it.

Rollback: restore the recorded prior image digest and environment file, remove
the pool variable, and restart the existing container service. The owner has staged HYDRA primary and ACHILLES fallback as the working
pre-pool configuration; restore the exact recorded working environment, not
an obsolete CERBERUS address after that service has moved.
No database or embedding rollback is necessary because this change does not
rewrite existing data. Retain both BGE services and artifacts for recovery.
