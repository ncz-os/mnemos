-- 0008_user_groups.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE user_groups (
    user_id VARCHAR2(36) NOT NULL,
    group_id VARCHAR2(36) NOT NULL,
    assigned_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX idx_user_groups_user ON user_groups (user_id);
CREATE INDEX idx_user_groups_group ON user_groups (group_id);
