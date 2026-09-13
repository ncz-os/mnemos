-- 0061d_morpheus_phase_parity.sql — PostgreSQL numbered parity anchor.
-- Postgres's memories.consolidated_at/morpheus_run_id/source_memories/
-- provenance/triples_extracted_at columns and the morpheus_extract_failures
-- table already exist (created by
-- db/migrations_v5_4_1_morpheus_extract_failures.sql, an old flat-file
-- migration predating the parity contract); this item (11c) only needed to
-- retcon the divergent Oracle/DB2/MySQL/MariaDB/SQLite shapes to match it.
-- No Postgres schema change needed.
SELECT 1;
