-- ---------------------------------------------------------------------------
-- MNEMOS v6.3 MORPHEUS run-lifecycle table for MySQL 9.0+ (item 11/11a).
--
-- Postgres's canonical ``morpheus_runs`` shape (split across
-- migrations_v3_3_morpheus.sql + namespace/consolidate/extract
-- migrations, 19 columns total) was missing entirely from MySQL/MariaDB
-- before item 11a — the runner's ``begin_run`` / ``set_phase`` /
-- ``update_counters`` / ``increment_extract_counters`` / ``finish_run``
-- / ``fail_run`` / ``sweep_orphan_runs`` / ``rollback_run`` calls fell
-- off a cliff on these backends because the table simply didn't exist.
--
-- This migration creates the canonical 19-column table in MySQL 9.0+
-- dialect. MariaDB has its own parity migration
-- (``0061c_morpheus_runs_parity_mariadb.sql``) since MariaDB's JSON
-- column is a LONGTEXT alias with a ``json_valid()`` CHECK (no native
-- ``CAST(... AS JSON)`` and the inline ``CHECK (config IS JSON)``
-- constraint differs).
--
-- Dialect adaptations vs Postgres:
--
-- * ``UUID`` (Postgres) -> ``CHAR(36)`` + ``DEFAULT (UUID())`` for id.
--   The Python repository layer (``MysqlMorpheusRepository.begin_run``)
--   lets the table default populate the id and SELECTs it back
--   afterwards since MySQL has no RETURNING clause.
-- * ``JSONB`` (Postgres) -> ``JSON`` for the ``config`` column. MySQL
--   9.0+ ships native JSON; MariaDB does not (separate migration).
-- * ``TIMESTAMPTZ`` (Postgres) -> ``DATETIME(6)``. UTC is pinned at
--   session level by ``SET time_zone='+00:00'`` in ``MysqlBackend.open``,
--   so DATETIME(6) values are wall-clock UTC. The runner's
--   ``begin_run`` writes ``started_at`` with a Python-side ``datetime.now(timezone.utc)``
--   value and MySQL coerces ISO-8601 text into DATETIME(6) transparently.
-- * ``CHECK (status IN (...))`` (Postgres) -> inline CHECK constraint.
--   MySQL honours inline CHECK since 8.0.
-- * ``DEFAULT gen_random_uuid()`` (Postgres) -> ``DEFAULT (UUID())``
--   (MySQL 8.0.13+ ships ``UUID()`` as a built-in function).
--
-- The runner-side ``begin_run`` Python impl emits
-- ``VALUES (... 'running')`` explicitly so the table default is only
-- the id. The CHECK constraint matches Postgres's canonical allowed
-- status values; ``rolled_back`` is the value ``rollback_run`` writes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS morpheus_runs (
    id                  CHAR(36)        NOT NULL DEFAULT (UUID()),
    started_at          DATETIME(6)     NOT NULL DEFAULT NOW(6),
    finished_at         DATETIME(6)         NULL,
    status              VARCHAR(16)     NOT NULL DEFAULT 'running',
    phase               VARCHAR(64)         NULL,
    triggered_by        VARCHAR(32)     NOT NULL DEFAULT 'cron',

    window_started_at   DATETIME(6)         NULL,
    window_ended_at     DATETIME(6)         NULL,
    window_hours        INT             NOT NULL DEFAULT 168,
    cluster_min_size    INT             NOT NULL DEFAULT 3,

    memories_scanned    INT             NOT NULL DEFAULT 0,
    clusters_found      INT             NOT NULL DEFAULT 0,
    summaries_created   INT             NOT NULL DEFAULT 0,

    memories_consolidated    INT         NOT NULL DEFAULT 0,
    clusters_consolidated    INT         NOT NULL DEFAULT 0,

    triples_extracted               INT    NOT NULL DEFAULT 0,
    memories_processed_for_extraction INT   NOT NULL DEFAULT 0,

    error               TEXT                NULL,
    config              JSON                NULL,
    namespace           VARCHAR(256)        NULL,

    PRIMARY KEY (id),
    CONSTRAINT morpheus_runs_status_check
        CHECK (status IN ('running','success','failed','rolled_back')),
    CONSTRAINT morpheus_runs_triggered_by_check
        CHECK (triggered_by IN ('cron','manual','api')),

    -- Inline KEY indexes match Postgres's partial / ordering indexes.
    -- MySQL 8 does NOT support CREATE INDEX IF NOT EXISTS, so the
    -- indexes must be defined inline in CREATE TABLE on the MySQL
    -- arm. See 0040_webhook_repository.sql for the same rationale.
    KEY idx_morpheus_runs_status (status),
    KEY idx_morpheus_runs_started (started_at DESC),
    KEY idx_morpheus_runs_namespace (namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

-- ALTER TABLE ... morpheus_extract_run_memories is created by the
-- existing v5.0 migration (migrations/0050_lifecycle_workers.sql) on
-- Postgres. MySQL/MariaDB inherit the same join table from migration
-- 0050_lifecycle_workers.sql — verified against the existing schema
-- runner in MysqlBackend.open() / MariadbBackend.open(). The runner's
-- ``rollback_run`` SQL references ``morpheus_extract_run_memories``
-- via the same column shape on every backend.