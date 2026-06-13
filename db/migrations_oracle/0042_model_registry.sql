-- 0042_model_registry.sql — model_registry for Oracle (cross-backend parity).
-- Oracle lacked a model_registry table entirely; OracleConsultationsRepository
-- resolve_tier_lineup / resolve_models query it. Ports
-- db/migrations_db2/0002_model_registry.sql to Oracle dialect (VARCHAR2/NUMBER/
-- CLOB/SYSTIMESTAMP; SMALLINT->NUMBER(1)).

CREATE TABLE IF NOT EXISTS model_registry (
    id                    VARCHAR2(36)  NOT NULL,
    provider              VARCHAR2(50)  NOT NULL,
    model_id              VARCHAR2(255) NOT NULL,
    display_name          VARCHAR2(255),
    family                VARCHAR2(100),
    context_window        NUMBER,
    max_output_tokens     NUMBER,
    input_cost_per_mtok   NUMBER(12,6) DEFAULT 0,
    output_cost_per_mtok  NUMBER(12,6) DEFAULT 0,
    cache_read_per_mtok   NUMBER(12,6) DEFAULT 0,
    cache_write_per_mtok  NUMBER(12,6) DEFAULT 0,
    capabilities          CLOB,
    available             NUMBER(1) DEFAULT 1 NOT NULL,
    deprecated            NUMBER(1) DEFAULT 0 NOT NULL,
    arena_score           NUMBER(8,2),
    arena_rank            NUMBER,
    graeae_weight         NUMBER(5,4),
    first_seen            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_seen             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_synced           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    raw_payload           CLOB,
    CONSTRAINT pk_model_registry PRIMARY KEY (id),
    CONSTRAINT uq_model_registry_provider_model UNIQUE (provider, model_id)
);

CREATE INDEX idx_model_registry_provider ON model_registry (provider);

CREATE INDEX idx_model_registry_available ON model_registry (available);
