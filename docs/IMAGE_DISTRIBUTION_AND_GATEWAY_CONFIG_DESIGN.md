# Image Distribution & Config-Gated Gateway — Architecture Decision

**Status:** accepted 2026-06-29. Supersedes the practice of shipping a
separately-patched private gateway image.
**Scope:** how MNEMOS container images are layered for mass distribution, and
how provider/gateway behavior is made runtime-configurable rather than baked.
**Position in stack:** spans the PANTHEON provider facade (`docs/PANTHEON.md`)
and the published `ghcr.io/ncz-os` image set.

## Context

MNEMOS publishes **public, mass-distribution OSS images**:
`mnemos-core` (kernel: EPIMONE persistence) → `mnemos` (everything) →
`mnemos-enterprise` (+ Oracle/Db2/MySQL backends), built from `mnemos-core`
with the add-on wheels (`mnemos-pantheon`, `mnemos-graeae`, `mnemos-knemon`,
`mnemos-charon`) pip-installed. Arbitrary third parties pull and run these.

A deployment had been running a **patched variant** of the enterprise image: a
small overlay that copied modified gateway modules over the installed wheels
and set a routing env var. The overlay mixed two very different things:

1. **Generic improvements** — a pooled HTTP client, an optional shadow
   OpenAI-compatible app, a provider→env-var key-name convention.
2. **Deployment-specific data** — concrete provider endpoints, a hardcoded
   model-id remap table (legacy ids → a provider's native ids), and a chosen
   passthrough provider.

Baking (2) into an image is wrong for a mass-distribution artifact: it leaks
provider/routing specifics into public OSS and forces a private image fork.

## Decision

**One image for everyone; behavior is determined entirely at runtime.** No
separately-patched private image exists. A deployment that wants
gateway-override behavior sets environment + supplies a catalog on the *same*
official image; with neither, the image behaves as clean, provider-agnostic
upstream.

### 1. Decompose the overlay by "generic vs deployment-specific"

- **Generic → upstreamed into the public packages, unconditionally.** Pooled
  HTTP client; the shadow app (already gated by `MNEMOS_PANTHEON_ENABLED`);
  the provider→env-var key-name map (a naming convention — keys themselves are
  always injected via environment, never baked).
- **Deployment-specific → never in code or image; supplied at runtime.**
  Provider endpoints, model-id remaps, and passthrough selection.

### 2. Config boundary

- **Environment variables** carry only **toggles and small scalars** —
  feature enables, the passthrough-provider name, catalog path. Unset ⇒ clean
  upstream behavior (idempotent default).
- **A provider/model catalog** (a mounted config file, or the provider-registry
  table) carries the **structured, deployment-specific data**: provider
  endpoints and per-provider native model IDs/aliases. The public image ships a
  **neutral/empty** default catalog; a deployment supplies its own at deploy
  time. Structured data does **not** go in environment variables.
- The hardcoded model-id remap is **deleted**: native model IDs become catalog
  entries (this is the long-intended "the catalog carries native models per
  provider" end-state). The gateway resolves a request's model id through the
  catalog; with an empty catalog it passes through unchanged.

### 3. Image layering for mass distribution

- `mnemos-core → mnemos → mnemos-enterprise`, clean and provider-agnostic,
  built **only** from public source + public add-on wheels.
- **Multi-arch** (`amd64` + `arm64`) — adopters run both.
- The gateway-override logic lives **in** this public code but is
  config/env-gated, so the identical enterprise image runs clean for adopters
  and deployment-routed for an operator via env + catalog.
- A reproducible image-builder produces these from a pinned public ref and
  publishes to `ghcr.io/ncz-os`. The builder injects **no** deployment data.

## Consequences

- The published images are correct with **no overlay**; the prior private
  patch is retired in favor of env + a mounted catalog on the official image.
- Deployment routing (endpoints, model aliases, passthrough) is operator
  config, versioned in the operator's own (private) deployment repo — never in
  the OSS image. This mirrors the existing public/private boundary used for
  key injection.
- Requires: (a) upstreaming the generic overlay bits into the public packages
  and cutting new add-on wheels; (b) making provider endpoints + model remaps
  catalog-driven and removing the hardcoded tables; (c) a gitops image-builder
  for the multi-arch publish.

## Anti-patterns this rejects

- Baking provider endpoints, model IDs, or keys into a distributed image.
- A separately-patched private image (forking behavior by build, not config).
- Selecting behavior by **image tag** instead of runtime configuration.
- Putting the structured provider/model catalog into environment variables.

## Addendum: routing in source, connection idempotized

The dividing line within the gateway itself:

- **Routing logic stays in (public) source** — the router, catalog resolution
  (including the native `wire_model_id`), `RouteDecision`, fallback chains, and
  the provider-config *merge mechanism*. This is the portable intelligence.
- **The connection target is idempotent config** — the upstream endpoint a
  deployment actually dials (a direct provider URL, a local proxy, or a gateway
  VIP) is never baked; it is supplied at runtime.

Concretely, the PANTHEON gateway's provider table (`_PANTHEON_PROVIDER_DEFAULTS`)
declares provider **identity** only (key-name, wire API, enabled) — never the
endpoint **URL**. `_provider_config` resolves the connection by merging, in
increasing precedence, the in-source identity defaults, the engine provider
registry, and the operator provider config (`get_provider_config`), plus the
catalog's provider registry file. If no `base_url`/`url` resolves for a provider,
the gateway **fails closed** (`503 "no endpoint configured"`) rather than dialing
a baked vendor URL. The identical published image therefore connects wherever the
deployment's config points it; with no config it serves nothing.
