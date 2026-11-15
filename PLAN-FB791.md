# PLAN FB791 — `hnsw_index_options_fragment`

## Scope
Tests only. `mnemos/persistence/vector_tuning.py::hnsw_index_options_fragment`: the HNSW build-parameter clause every DDL site concatenates.

## Files
- `tests/test_hnsw_options_20261107.py` (new). No production change.

## Cases
The exact clause and its single leading space; equality with the bare constant plus that space; the values coming from the module constants and
those constants being pgvector's documented defaults; the no-trailing-space and single-line shape; stability across calls; the zero-argument
signature; the concatenation that motivates the helper; `__all__` membership; and the parser shape of the clause.

## Misconception
The brief expects unset options to be omitted and a dialect parameter. The helper takes no arguments, always emits both build parameters from
constants, and produces one Postgres-only form (the module records that Oracle has no vector index DDL). Both recorded as `DIVERGENCE:`.

## Risks
Pure constant-derived string; the assertions compare against the module constants as well as the literal so a constant bump fails loudly.
