"""pgvector HNSW tuning constants and version helpers (F-8, 2026-10-31).

Source: Fable federation report (2026-10-31, commit 0073bb4), F-8 PERF vec:

    "F-8 PERF vec: ``SET LOCAL hnsw.ef_search = GREATEST(limit*4, 100)``
    per semantic query, declare ``m``/``ef_construction``, iterative scan
    where pgvector >= 0.8; HNSW is created with defaults
    (``schema.py:507``, ``migrations.sql:67``) and ``ef_search`` is never
    set, so each query sees 40 candidates which the namespace/owner/
    category WHERE then post-filters (``postgres.py:1588-1592``); Oracle
    has no vector index DDL, every search is an exact scan
    (``oracle.py:2326-2337``) (S)."

Why the tuning matters
----------------------

pgvector's HNSW default ``hnsw.ef_search`` is **40**. The HNSW scan
returns that many candidates and the SQL ``WHERE`` clause then filters
them. MNEMOS' semantic search always carries a tenant predicate
(``namespace``/``owner_id``/``group_ids``) plus optional
category/tag/provenance filters, so a query that asks for 10 rows can
legitimately need to look at hundreds of ANN candidates before the
filter leaves 10 rows behind. With the default 40 the planner stops
early and the query returns fewer rows than the caller asked for —
visible as "semantic search sometimes returns 3 rows when I asked for
10".

``GREATEST(limit*4, 100)`` scales the candidate window with the
requested row count and never drops below 100, which is enough headroom
for a tenant filter that selects a small fraction of the corpus while
staying far below the point where the HNSW scan degrades into a
sequential scan.

Two facts the version probe exists for
---------------------------------------

* **pgvector >= 0.8** added HNSW *iterative index scans*
  (``hnsw.iterative_scan = strict_order``/``relaxed_order``). Without
  it, pgvector 0.8+ still applies the "40 candidates then stop" rule
  once the filter is applied; with it the scan keeps pulling candidates
  until the ``LIMIT`` is satisfied. Older pgvector (< 0.8) does not
  know the GUC at all, so setting it raises
  ``ERROR: unrecognized configuration parameter "hnsw.iterative_scan"``.
  The probe therefore gates the GUC on the *installed* version.

* **``m`` and ``ef_construction``** are build-time parameters of the
  index, not query-time knobs. Declaring them explicitly makes the
  index reproducible across standups instead of silently taking
  whatever the extension's defaults were on the day the index was
  built. ``m=16`` is the pgvector default for cosine distance and
  ``ef_construction=64`` is the pgvector default; they are declared
  explicitly so the intent is recorded in the DDL and a future bump is
  a one-line change rather than an archaeology exercise.

Oracle
------

Oracle has **no vector index DDL** in this codebase: there is no
``CREATE INDEX ... USING hnsw`` equivalent, so every Oracle semantic
search is an exact scan over the whole corpus
(``mnemos/persistence/oracle.py``). None of the tuning in this module
applies to Oracle, and the Oracle backend does not import it. The
constant :data:`ORACLE_HAS_VECTOR_INDEX_DDL` exists so the fact is
stated in one place instead of being implied by the absence of code.
"""

from __future__ import annotations

__all__ = [
    "HNSW_EF_SEARCH_FLOOR",
    "HNSW_EF_SEARCH_MULTIPLIER",
    "HNSW_INDEX_EF_CONSTRUCTION",
    "HNSW_INDEX_M",
    "ORACLE_HAS_VECTOR_INDEX_DDL",
    "PGVECTOR_ITERATIVE_SCAN_MIN_VERSION",
    "HNSW_INDEX_OPTIONS_SQL",
    "hnsw_ef_search_for",
    "pgvector_version_at_least",
]


#: Multiplier applied to the requested row count to size the ANN
#: candidate window.
HNSW_EF_SEARCH_MULTIPLIER: int = 4

#: Floor for the ANN candidate window. A query asking for 1 row still
#: gets a 100-candidate window so a selective tenant filter can still
#: find its rows.
HNSW_EF_SEARCH_FLOOR: int = 100

#: ``m`` — the number of bi-directional links per HNSW node. pgvector's
#: default for cosine distance; declared explicitly so the index build
#: is reproducible.
HNSW_INDEX_M: int = 16

#: ``ef_construction`` — the size of the dynamic candidate list used
#: while BUILDING the HNSW index. pgvector's default; declared
#: explicitly for the same reason as ``m``.
HNSW_INDEX_EF_CONSTRUCTION: int = 64

#: Minimum pgvector version that understands ``hnsw.iterative_scan``.
#: Released 2024-07; before it the GUC does not exist and setting it
#: raises ``unrecognized configuration parameter``.
PGVECTOR_ITERATIVE_SCAN_MIN_VERSION: tuple[int, ...] = (0, 8)

#: Oracle has no vector index DDL — every Oracle semantic search is an
#: exact scan. Stated here (rather than left implicit) so the gap is
#: visible to anyone reading the tuning code.
ORACLE_HAS_VECTOR_INDEX_DDL: bool = False


#: The ``WITH (...)`` options clause for the HNSW index, built from the
#: constants above so every DDL site (the legacy PG bootstrap migration,
#: the runtime shape repair in ``mnemos/persistence/schema.py`` and the
#: installer's index rebuild) declares the same build parameters.
HNSW_INDEX_OPTIONS_SQL: str = f"WITH (m = {HNSW_INDEX_M}, ef_construction = {HNSW_INDEX_EF_CONSTRUCTION})"


def hnsw_ef_search_for(limit: int) -> int:
    """Return the ``hnsw.ef_search`` value for a query returning ``limit`` rows.

    Implements ``GREATEST(limit*4, 100)``: the candidate window scales
    with the requested row count and never falls below 100.

    A non-positive or non-numeric ``limit`` is treated as "as many as
    the floor allows" — the floor is the safe direction (a wider
    candidate window never returns fewer rows than requested, only
    costs more CPU).
    """
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        return HNSW_EF_SEARCH_FLOOR
    return max(requested * HNSW_EF_SEARCH_MULTIPLIER, HNSW_EF_SEARCH_FLOOR)


def pgvector_version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    """Return whether an installed pgvector ``extversion`` is at least ``minimum``.

    ``extversion`` is a free-form string ("0.8.0", "0.7.4", "0.8.0-pg13").
    Only the leading numeric components are considered; anything that
    cannot be parsed compares as *older* (the conservative direction:
    do not set a GUC the installed extension may not know).
    """
    if not version:
        return False
    parts: list[int] = []
    for chunk in str(version).split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return False
    # Pad the installed version so "0.8" compares equal to "0.8.0".
    padded = tuple(parts) + (0,) * (len(minimum) - len(parts)) if len(parts) < len(minimum) else tuple(parts)
    return padded >= minimum
