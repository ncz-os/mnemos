"""MORPHEUS — dream-state memory consolidation.

The off-peak worker that processes accumulated memory into shaped form.
Named after the Greek god of dreams (μορφεύς, "the one who shapes")
and the Matrix character of the same name — both meanings land:
MORPHEUS shapes raw memories into clearer summaries, and (in later
slices) wakes the corpus from its raw-data simulation into something
the operator can actually use.

Architecture per GRAEAE consensus (consultation 2026-04-25):

  v1 — slice 1 (this scaffold)
    * morpheus_runs table + per-row morpheus_run_id tagging.
    * Runner skeleton; phases stubbed but the audit + rollback shape
      is real.
    * Admin API: list runs, get run details, manually trigger,
      rollback by run_id.

  v1 — slice 2 (synthesis)
    * REPLAY: scan memories from last N hours.
    * CLUSTER: cosine-similarity over pgvector embeddings (no LLM).
    * SYNTHESISE: per-cluster LLM pass producing summary memories.
    * COMMIT: insert with provenance='morpheus_local',
      morpheus_run_id=<run>, source_memories=[<original ids>].

  v2 (mutation paths — risky, ship after v1 is proven)
    * EXTRACT: KG triples mined from verbatim_content.
    * CONSOLIDATE: merge near-duplicate clusters into a canonical
      with permission_mode=400 read-only pointers on originals.
    * ARCHIVE: cold-set rotation (PERSEPHONE subsystem).

Rollback contract: every change tags morpheus_run_id; undo is
DELETE FROM memories WHERE morpheus_run_id = X. v2 mutation paths
will additionally restore consolidated_into pointers on rollback.
"""
