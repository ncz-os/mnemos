-- ---------------------------------------------------------------------------
-- MNEMOS v6.3 MORPHEUS run-lifecycle table for MariaDB 11.7+ (item 11/11a).
--
-- MariaDB has no native ``JSON`` column type — its JSON is a LONGTEXT
-- alias with a ``json_valid()`` CHECK constraint, and ``CAST(... AS JSON)``
-- is unsupported (causes a syntax error). This migration mirrors the
-- MySQL 0061c_morpheus_runs.sql parity migration but drops the MySQL
-- ``JSON`` column in favour of LONGTEXT + ``json_valid()`` CHECK.
--
-- The Python repository layer for MariaDB (``MariadbMorpheusRepository``)
-- writes the ``config`` JSON as a plain string and the ``json_valid()``
-- CHECK rejects non-JSON values at insert time. No CAST needed because
-- MariaDB stores JSON text natively.
--
-- All other columns match the Postgres canonical 19-column shape:
--
--   id, started_at, finished_at, status, phase, triggered_by,
--   window_started_at, window_ended_at, window_hours, cluster_min_size,
--   memories_scanned, clusters_found, summaries_created,
--   memories_consolidated, clusters_consolidated,
--   triples_extracted, memories_processed_for_extraction,
--   error, config, namespace
--
-- MariaDB's ``UUID()`` function (in MariaDB 10.7+) generates a
-- ``CHAR(36)`` value identical to MySQL 8.0.13+'s ``UUID()`` so the
-- ``MysqlMorpheusRepository.begin_run`` impl that uses
-- ``DEFAULT (UUID())`` + SELECT-by-key works verbatim after the
-- MariaDB-specific config-column swap.
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
    -- LONGTEXT + json_valid() CHECK is MariaDB's native JSON storage;
    -- the CHECK rejects non-JSON values at insert time so callers
    -- can't poison the column with malformed text.
    config              LONGTEXT            NULL,
    namespace           VARCHAR(256)        NULL,

    PRIMARY KEY (id),
    CONSTRAINT morpheus_runs_status_check
        CHECK (status IN ('running','success','failed','rolled_back')),
    CONSTRAINT morpheus_runs_triggered_by_check
        CHECK (triggered_by IN ('cron','manual','api')),
    CONSTRAINT morpheus_runs_config_is_json
        CHECK (config IS NULL OR JSON_VALID(config)),

    -- Inline KEY indexes match the MySQL parity migration.
    KEY idx_morpheus_runs_status (status),
    KEY idx_morpheus_runs_started (started_at DESC),
    KEY idx_morpheus_runs_namespace (namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

-- The runner's ``rollback_run`` SQL references ``morpheus_extract_run_memories``
-- on every backend. MariaDB inherits the table from the existing
-- 0050_lifecycle_workers.sql migration runner in MariadbBackend.open()
-- — verified by ``grep -rn morpheus_extract_run_memories
-- mnemos/db_migrations/migrations_mariadb/``.