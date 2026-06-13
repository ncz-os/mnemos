-- migration: 0002_model_registry
-- target:    IBM Db2 12.1.5 (Oracle Compat mode)
-- purpose:   Db2 port of the canonical model_registry (db/migrations_model_registry.sql,
--            PG). Referenced by db/migrations_db2/0037_deepseek_direct_provider_seed.sql
--            and the Db2ConsultationAuditRepository read methods
--            (fetch_recommended_model / fetch_available_models / lookup_provider_for_model /
--            fetch_model_provider). DB2-compat: VARCHAR ids (HEX(GENERATE_UNIQUE()) at
--            insert), SMALLINT booleans, CLOB JSON (no IS JSON CHECK), DECIMAL pricing,
--            TIMESTAMP WITH TIME ZONE defaults. ; terminator.

CREATE TABLE model_registry (
    id                    VARCHAR(36)              NOT NULL,
    provider              VARCHAR(50)              NOT NULL,
    model_id              VARCHAR(255)             NOT NULL,
    display_name          VARCHAR(255),
    family                VARCHAR(100),
    context_window        INTEGER,
    max_output_tokens     INTEGER,
    input_cost_per_mtok   DECIMAL(12, 6)           DEFAULT 0,
    output_cost_per_mtok  DECIMAL(12, 6)           DEFAULT 0,
    cache_read_per_mtok   DECIMAL(12, 6)           DEFAULT 0,
    cache_write_per_mtok  DECIMAL(12, 6)           DEFAULT 0,
    capabilities          CLOB(1M),
    available             SMALLINT                 NOT NULL DEFAULT 1,
    deprecated            SMALLINT                 NOT NULL DEFAULT 0,
    arena_score           DECIMAL(8, 2),
    arena_rank            INTEGER,
    graeae_weight         DECIMAL(5, 4),
    first_seen            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_payload           CLOB(1M),
    CONSTRAINT pk_model_registry PRIMARY KEY (id),
    CONSTRAINT uq_model_registry_provider_model UNIQUE (provider, model_id)
);

CREATE INDEX idx_model_registry_provider ON model_registry (provider);

CREATE INDEX idx_model_registry_available ON model_registry (available);

CREATE INDEX idx_model_registry_arena_score ON model_registry (arena_score DESC);
