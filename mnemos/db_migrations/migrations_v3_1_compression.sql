-- ---------------------------------------------------------------------------
-- MNEMOS v3.1 migration: competitive-selection compression platform
--
-- Three tables wire the v3.1 compression contest:
--
--   memory_compression_queue       Pending compression work for the
--                                  distillation worker. One row per
--                                  (memory, contest) task. Reasons:
--                                  on_write, manual, scheduled, reprocess.
--
--   memory_compression_candidates  Full contest log. One row per engine
--                                  attempt per contest round. Winners AND
--                                  losers both recorded with their scoring
--                                  fields, judge model, timing, and
--                                  reject_reason. This is the audit trail
--                                  that lets operators see why a given
--                                  engine was chosen (or rejected).
--
--   memory_compressed_variants     Latest winning variant per memory.
--                                  Read path for the currently-served
--                                  compressed form. Superseded rows remain
--                                  in memory_compression_candidates with
--                                  is_winner=TRUE but no longer referenced
--                                  from here.
--
-- Competitive selection: the manager scores every eligible engine's output
-- via a composite function (quality * ratio_term * speed_factor), applies
-- a quality floor to disqualify damaged output, selects the highest
-- composite_score, writes the winner into memory_compressed_variants, and
-- keeps every candidate in memory_compression_candidates — including
-- losers — with its score and reject_reason. See compression/manager.py.
--
-- Backward compatibility: the v3.0 inline compression fields on `memories`
-- (compressed_content, compression_method, compression_ratio, etc.) remain
-- populated by the v3.1 manager with the current winner so existing read
-- paths continue to work unchanged. They will be deprecated in a later
-- release once all readers migrate to memory_compressed_variants.
--
-- Idempotent: all CREATEs are IF NOT EXISTS; no destructive ALTERs.
-- ---------------------------------------------------------------------------

-- memory_compression_queue ---------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_compression_queue (
    id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id         TEXT          NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    owner_id          TEXT          NOT NULL DEFAULT 'default',
    reason            VARCHAR(32)   NOT NULL,
    status            VARCHAR(16)   NOT NULL DEFAULT 'pending',
    priority          SMALLINT      NOT NULL DEFAULT 0,
    scoring_profile   VARCHAR(32)   NOT NULL DEFAULT 'balanced',
    attempts          SMALLINT      NOT NULL DEFAULT 0,
    enqueued_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    error             TEXT,

    CONSTRAINT mcq_status_valid CHECK (status IN ('pending','running','done','failed')),
    CONSTRAINT mcq_reason_valid CHECK (reason IN ('on_write','manual','scheduled','reprocess')),
    CONSTRAINT mcq_scoring_profile_valid
        CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom'))
);

CREATE INDEX IF NOT EXISTS idx_mcq_ready
    ON memory_compression_queue(status, priority DESC, enqueued_at)
    WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_mcq_memory ON memory_compression_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcq_owner  ON memory_compression_queue(owner_id);

-- memory_compression_candidates ----------------------------------------------
-- Full contest log. Every engine attempt per contest round lands here,
-- winners and losers alike. compression_ratio upper bound is permissive
-- (up to 10) because losing engines can produce output larger than the
-- original; we still record those for audit.
CREATE TABLE IF NOT EXISTS memory_compression_candidates (
    id                  UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id           TEXT             NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    owner_id            TEXT             NOT NULL DEFAULT 'default',
    contest_id          UUID             NOT NULL,
    engine_id           VARCHAR(32)      NOT NULL,
    engine_version      VARCHAR(32),
    compressed_content  TEXT,
    original_tokens     INTEGER          NOT NULL,
    compressed_tokens   INTEGER,
    compression_ratio   DOUBLE PRECISION,
    quality_score       DOUBLE PRECISION,
    speed_factor        DOUBLE PRECISION,
    composite_score     DOUBLE PRECISION,
    scoring_profile     VARCHAR(32)      NOT NULL DEFAULT 'balanced',
    elapsed_ms          INTEGER,
    judge_model         VARCHAR(64),
    gpu_used            BOOLEAN          NOT NULL DEFAULT FALSE,
    is_winner           BOOLEAN          NOT NULL DEFAULT FALSE,
    reject_reason       VARCHAR(32),
    manifest            JSONB,
    created             TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT mcc_ratio_range
        CHECK (compression_ratio IS NULL
               OR (compression_ratio > 0 AND compression_ratio <= 10)),
    CONSTRAINT mcc_quality_range
        CHECK (quality_score IS NULL
               OR (quality_score >= 0 AND quality_score <= 1)),
    CONSTRAINT mcc_speed_range
        CHECK (speed_factor IS NULL
               OR (speed_factor >= 0 AND speed_factor <= 1)),
    CONSTRAINT mcc_reject_reason_valid
        CHECK (reject_reason IS NULL OR reject_reason IN
            ('quality_floor','no_output','error','inferior','timeout','disabled')),
    CONSTRAINT mcc_scoring_profile_valid
        CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom')),
    CONSTRAINT mcc_winner_has_output CHECK (
        NOT is_winner OR (compressed_content IS NOT NULL
                          AND compression_ratio IS NOT NULL
                          AND composite_score IS NOT NULL)
    ),
    CONSTRAINT mcc_loser_has_reason CHECK (
        is_winner OR reject_reason IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_mcc_memory  ON memory_compression_candidates(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcc_contest ON memory_compression_candidates(contest_id);
CREATE INDEX IF NOT EXISTS idx_mcc_memory_winner
    ON memory_compression_candidates(memory_id) WHERE is_winner;
CREATE INDEX IF NOT EXISTS idx_mcc_owner   ON memory_compression_candidates(owner_id);
CREATE INDEX IF NOT EXISTS idx_mcc_engine  ON memory_compression_candidates(engine_id);

-- memory_compressed_variants -------------------------------------------------
-- One row per memory pointing at the current winning candidate.
-- Content is inlined (not just a candidate-id pointer) so variant reads
-- don't require a join and so candidate pruning policies can't drop
-- actively-served content by accident.
CREATE TABLE IF NOT EXISTS memory_compressed_variants (
    memory_id            TEXT             PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    owner_id             TEXT             NOT NULL DEFAULT 'default',
    winner_candidate_id  UUID             REFERENCES memory_compression_candidates(id) ON DELETE SET NULL,
    engine_id            VARCHAR(32)      NOT NULL,
    engine_version       VARCHAR(32),
    compressed_content   TEXT             NOT NULL,
    compressed_tokens    INTEGER,
    compression_ratio    DOUBLE PRECISION NOT NULL,
    quality_score        DOUBLE PRECISION,
    composite_score      DOUBLE PRECISION NOT NULL,
    scoring_profile      VARCHAR(32)      NOT NULL DEFAULT 'balanced',
    judge_model          VARCHAR(64),
    selected_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT mcv_ratio_range
        CHECK (compression_ratio > 0 AND compression_ratio <= 10),
    CONSTRAINT mcv_quality_range
        CHECK (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)),
    CONSTRAINT mcv_scoring_profile_valid
        CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom'))
);

CREATE INDEX IF NOT EXISTS idx_mcv_owner  ON memory_compressed_variants(owner_id);
CREATE INDEX IF NOT EXISTS idx_mcv_engine ON memory_compressed_variants(engine_id);
