-- migration: 0031_memory_category_decay
-- target:    IBM Db2 12.1.x
-- purpose:   v6.2 M-2.2.4 — per-category temporal-decay table.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 3
-- Mirrors db/migrations_oracle/0031_memory_category_decay.sql.

CREATE TABLE memory_category_decay (
  category        VARCHAR(64)   NOT NULL,
  half_life_days  DECIMAL(10,2) NOT NULL,
  decay_kind      VARCHAR(16)   NOT NULL,
  floor           DECIMAL(5,4)  NOT NULL WITH DEFAULT 0,
  CONSTRAINT pk_memory_category_decay PRIMARY KEY (category),
  CONSTRAINT ck_memory_category_decay_kind
    CHECK (decay_kind IN ('exponential','sigmoid','none')),
  CONSTRAINT ck_memory_category_decay_floor
    CHECK (floor >= 0 AND floor <= 1),
  CONSTRAINT ck_memory_category_decay_halflife
    CHECK (half_life_days > 0)
);

-- Db2 lacks ON CONFLICT; use MERGE for idempotent seed.
MERGE INTO memory_category_decay d
USING (VALUES
  ('feedback',       CAST(365 AS DECIMAL(10,2)), 'exponential', CAST(0.5  AS DECIMAL(5,4))),
  ('rules',          CAST(730 AS DECIMAL(10,2)), 'exponential', CAST(0.7  AS DECIMAL(5,4))),
  ('user',           CAST(365 AS DECIMAL(10,2)), 'exponential', CAST(0.6  AS DECIMAL(5,4))),
  ('reference',      CAST(180 AS DECIMAL(10,2)), 'exponential', CAST(0.3  AS DECIMAL(5,4))),
  ('project',        CAST(60  AS DECIMAL(10,2)), 'exponential', CAST(0.05 AS DECIMAL(5,4))),
  ('facts',          CAST(90  AS DECIMAL(10,2)), 'exponential', CAST(0.2  AS DECIMAL(5,4))),
  ('infrastructure', CAST(30  AS DECIMAL(10,2)), 'exponential', CAST(0.1  AS DECIMAL(5,4))),
  ('credentials',    CAST(14  AS DECIMAL(10,2)), 'sigmoid',     CAST(0.0  AS DECIMAL(5,4))),
  ('working',        CAST(7   AS DECIMAL(10,2)), 'exponential', CAST(0.0  AS DECIMAL(5,4))),
  ('(default)',      CAST(180 AS DECIMAL(10,2)), 'exponential', CAST(0.1  AS DECIMAL(5,4)))
) AS src(category, half_life_days, decay_kind, floor)
ON d.category = src.category
WHEN NOT MATCHED THEN
  INSERT (category, half_life_days, decay_kind, floor)
  VALUES (src.category, src.half_life_days, src.decay_kind, src.floor);
