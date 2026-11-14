# ADR D-MN1 — Auth default and `/metrics` (MN-01, MN-19)

**Status:** Proposed
**Date:** 2026-11-07
**Decision owner:** operator (Jason Perlow)
**Scope:** documentation decision only. No production code changes in this ADR.
**Backlog source:** D-MN1 groups MN-01 and MN-19 from `refactor-research-20261031/BACKLOG-FROM-FABLE-20261107.md`.
That file is **not present in this repository** and could not be read (see §2.4).

---

## 1. Decision needed

We must choose the **default** for two switches: whether the REST API requires a login
(auth) when MNEMOS runs as a shared server, and whether the Prometheus `/metrics`
endpoint also requires one. We must decide **now** because whatever the default is,
it ships to every operator who runs the published container image, and today the
two switches disagree with each other. If we do nothing, one deployment shape
(`docker compose` from this repo) keeps running with auth **off and port 5002
published on every interface**, and `/metrics` keeps a fail-closed auth path that
**cannot succeed on SQLite**, the default backend.

**Recommended:** keep auth ON for the `server` profile with **one API key per agent**,
bind the shipped compose file to **loopback only**, and make `/metrics` require the
same auth whenever REST auth is on.

---

## 2. Context — what we verified

Every claim below was checked against the source on branch `hv/adr-d-mn1-20261107`
(base `master`). Line numbers are from that checkout. Claims I could **not** verify
are listed in §2.4 and are not load-bearing for the recommendation.

### 2.1 The current auth default (MN-01)

- Settings default: `MNEMOS_PROFILE` defaults to `personal`
  (`mnemos/core/config.py:347`). `personal` is an alias for `edge`
  (`mnemos/core/config.py:61-63`, `:1416-1430`).
- Profile defaults: the `edge` profile sets `auth_enabled: False`
  (`mnemos/core/config.py:40-48`); the `server` profile sets `auth_enabled: True`
  (`mnemos/core/config.py:31-39`). Verified live: with no env set,
  `get_settings()` reports `profile=edge, auth.enabled=False`; with
  `MNEMOS_PROFILE=server` it reports `auth.enabled=True`.
- Auth off is not "read-only": every request is handed a synthetic **root**
  principal (`mnemos/api/dependencies.py:386-389`, singleton built at `:336-343`
  with `role="root"`). So auth off on a reachable port is full admin access.
- The guard against that is `mnemos/core/network_guard.py`. It refuses to start an
  unauthenticated API on a non-loopback bind (`refusal_reason`, `:67-92`), and it is
  enforced from two places: `mnemos serve` (`mnemos/cli/main.py:658-676`) and the
  ASGI lifespan (`mnemos/core/lifecycle.py:807-810`). A direct uvicorn launch (how
  the images start) has no recorded bind and is treated as reachable, i.e. fails
  closed. This is the "off-host-bind guard" the backlog refers to.
- **Gap we found:** the compose file in this repo runs the service with
  `MNEMOS_PROFILE: personal` and publishes `'5002:5002'` — i.e. on **all host
  interfaces** (`docker-compose.yml:280` and `:281-282`). That combination resolves
  to edge defaults (auth off) *and* a published port. It does not currently crash,
  because `mnemos serve` records the container-internal bind it validated; the
  published-port mapping is what makes it reachable. So the shipped compose recipe
  is exactly the case the backlog's "bind compose to loopback" half-fix is about.
- Compare the other compose file: `docker-compose.staging.yml` sets
  `MNEMOS_PROFILE: staging` (`:287`). **`staging` is not a valid profile** —
  `normalize_profile` accepts only `server`, `edge`, `dev`
  (`mnemos/core/config.py:1424-1430`); verified live, it raises
  `ValueError: Unsupported MNEMOS profile 'staging'`. That file is therefore
  broken independently of this decision. It is noted here because it shows the
  "set a profile by hand" approach is fragile; this ADR does not fix it.

### 2.2 The published image already turns auth on

`Dockerfile.core:199-200`, `Dockerfile.oracle:57-58` and `Dockerfile.split:135-136`
all set `MNEMOS_PROFILE=server` **and** `MNEMOS_AUTH_ENABLED=true`
(`docs/INSTALL.md:267`). Container operators on the published image therefore
already run auth on. The only auth-off-and-published path is a **source-built
compose** run — the `docker-compose.yml` defect above.

### 2.3 `/metrics` (MN-19)

- `/metrics` is mounted on the main API router (`mnemos/api/main.py:415`) and is
  served on the same port as everything else (`:5002`).
- It has its own, **separate** switch: `metrics_require_auth`, default `False`
  (`mnemos/core/config.py:624-632`), read only by
  `mnemos/core/observability.py:302-313`. It is **not** tied to `auth.enabled`.
  Verified live: `MNEMOS_PROFILE=server` gives `auth.enabled=True` but
  `metrics_require_auth=False`.
