-- migration: v6_2_category_decay_sqlite
-- target:    SQLite 3.40+
-- purpose:   v6.2 M-2.2.4 — per-category temporal-decay table (SQLite shape).
-- design:    docs/v6.2-nexus-pattern-adoption.md § 3.
-- Mirrors db/migrations/0031_memory_category_decay.sql.

CREATE TABLE IF NOT EXISTS memory_category_decay (
  category        TEXT NOT NULL,
  half_life_days  REAL NOT NULL CHECK (half_life_days > 0),
  decay_kind      TEXT NOT NULL CHECK (decay_kind IN ('exponential','sigmoid','none')),
  floor           REAL NOT NULL DEFAULT 0 CHECK (floor >= 0 AND floor <= 1),
  PRIMARY KEY (category)
);

INSERT OR IGNORE INTO memory_category_decay (category, half_life_days, decay_kind, floor) VALUES
  ('feedback',       365, 'exponential', 0.5),
  ('rules',          730, 'exponential', 0.7),
  ('user',           365, 'exponential', 0.6),
  ('reference',      180, 'exponential', 0.3),
  ('project',        60,  'exponential', 0.05),
  ('facts',          90,  'exponential', 0.2),
  ('infrastructure', 30,  'exponential', 0.1),
  ('credentials',    14,  'sigmoid',     0.0),
  ('working',        7,   'exponential', 0.0),
  ('(default)',      180, 'exponential', 0.1);
