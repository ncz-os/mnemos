-- 0008_user_groups.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE user_groups (
    user_id VARCHAR(36) NOT NULL,
    group_id VARCHAR(36) NOT NULL,
    assigned_at TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX idx_user_groups_user ON user_groups (user_id);
CREATE INDEX idx_user_groups_group ON user_groups (group_id);
