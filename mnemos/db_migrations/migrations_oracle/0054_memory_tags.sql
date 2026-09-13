-- Lightweight, multi-valued memory tags for project scoping.
BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE memory_tags (
            memory_id VARCHAR2(100) NOT NULL,
            tag       VARCHAR2(255) NOT NULL,
            added_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            CONSTRAINT pk_memory_tags PRIMARY KEY (memory_id, tag),
            CONSTRAINT fk_memory_tags_memory FOREIGN KEY (memory_id)
                REFERENCES memories (id) ON DELETE CASCADE
        )';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'CREATE INDEX idx_memory_tags_tag ON memory_tags (tag)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
END;
/
