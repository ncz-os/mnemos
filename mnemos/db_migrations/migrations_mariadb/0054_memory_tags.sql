-- Lightweight, multi-valued memory tags for project scoping.
-- memory_id matches the ASCII memories.id used by the MariaDB vector schema.
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id VARCHAR(64) CHARACTER SET ascii NOT NULL,
    tag       VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    added_at  DATETIME(6) NOT NULL DEFAULT NOW(6),
    PRIMARY KEY (memory_id, tag),
    KEY idx_memory_tags_tag (tag),
    CONSTRAINT fk_memory_tags_memory FOREIGN KEY (memory_id)
        REFERENCES memories (id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
