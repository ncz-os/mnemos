# MNEMOS Modularization Audit and Install-Time Component Selection

Date: 2026-06-14

## Executive summary

MNEMOS already has the raw packaging primitives for modular deployment: `pyproject.toml` exposes per-component extras and bundles, and `mnemos/core/extras.py` can probe installed optional dependencies at runtime. The missing layer was deployment composition. `MNEMOS_PROFILE` historically selected backend/auth/worker-count defaults, but it did not declare which runtime services should run. Operators had to know and set separate knobs for NATS, federation, webhooks, distillation, and PANTHEON.

This audit recommends treating profile and service composition as adjacent but distinct concepts:

- **Profile**: storage/auth/rate-limit/worker defaults (`server`, `edge`, `dev`).
- **Component selection**: install-time pip extras/bundles (`--with server,ml,nats,pantheon`).
- **Service manifest**: profile + selected components => runtime workers/consumers/fanouts.

Backwards compatibility is preserved: no installer `--with` and no `MNEMOS_PROFILE_SERVICES_ENABLED=true` keeps legacy defaults and explicit env flags behave as before.

## Current-state assessment

Verified in canonical master:

1. `pyproject.toml` declares optional dependencies for individual components (`morpheus`, `persephone`, `pantheon`, `kronos`, `kronos-gpu`, `knossos`, `apollo`, `artemis`, `nats`, `hot`, `edge`, `sqlite`, `tracing`, `structlog`, `docling`, `build`) and composite bundles (`edge`, `server`, `ml`, `interop`, `full`).
2. `mnemos/core/extras.py` has `EXTRA_PROBES` and `FEATURE_BUNDLES` for runtime availability checks and UX hints.
3. `mnemos/core/config.py` has `PROFILE_DEFAULTS` for `server`, `edge`, and `dev`, plus `personal -> edge` aliasing.
4. Installer entry points are `install.sh` and `python -m mnemos.installer`; migrations are orchestrated in `mnemos/installer/db.py`.
5. The compression stack is collaborative: PERSEPHONE + MORPHEUS + APOLLO + ARTEMIS work as one operational slice. PANTHEON is an optional model-proxy surface, not required for a normal server.

## Gaps and fixes

### Gap A: profile was not service composition

Before this change, a server profile implied Postgres/Redis-like defaults but did not automatically enable NATS fanout, federation consumers, webhook NATS triggers, or distillation according to a declarative deployment shape.

Implemented design:

- New module: `mnemos/core/services.py`.
- New config group: `[services]` / `MNEMOS_PROFILE_SERVICES_ENABLED` and `MNEMOS_SELECTED_COMPONENTS`.
- Runtime resolution order:
  1. legacy defaults when profile-services are not managed;
  2. profile manifest when managed;
  3. selected component/bundle enables;
  4. explicit env flags last.

This keeps no-flags behavior compatible while allowing installers/operators to opt into declarative composition.

### Gap B: no install-time component selection

Implemented UX:

```bash
bash install.sh --profile server --with server,ml
bash install.sh --profile edge --with edge,interop
bash install.sh --profile server --with server,pantheon   # explicit PANTHEON opt-in
MNEMOS_INSTALL_INTERACTIVE_COMPONENTS=1 bash install.sh --wizard
```

`install.sh` now forwards arguments without whitespace splitting and can prompt for components when `MNEMOS_INSTALL_INTERACTIVE_COMPONENTS=1`. The Python installer accepts `--with <comma-list>` and wires it to:

- pip extras (via `pip install .[extras]` in the managed venv);
- persisted runtime service flags (`MNEMOS_PROFILE_SERVICES_ENABLED`, `MNEMOS_SELECTED_COMPONENTS`, and existing per-service env names);
- a migration selector hook (`selected_migration_groups`) that scopes the ordered Postgres migration list for explicit component selections while preserving full-chain behavior when no components are selected.

### Gap C: PANTHEON default-on risk in server bundle

Recommendation: keep PANTHEON opt-in for deployment composition. The historical `pyproject.toml` `server` extra still includes PANTHEON for packaging back-compat, but the installer-managed `server` component expands to `nats + persephone` and **does not enable PANTHEON**. Operators opt in with `--with pantheon` or an explicit `MNEMOS_PANTHEON_ENABLED=true`.

### Gap D: migration parity drift

Recommendation: make full backend migration parity a CI and pre-install gate:

```bash
python scripts/check_migration_parity.py --mode full
```

This catches schema drift such as historically missing `consolidated_into` or `pantheon_routing_audit` columns on Oracle/Db2 before an installer attempts a backend-specific deployment. The installer’s `selected_migration_groups()` is the explicit component boundary for Postgres migration filtering; omitted component selection still applies the complete ordered chain for legacy compatibility.

