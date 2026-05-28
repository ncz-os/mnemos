#!/bin/bash
# run on PYTHIA
set -e
echo "=== Oracle: drop+add embedding VECTOR(1024) ==="
docker exec -i pythia-oracle sqlplus -s mnemos/mnemos_dev@//localhost:1521/ORCLPDB1 <<'SQL'
ALTER TABLE memories DROP COLUMN embedding;
ALTER TABLE memories ADD embedding VECTOR(1024, FLOAT32);
COMMIT;
SELECT column_name, data_type, data_length FROM user_tab_columns WHERE table_name='MEMORIES' AND column_name='EMBEDDING';
SELECT COUNT(*) total FROM memories;
EXIT
SQL
