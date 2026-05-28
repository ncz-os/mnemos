#!/bin/bash
set -e
docker exec -i pythia-oracle sqlplus -s mnemos/mnemos_dev@//localhost:1521/ORCLPDB1 <<'SQL'
ALTER TABLE memories ADD embedding_nomic VECTOR(768, FLOAT32);
COMMIT;
SELECT column_name, data_type, data_length
FROM user_tab_columns
WHERE table_name='MEMORIES' AND column_name IN ('EMBEDDING','EMBEDDING_NOMIC')
ORDER BY column_id;
EXIT
SQL