## Profile -> services mapping

| Profile | Managed default services | Disabled by default | Notes |
| --- | --- | --- | --- |
| `edge` | deletion request worker | NATS, federation sync, distillation, PERSEPHONE archival, PANTHEON, webhook NATS trigger | Single-node SQLite/laptop/edge appliance. |
| `server` | distillation worker, deletion request worker, PERSEPHONE archival, federation sync, federation NATS consumers, webhook NATS trigger, NATS webhook/federation fanout | PANTHEON routing/proxy unless explicitly selected | Production server substrate. NATS still requires `MNEMOS_NATS_URL`; federation still requires peers. |
| `dev` | distillation worker, deletion request worker | NATS, federation sync, PERSEPHONE archival, PANTHEON | SQLite + debug-friendly defaults. |

Explicit environment flags remain authoritative. Examples: `MNEMOS_PANTHEON_ENABLED=true`, `MNEMOS_NATS_WEBHOOKS_ENABLED=false`, `MNEMOS_FEDERATION_ENABLED=false`.

## Component x deployment-type matrix

Legend: **Required**, **Recommended**, **Optional**, **Off**.

| Component / subsystem | Primary server | Replica server | Edge node | DR / standby |
| --- | --- | --- | --- | --- |
| Core API | Required | Required | Required | Required |
| Storage | Postgres required | Postgres required | SQLite recommended | Postgres replica or restored standby required |
| Redis/rate-limit | Recommended | Recommended | Off | Optional |
| NATS substrate | Recommended | Recommended | Off by default | Recommended if active failover consumes events |
| Federation HTTP sync | Recommended | Recommended | Off unless participating in federation | Recommended for catch-up |
| Federation NATS consumers | Recommended | Recommended with queue group | Off | Optional, enable during active promotion/catch-up |
| Webhook delivery workers | Recommended | Recommended | Optional | Off unless DR is active |
| Webhook NATS trigger | Recommended | Recommended with queue group | Off | Optional |
| Compression/distillation worker | Recommended | Recommended, but size worker count carefully | Off by default | Optional; usually off until promotion |
| PERSEPHONE archival | Recommended | Recommended if replicas can safely process archival | Off by default | Optional |
| MORPHEUS/APOLLO/ARTEMIS | Recommended as ML bundle | Recommended as ML bundle | Optional on capable edge | Optional |
| KRONOS | Optional forecasting/anomaly | Optional | Optional | Optional |
| KNOSSOS | Optional interop/graph | Optional | Optional | Optional |
| PANTHEON | Optional opt-in | Optional opt-in | Off | Off unless DR active and explicitly required |
| Tracing/structured logs | Recommended | Recommended | Optional | Recommended |
| Docling/import tooling | Optional | Optional | Optional | Optional |

## Proposed install UX

Primary non-interactive examples:

```bash
# Back-compatible install: legacy behavior, no managed service manifest.
bash install.sh

# Managed production server without PANTHEON.
bash install.sh --profile server --with server

# Server with compression/ML and interop.
bash install.sh --profile server --with server,ml,interop

# Explicitly opt into PANTHEON model proxy/audit consumer.
bash install.sh --profile server --with server,pantheon

# Edge appliance with SQLite extras only.
bash install.sh --profile edge --with edge
```

Interactive component selection is opt-in to avoid surprising curl-pipe installs:

```bash
MNEMOS_INSTALL_INTERACTIVE_COMPONENTS=1 bash install.sh --wizard
```

## Implementation notes

- `mnemos/core/services.py` is the declarative service manifest and resolver.
- Runtime settings expose `settings.services.resolution`.
- Lifecycle hooks now consult `service_enabled(...)` before launching federation, webhook NATS, PERSEPHONE, and PANTHEON audit services.
- PANTHEON routes now use the resolved service state, preserving explicit `MNEMOS_PANTHEON_ENABLED=true` behavior.
- Installer config persists selected components in `config.toml` and service env files.
- `mnemos/installer/db.py:selected_migration_groups()` scopes optional Postgres migration slices for explicit selections and leaves no-selection installs on the legacy full chain.

## CI / pre-install gates

Required before broadening migration scoping across every backend:

1. `python scripts/check_migration_parity.py --mode full` across Postgres, SQLite, Oracle, Db2, and MySQL where supported.
2. Unit tests for `resolve_profile_services` and installer component normalization.
3. Upgrade-path tests proving no `--with` preserves existing defaults.
4. NATS multi-replica tests with queue groups for webhook and federation consumers.
