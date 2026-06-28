# MNEMOS → native MIF 1.0 adoption (CHARON)

**Status:** in progress — Phase 0+1 landed (this branch). **ADR:** MNEMOS memory
`mem_1782679514682_85c817` (GRAEAE consult `e3f81616`, 2026-06-28).

MNEMOS adopts the **Modeled Information Format (MIF) 1.0** (<https://mif-spec.dev>)
as its **native, full-Level-3** portability format and **retires the MPF**
(Memory Portability Format) envelope. MNEMOS contributed the MIF 1.0 W3C-PROV
provenance layer (#85) and the vendor-neutral DocumentReference design (#84), so
this is dogfooding our own contribution.

## Decisions (operator-set, GRAEAE-validated)

1. **Native, not a projection.** MIF backs the internal memory model; the
   existing schema becomes a performance projection. The pivot representation is
   the MIF **JSON-LD concept** (what the published schemas validate); the
   canonical **Markdown** concept file round-trips losslessly to/from it.
2. **Base type = native `mif_type`** (`semantic|episodic|procedural`), set at
   ingest/classification. MIF's triad is *ontological* (the nature of the
   memory), not *topical* (the subject) — so MNEMOS `category` is **not** the
   base type. A `category → mif_type` map exists **only** as a migration
   fallback for legacy rows lacking `mif_type`.
3. **Identity** = deterministic **UUIDv5** of the `mem_<epochms>_<hash>` id
   (namespace `uuid5(URL, "https://mnemos.dev/id")`), emitted as
   `@id: urn:mif:<uuid>`; the original id is preserved in
   `properties["mnemos:id"]`. Same memory → same `@id`, always.
4. **Round-trip** is lossless and verified: `memory_to_concept` →
   `concept_to_markdown` → `markdown_to_concept` → `concept_to_memory` preserves
   identity, taxonomy, content, and provenance. Frontmatter is authoritative.
5. **Vault** (`namespace == "vault"`): MIF output **never** emits secret
   content — `content` becomes `[CONTENT ENCRYPTED]`, `provenance.sourceRef`
   becomes `redacted`, and the embedding `sourceText` / compression `summary`
   are omitted. (Mirrors the F1–F6 security review's redact-at-boundary rule.
   `redact_vault=False` is the authorized-export escape hatch.)
6. **MPF retirement** = hard cut + a one-time synchronous MPF→MIF migration;
   keep the MPF *reader* for a rollback window, then remove emission.

## Field mapping (MNEMOS row → MIF concept)

| MNEMOS | MIF JSON-LD | MIF Markdown frontmatter |
|---|---|---|
| `id` | `@id` (`urn:mif:` + UUIDv5) + `properties["mnemos:id"]` | `id` (UUID) |
| `mif_type` / category-map | `conceptType` | `type` |
| `content` | `content` | body |
| `created` / `updated` | `created` / `modified` | `created` / `modified` |
| `namespace` | `namespace` | `namespace` |
| `category`, `subcategory` | `tags` + `properties["mnemos:*"]` | `tags`, `extensions` |
| `source_agent`/`provider`/`model`/`session`, `quality_rating` | `provenance.{agent,agentVersion,sourceType,sourceRef,confidence}` | `provenance.*` |
| `embedding_model`, `embedding_dim` | `embedding.{model,dimensions,sourceText}` | `embedding.*` |
| `compressed_content` | `summary` (Level 3) | `summary` |

`properties` is a flat scalar map per the MIF schema; MNEMOS-native fields are
namespaced under a `mnemos:` key prefix so the round-trip is lossless and valid.

## Module + tests

- `mnemos/portability/mif.py` — the mapping + Markdown↔JSON-LD + schema
  validation (`validate_concept` against the vendored published schemas in
  `mnemos/portability/mif_schemas/`).
- `mnemos/portability/charon.py` — MIF **bundle** export/import: a directory of
  `<conceptType>/<uuid>.md` concept files (matching MIF's path-style relationship
  targets) + a `mif-manifest.json` index (spec version, schema `$id`, per-concept
  id/type/path/source). `export_bundle` schema-validates every concept before
  writing; `import_bundle` reads via the manifest or falls back to a `*.md` walk
  (so hand-authored MIF dirs import too). This is the CHARON format that replaces
  the MPF envelope.
- `tests/test_mif_portability.py` (13) + `tests/test_charon_bundle.py` (6) —
  schema validity, deterministic id, lossless round-trips, native-type override,
  vault redaction (incl. in-bundle), provenance (#85), foreign-concept import,
  bundle layout/manifest, manifest-less import.

## Remaining phases

- **2 — native model:** DB migration adding `mif_type` (required) + `mif_uuid`
  (indexed) + the MIF columns; ingest/classification sets `mif_type`; backfill.
- **3 — surfaces:** `/v1/export`,`/v1/import` + CLI emit/consume MIF; MPF→MIF
  migration job; MPF reader kept for rollback.
- **4 — retire MPF:** remove MPF emission; archive `mnemos-os/mpf`; full
  Level-3 wiring (citations, DocumentReference #84, PROV #85) + CI conformance
  gate (OKF + lossless round-trip).
