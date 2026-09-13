--#SET TERMINATOR @
-- Lightweight, multi-valued memory tags for project scoping.
CREATE TABLE memory_tags (
    memory_id VARCHAR(100) NOT NULL,
    tag       VARCHAR(255 CODEUNITS32) NOT NULL,
    added_at  TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT pk_memory_tags PRIMARY KEY (memory_id, tag),
    CONSTRAINT fk_memory_tags_memory FOREIGN KEY (memory_id)
        REFERENCES memories (id) ON DELETE CASCADE
)@

CREATE INDEX idx_memory_tags_tag ON memory_tags (tag)@
