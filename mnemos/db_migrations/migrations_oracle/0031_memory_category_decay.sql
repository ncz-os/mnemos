-- migration: 0031_memory_category_decay
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   v6.2 M-2.2.4 — per-category temporal-decay table for the
--            retrieval scoring pipeline. Seeded with reasonable defaults;
--            admin endpoint allows runtime overrides.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 3
--
-- Decay kinds:
--   - 'exponential' = base_score * max(floor, exp(-ln(2) * t / half_life))
--   - 'sigmoid'     = base_score * (1 / (1 + exp(k*(t - half_life))))
--   - 'none'        = base_score unchanged (recency_weight still applies)
--
-- Idempotency: guarded by USER_TABLES + MERGE for seed rows.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MEMORY_CATEGORY_DECAY';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE memory_category_decay (
        category        VARCHAR2(64)  NOT NULL,
        half_life_days  NUMBER(10, 2) NOT NULL,
        decay_kind      VARCHAR2(16)  NOT NULL,
        floor           NUMBER(5, 4)  DEFAULT 0 NOT NULL,
        CONSTRAINT pk_memory_category_decay PRIMARY KEY (category),
        CONSTRAINT ck_memory_category_decay_kind
          CHECK (decay_kind IN ('exponential','sigmoid','none')),
        CONSTRAINT ck_memory_category_decay_floor
          CHECK (floor >= 0 AND floor <= 1),
        CONSTRAINT ck_memory_category_decay_halflife
          CHECK (half_life_days > 0)
      )
    ]';
  END IF;
END;
/

-- Seed default categories. MERGE so re-running the migration after admin
-- edits is a no-op for changed rows (only fills in missing defaults).
MERGE INTO memory_category_decay d
USING (
  SELECT 'feedback'       AS category, 365  AS half_life_days, 'exponential' AS decay_kind, 0.5  AS floor FROM dual UNION ALL
  SELECT 'rules'          AS category, 730  AS half_life_days, 'exponential' AS decay_kind, 0.7  AS floor FROM dual UNION ALL
  SELECT 'user'           AS category, 365  AS half_life_days, 'exponential' AS decay_kind, 0.6  AS floor FROM dual UNION ALL
  SELECT 'reference'      AS category, 180  AS half_life_days, 'exponential' AS decay_kind, 0.3  AS floor FROM dual UNION ALL
  SELECT 'project'        AS category, 60   AS half_life_days, 'exponential' AS decay_kind, 0.05 AS floor FROM dual UNION ALL
  SELECT 'facts'          AS category, 90   AS half_life_days, 'exponential' AS decay_kind, 0.2  AS floor FROM dual UNION ALL
  SELECT 'infrastructure' AS category, 30   AS half_life_days, 'exponential' AS decay_kind, 0.1  AS floor FROM dual UNION ALL
  SELECT 'credentials'    AS category, 14   AS half_life_days, 'sigmoid'     AS decay_kind, 0.0  AS floor FROM dual UNION ALL
  SELECT 'working'        AS category, 7    AS half_life_days, 'exponential' AS decay_kind, 0.0  AS floor FROM dual UNION ALL
  SELECT '(default)'      AS category, 180  AS half_life_days, 'exponential' AS decay_kind, 0.1  AS floor FROM dual
) src
ON (d.category = src.category)
WHEN NOT MATCHED THEN
  INSERT (category, half_life_days, decay_kind, floor)
  VALUES (src.category, src.half_life_days, src.decay_kind, src.floor);

COMMIT;
