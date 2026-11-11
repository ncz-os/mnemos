"""Oracle rejects a bind variable named after a reserved word with ORA-01745.

7.1.0 shipped ``record_sync_lag`` with ``:rows`` and every peer pull on the Oracle primary failed
after the sync (HTTP 500 on /v1/federation/peers/<id>/sync, peers never refreshed). Static guard: no
SQL string in the Oracle backend binds a reserved word.
"""

import ast
import pathlib
import re

ORACLE_RESERVED = {
    "ACCESS", "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUDIT", "BETWEEN", "BY", "CHAR", "CHECK",
    "CLUSTER", "COLUMN", "COMMENT", "COMPRESS", "CONNECT", "CREATE", "CURRENT", "DATE", "DECIMAL", "DEFAULT",
    "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "EXCLUSIVE", "EXISTS", "FILE", "FLOAT", "FOR", "FROM",
    "GRANT", "GROUP", "HAVING", "IDENTIFIED", "IMMEDIATE", "IN", "INCREMENT", "INDEX", "INITIAL", "INSERT",
    "INTEGER", "INTERSECT", "INTO", "IS", "LEVEL", "LIKE", "LOCK", "LONG", "MAXEXTENTS", "MINUS", "MLSLABEL",
    "MODE", "MODIFY", "NOAUDIT", "NOCOMPRESS", "NOT", "NOWAIT", "NULL", "NUMBER", "OF", "OFFLINE", "ON",
    "ONLINE", "OPTION", "OR", "ORDER", "PCTFREE", "PRIOR", "PUBLIC", "RAW", "RENAME", "RESOURCE", "REVOKE",
    "ROW", "ROWID", "ROWNUM", "ROWS", "SELECT", "SESSION", "SET", "SHARE", "SIZE", "SMALLINT", "START",
    "SUCCESSFUL", "SYNONYM", "SYSDATE", "TABLE", "THEN", "TO", "TRIGGER", "UID", "UNION", "UNIQUE", "UPDATE",
    "USER", "VALIDATE", "VALUES", "VARCHAR", "VARCHAR2", "VIEW", "WHENEVER", "WHERE", "WITH",
}
BIND = re.compile(r"(?<![\w:]):([A-Za-z_][A-Za-z0-9_]*)")
SQL = re.compile(r"\b(SELECT|UPDATE|INSERT|DELETE|MERGE)\b", re.I)


def test_oracle_backend_has_no_reserved_word_bind_names():
    path = pathlib.Path(__file__).resolve().parents[1] / "mnemos" / "persistence" / "oracle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL.search(node.value):
            for name in BIND.findall(node.value):
                if name.upper() in ORACLE_RESERVED:
                    offenders.append((node.lineno, name))
    assert not offenders, f"reserved-word bind names (ORA-01745): {offenders}"
