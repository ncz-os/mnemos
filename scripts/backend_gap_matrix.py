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
    """Classify a method body: RAISE (raises NotImplementedError), IMPL (does
    real async work — any ``await``, i.e. a DB call or delegate), or STUB
    (returns a constant / ``pass`` / ``_ = ...`` with no await). The await
    heuristic is reliable: a real persistence method always awaits a cursor or
    connection; a stub just returns ``[]`` / ``None`` or raises."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and node.exc is not None:
            e = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(e, ast.Name) and "NotImplemented" in e.id:
                return "RAISE"
    for node in ast.walk(fn):
        if isinstance(node, ast.Await):
            return "IMPL"
    return "STUB"


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