- When the switch is on, the gate (`observability.py:316-397`) does **not** use the
  normal auth dependency. It (a) reads `request.app.state.pool`, which is a raw
  asyncpg pool and is `None` on every non-Postgres backend
  (`mnemos/core/lifecycle.py:825-894`, `:940`), and (b) runs Postgres-style SQL
  with `$1` placeholders. On SQLite — the default backend — the pool is `None`,
  so the gate returns **503 to every scrape, including a valid key**
  (`observability.py:362-368`). Verified live against a TestClient with
  `pool=None`: `503 {"detail":"Database pool not available for auth check"}`.
- This is the ABC-first rule in `AGENTS.md` being violated: the gate reaches for
  the raw pool instead of `OAuthRepository.lookup_api_key`
  (`mnemos/persistence/base.py:2171-2181`), which is backend-neutral and already
  used by the real auth path (`mnemos/api/dependencies.py:352-406`).
- Consequences by backend, from the code above: **SQLite** — gate on means
  metrics are un-scrapable; **Postgres/Oracle/Db2/MySQL/MariaDB** — the raw pool
  is absent for all but Postgres, so the same 503 applies. Only Postgres can pass
  the gate today.
- Docs currently state the opposite of the recommendation: `docs/OBSERVABILITY.md:36-37`
  describes `/metrics` as "unauthenticated by default" and `:53-66` presents
  bearer auth as an optional extra.

### 2.4 What I could not verify

- **The backlog file itself.** `refactor-research-20261031/BACKLOG-FROM-FABLE-20261107.md`
  is not in this checkout (searched the whole repo). MN-01/MN-19 are used here as
  given by the task text only; their internal wording (severity, owner, dates) is
  unverified. Anything in this ADR that depends on backlog *language* rather than
  backlog *ids* is written from the three-option brief in the task.
- **Whether production fleets actually scrape `/metrics` today** and with what
  credentials. No operator inventory was available; §6 asks.
- **MariaDB auth.** `_supports_oauth_persistence` is set on SQLite, Postgres,
  Oracle and MySQL but the MariaDB file does not set it, so MariaDB likely 503s on
  auth (`dependencies.py:364-372`). Not exercised here; an open question, not a
  decision input.

---

## 3. Options

### (a) Keep auth off behind the off-host-bind guard

- **Benefit:** zero setup for a single user on a laptop; no keys to mint or rotate;
  no behaviour change for `edge`/`dev` users. The guard already prevents the
  classic mistake of an open, uncontrolled port.
- **Cost:** "auth off" is root for everyone who can reach the port, so the guard
  must be perfect. It is not: the shipped `docker-compose.yml` publishes 5002 on
  all interfaces with the auth-off profile (§2.1). Also, nothing protects
  `/metrics` at all in this option.
- **Risk:** one published port or one relaxed firewall rule = full admin access to
  memory and audit data. Multi-tenant or multi-agent use is impossible.
- **Reversibility:** high — this is the status quo; turning auth on later is a
  config change plus key distribution.

### (b) Auth on for the `server` profile, one API key per agent, plus loopback compose

- **Benefit:** matches what the published image already does (§2.2); each client
  (Claude Code, Codex, Cursor, a scraper) gets its own key, so revoking one agent
  does not break the others, and `api_keys.last_used` gives per-agent usage. The
  off-host-bind guard stays as a backstop. Fixes the compose defect by binding it
  to `127.0.0.1`.
- **Cost:** operators must create at least one user and mint keys before clients
  work. Key creation is already backend-neutral via
  `POST /users/{id}/apikeys` (`mnemos/api/routes/admin.py:115-171`) and lookup via
  `OAuthRepository.lookup_api_key`, so the cost is documentation and a first-run
  step, not new code.
- **Risk:** if key distribution is sloppy, agents share one key and the per-agent
  benefit is lost (it becomes root-for-everyone-again, just with a password on
  it). Also, the session-cookie path hardcodes `role="user"`
  (`dependencies.py:433-441`), so browser flows cannot mint keys — the operator
  needs the initial root key out-of-band.
- **Reversibility:** high — `MNEMOS_AUTH_ENABLED=false` plus loopback bind restores
  option (a) for one box; the schema and routes already exist.

### (c) Also require auth on `/metrics`

- **Benefit:** removes the last unauthenticated data surface; `stats` and other
  operator views already require root (`mnemos/api/routes/health.py:89-90`), so
  this makes `/metrics` consistent with the rest of the operator surface.
- **Cost:** whatever is chosen must actually work. As shipped, turning the switch
  on **breaks scraping on every backend except Postgres** (§2.3), so option (c) as
  currently implemented is not deliverable without routing the gate through
  `OAuthRepository`. A scraper needs its own root key.
- **Risk:** if shipped as-is, dashboards and alerts go dark (503) on SQLite and
  Oracle — a silent monitoring outage on the default deployment.
- **Reversibility:** high — it is one env var, once the implementation is correct.

---

## 4. Recommendation

**Take (b) + (c): auth on for the `server` profile with one API key per agent, and
`/metrics` requires auth exactly when REST auth is on.** Bind `docker-compose.yml`
to loopback so the source-built compose path stops publishing an auth-off API.

Reasoning:

