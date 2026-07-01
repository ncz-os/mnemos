-- migration: 0031_memory_category_decay
-- target:    PostgreSQL 16
-- purpose:   v6.2 M-2.2.4 — per-category temporal-decay table.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 3
-- Mirrors db/migrations_oracle/0031_memory_category_decay.sql.

CREATE TABLE IF NOT EXISTS memory_category_decay (
  category        VARCHAR(64)   NOT NULL,
  half_life_days  NUMERIC(10,2) NOT NULL,
  decay_kind      VARCHAR(16)   NOT NULL,
  floor           NUMERIC(5,4)  NOT NULL DEFAULT 0,
  CONSTRAINT pk_memory_category_decay PRIMARY KEY (category),
  CONSTRAINT ck_memory_category_decay_kind
    CHECK (decay_kind IN ('exponential','sigmoid','none')),
  CONSTRAINT ck_memory_category_decay_floor
    CHECK (floor >= 0 AND floor <= 1),
  CONSTRAINT ck_memory_category_decay_halflife
    CHECK (half_life_days > 0)
);

INSERT INTO memory_category_decay (category, half_life_days, decay_kind, floor) VALUES
  ('feedback',       365, 'exponential', 0.5),
  ('rules',          730, 'exponential', 0.7),
  ('user',           365, 'exponential', 0.6),
  ('reference',      180, 'exponential', 0.3),
  ('project',        60,  'exponential', 0.05),
  ('facts',          90,  'exponential', 0.2),
  ('infrastructure', 30,  'exponential', 0.1),
  ('credentials',    14,  'sigmoid',     0.0),
  ('working',        7,   'exponential', 0.0),
  ('(default)',      180, 'exponential', 0.1)
ON CONFLICT (category) DO NOTHING;
