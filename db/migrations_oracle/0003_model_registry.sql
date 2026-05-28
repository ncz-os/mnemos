-- KNEMON: model_registry port to Oracle 23ai
-- Ports mnemos/db/migrations_model_registry.sql (Postgres) to Oracle.
-- ID uses VARCHAR2(100) DEFAULT LOWER(SYS_GUID()) to match the canonical
-- MNEMOS Oracle convention in 0001_core_schema.sql (memories, sessions, etc).
-- TEXT[] -> CLOB CHECK IS JSON; BOOLEAN -> NUMBER(1); JSONB -> CLOB CHECK IS JSON;
-- TIMESTAMPTZ -> TIMESTAMP WITH TIME ZONE.

CREATE TABLE IF NOT EXISTS model_registry (
    id                    VARCHAR2(100) DEFAULT LOWER(SYS_GUID()) PRIMARY KEY,
    provider              VARCHAR2(50) NOT NULL,
    model_id              VARCHAR2(200) NOT NULL,
    display_name          VARCHAR2(400),
    family                VARCHAR2(200),
    context_window        NUMBER(10),
    max_output_tokens     NUMBER(10),
    capabilities          CLOB CHECK (capabilities IS JSON),
    input_cost_per_mtok   NUMBER(12,6) DEFAULT 0,
    output_cost_per_mtok  NUMBER(12,6) DEFAULT 0,
    cache_read_per_mtok   NUMBER(12,6) DEFAULT 0,
    cache_write_per_mtok  NUMBER(12,6) DEFAULT 0,
    available             NUMBER(1) DEFAULT 1 NOT NULL CHECK (available IN (0,1)),
    deprecated            NUMBER(1) DEFAULT 0 NOT NULL CHECK (deprecated IN (0,1)),
    arena_score           NUMBER(8,2),
    arena_rank            NUMBER(10),
    graeae_weight         NUMBER(5,4),
    first_seen            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_seen             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_synced           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    raw_payload           CLOB CHECK (raw_payload IS JSON),
    CONSTRAINT uq_model_registry_provider_model UNIQUE (provider, model_id)
);

CREATE INDEX IF NOT EXISTS idx_mr_provider ON model_registry(provider);
CREATE INDEX IF NOT EXISTS idx_mr_avail ON model_registry(available);
CREATE INDEX IF NOT EXISTS idx_mr_weight ON model_registry(graeae_weight);
CREATE INDEX IF NOT EXISTS idx_mr_family ON model_registry(family);
CREATE INDEX IF NOT EXISTS idx_mr_last_synced ON model_registry(last_synced);

CREATE TABLE IF NOT EXISTS model_registry_sync_log (
    id                  VARCHAR2(100) DEFAULT LOWER(SYS_GUID()) PRIMARY KEY,
    provider            VARCHAR2(50) NOT NULL,
    synced_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    models_found        NUMBER(10) DEFAULT 0 NOT NULL,
    models_added        NUMBER(10) DEFAULT 0 NOT NULL,
    models_updated      NUMBER(10) DEFAULT 0 NOT NULL,
    models_deprecated   NUMBER(10) DEFAULT 0 NOT NULL,
    error               VARCHAR2(4000)
);