1. The published image already ships auth on (§2.2); (a) would mean *changing the
   container default back*, leaving docs and image disagreeing. (b) makes docs,
   source compose, and image agree.
2. Auth off is root (`dependencies.py:386-389`). Per-agent keys give revocation and
   per-agent attribution without RBAC work, and the routes and repository methods
   already exist — so this is a documentation and first-run decision, not a code
   project.
3. `/metrics` should not sit on a second switch that can disagree with REST auth.
   **But** the current gate is broken on SQLite (and every backend but Postgres,
   §2.3) and must be re-implemented through `OAuthRepository` before (c) ships —
   otherwise the decision turns monitoring off, not on.
4. Binding compose to loopback is one line and removes the actual exposure.

**Strongest argument against:** per-agent keys are only as good as their
distribution — in practice most operators will mint one key and paste it into every
client, so (b) buys authentication but not the per-agent isolation that justifies it,
and the setup step is exactly the friction that pushes people toward (a) or
`MNEMOS_ALLOW_UNAUTHENTICATED_NETWORK_BIND=1`.

---

## 5. Implementation plan

Ordered so no step leaves monitoring dark.

1. **Docs.** Rewrite `docs/OBSERVABILITY.md:36-37` and `:53-66` so `/metrics`
   follows REST auth, and document the scraper key. Update `docs/INSTALL.md` with
   the per-profile auth default and the per-agent key recipe. *(Docs only.)*
2. **Fix the `/metrics` gate (prerequisite for step 5).** Replace the raw
   `app.state.pool` + `$1` lookup in `mnemos/core/observability.py:362-386` with
   `OAuthRepository.lookup_api_key` via `request.app.state.persistence_backend`,
   as the REST path does (`dependencies.py:352-406`). Keep the fail-closed 503
   for "settings unreadable" and for "backend does not implement OAuth".*
3. **Derive the `/metrics` default from REST auth.** Make
   `metrics_require_auth` default to `None`/unset and resolve it in
   `_build_settings` the way MCP auth is resolved
   (`mnemos/core/config.py:_apply_mcp_auth_default`, `:1649-1659`): explicit env
   wins, otherwise it equals `auth.enabled`. Keep the env var so an operator can
   still force it either way.
4. **Bind compose to loopback.** `docker-compose.yml:281-282` →
   `'127.0.0.1:5002:5002'`, and raise `MNEMOS_PROFILE` to `server` (or add
   `MNEMOS_AUTH_ENABLED: 'true'`) so the published-port and auth defaults no
   longer contradict each other.
5. **Default profile unchanged.** The `edge` default stays for bare pip/dev users;
   this ADR turns auth on for the `server` profile, not for laptops.
6. **Tests.** (i) In `tests/test_observability.py`, a valid root key must return
   **200** on SQLite — today every case stubs an asyncpg pool (`:341-356`), which
   is why the breakage was never caught. (ii) `metrics_require_auth` follows
   `auth.enabled` when unset and an env var overrides it. (iii) A config test that
   `docker-compose.yml` publishes only loopback, in the style of
   `tests/test_no_private_default_endpoints.py::test_compose_embed_host_points_at_compose_ollama_service`.
7. **Rollout.** Docs in one release note; the step-3 behaviour change in a minor
   version, announced, old behaviour still reachable via
   `MNEMOS_METRICS_REQUIRE_AUTH=false`. Postgres fleets scraping with a root key
   see no change.
8. **Rollback.** One env var: `MNEMOS_AUTH_ENABLED=false` for REST (loopback bind
   still required), `MNEMOS_METRICS_REQUIRE_AUTH=false` for `/metrics`. No schema
   change, so no migration.

**Backlog items this unblocks:** MN-01 (auth default) and MN-19 (`/metrics` auth),
i.e. the whole of D-MN1. It also removes one raw-pool access outside
`mnemos/persistence/`, which is a step toward F-14 / the `lint:abc-first` ratchet
described in `AGENTS.md`.

\* Step 2 is production code and is **out of scope for this ADR's author**; it is
flagged as a prerequisite so the operator does not adopt (c) before it lands.

---

## 6. Open questions for the operator

1. Do any fleets scrape `/metrics` today, and are they Postgres-backed? If some are
   SQLite, step 3 must wait for step 2 or their dashboards go dark.
2. Is 30 s of revocation lag acceptable? Keys are cached for
   `MNEMOS_AUTH_CACHE_TTL_SECONDS` (default 30, `mnemos/core/config.py:1118`), so a
   revoked agent key keeps working up to 30 s per worker.
3. Who mints the first root key, and how is it delivered? The session-cookie path
   cannot mint keys (it hardcodes `role="user"`), so this is out-of-band today.
4. Is the `docker-compose.staging.yml` `MNEMOS_PROFILE: staging` breakage (§2.1)
   fixed in this work or tracked separately? It currently cannot start.
5. Should MariaDB be in scope for auth (§2.4), or is it SQLite/Postgres only?

---

## 7. Status

**Proposed.** Awaiting operator decision on §6. No code merged under this ADR.
