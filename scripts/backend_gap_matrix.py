"""Cross-backend feature-coverage matrix vs the persistence ABC.

For each ABC repository (base.py) + its abstract methods, classify how each
backend (oracle/postgres/db2/mysql/sqlite) implements it:
  IMPL  = real body (SQL / multi-statement logic)
  STUB  = trivial body (pass / return [] / None / 0 / False / {} / _ = ...)
  RAISE = raises NotImplementedError
  --    = not defined on the backend's matching concrete class (inherits ABC
          or a parent backend; for db2 that means it inherits OracleBackend)
"""
import ast, os

BASE = "mnemos/persistence/base.py"
BACKENDS = {
    "oracle": "mnemos/persistence/oracle.py",
    "postgres": "mnemos/persistence/postgres.py",
    "db2": "mnemos/persistence/db2.py",
    "mysql": "mnemos/persistence/mysql.py",
    "sqlite": "mnemos/persistence/sqlite.py",
}
PREFIX = {"oracle": "Oracle", "postgres": "Postgres", "db2": "Db2", "mysql": "Mysql", "sqlite": "Sqlite"}

# ABC repo classes + their abstract async methods
btree = ast.parse(open(BASE).read())
abc_repos = {}  # repo class -> [method names]
for node in btree.body:
    if isinstance(node, ast.ClassDef) and node.name.endswith("Repository"):
        methods = [b.name for b in node.body
                   if isinstance(b, (ast.AsyncFunctionDef, ast.FunctionDef)) and not b.name.startswith("_")]
        if methods:
            abc_repos[node.name] = methods


def classify(fn):
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]  # drop docstring
    if not body:
        return "STUB"
    # raises NotImplementedError anywhere at top level
    for s in body:
        if isinstance(s, ast.Raise) and s.exc is not None:
            name = ""
            e = s.exc
            if isinstance(e, ast.Call):
                e = e.func
            if isinstance(e, ast.Name):
                name = e.id
            if "NotImplemented" in name:
                return "RAISE"
    # trivial stub: single return of a literal/empty, or just `_ = ...`, or pass
    if len(body) == 1:
        s = body[0]
        if isinstance(s, ast.Pass):
            return "STUB"
        if isinstance(s, ast.Return):
            v = s.value
            if v is None or (isinstance(v, ast.Constant) and v.value in (None, 0, False, "")):
                return "STUB"
            if isinstance(v, (ast.List, ast.Dict, ast.Tuple)) and not getattr(v, "elts", getattr(v, "keys", [1])):
                return "STUB"
            if isinstance(v, ast.Tuple) and all(isinstance(x, (ast.List, ast.Dict, ast.Constant)) for x in v.elts):
                return "STUB"
        if isinstance(s, ast.Assign):  # `_ = (tx, ...)`
            return "STUB"
    if len(body) == 2 and isinstance(body[0], ast.Assign) and isinstance(body[1], ast.Return):
        v = body[1].value
        if isinstance(v, (ast.List, ast.Dict)) or v is None:
            return "STUB"
    return "IMPL"


def backend_methods(path, prefix):
    src = open(path).read()
    tree = ast.parse(src)
    # collect concrete repo classes by prefix, map their methods
    out = {}  # repo-suffix -> {method: status}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.startswith(prefix) and node.name.endswith("Repository"):
            suffix = node.name[len(prefix):]  # e.g. MemoryRepository
            m = {}
            for b in node.body:
                if isinstance(b, (ast.AsyncFunctionDef, ast.FunctionDef)) and not b.name.startswith("_"):
                    m[b.name] = classify(b)
            out[suffix] = m
    return out


bm = {bk: backend_methods(p, PREFIX[bk]) for bk, p in BACKENDS.items()}

print("# Cross-backend persistence coverage matrix\n")
totals = {bk: {"IMPL": 0, "STUB": 0, "RAISE": 0, "--": 0} for bk in BACKENDS}
for repo, methods in abc_repos.items():
    suffix = repo
    print(f"## {repo} ({len(methods)} methods)")
    print("| method | " + " | ".join(BACKENDS) + " |")
    print("|---|" + "|".join("---" for _ in BACKENDS) + "|")
    for meth in methods:
        row = [meth]
        for bk in BACKENDS:
            st = bm[bk].get(suffix, {}).get(meth, "--")
            totals[bk][st] += 1
            row.append(st)
        print("| " + " | ".join(row) + " |")
    print()

print("## TOTALS (across all ABC repo methods)")
print("| backend | IMPL | STUB | RAISE | absent(--) |")
print("|---|---|---|---|---|")
for bk in BACKENDS:
    t = totals[bk]
    print(f"| {bk} | {t['IMPL']} | {t['STUB']} | {t['RAISE']} | {t['--']} |")
