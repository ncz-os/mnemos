"""Ground-truth DB2-native gap finder (current head, replaces stale audit).

For every async method defined on an Oracle* class in oracle.py that is NOT
overridden by the corresponding Db2* class in db2.py, scan the method body for
Oracle-isms that the native cursor guard rejects or DB2 mis-executes.
"""
import ast, re

ORACLE = "mnemos/persistence/oracle.py"
DB2 = "mnemos/persistence/db2.py"

ISMS = [
    (re.compile(r"\bSYSTIMESTAMP\b", re.I), "SYSTIMESTAMP"),
    (re.compile(r"\bSYSDATE\b", re.I), "SYSDATE"),
    (re.compile(r"\bFROM\s+DUAL\b", re.I), "FROM DUAL"),
    (re.compile(r"\bROWNUM\b", re.I), "ROWNUM"),
    (re.compile(r"\bNUMTODSINTERVAL\b", re.I), "NUMTODSINTERVAL"),
    (re.compile(r"\bTO_TIMESTAMP_TZ\b", re.I), "TO_TIMESTAMP_TZ"),
    (re.compile(r"\bTO_VECTOR\b", re.I), "TO_VECTOR"),
    (re.compile(r":[a-zA-Z_]\w*"), "named :bind"),
]

def classes(path):
    tree = ast.parse(open(path).read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = {}
            for b in node.body:
                if isinstance(b, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    methods[b.name] = ast.get_source_segment(open(path).read(), b) or ""
            out[node.name] = methods
    return out

# cache source reads
osrc = open(ORACLE).read()
dsrc = open(DB2).read()
otree = ast.parse(osrc)
dtree = ast.parse(dsrc)

def class_methods(tree, src):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            m = {}
            for b in node.body:
                if isinstance(b, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    seg = ast.get_source_segment(src, b) or ""
                    m[b.name] = seg
            out[node.name] = m
    return out

ocls = class_methods(otree, osrc)
dcls = class_methods(dtree, dsrc)

# Map Oracle class -> Db2 class by name convention (Oracle<X> -> Db2<X>)
def db2_name(oname):
    return oname.replace("Oracle", "Db2", 1)

# Build set of all method names overridden anywhere in any Db2 class that
# subclasses the matching Oracle class (handle mixins by unioning all Db2 classes).
all_db2_overrides = {}
for dn, dm in dcls.items():
    all_db2_overrides.setdefault(dn, set()).update(dm.keys())

def mask(s):
    # crude literal/comment mask to cut :bind false positives in strings
    s = re.sub(r"#.*", "", s)
    s = re.sub(r"'(?:''|[^'])*'", "''", s)
    s = re.sub(r'"(?:[^"]|"")*"', '""', s)
    # python f/normal string triple — drop triple-quoted blocks roughly
    return s

gaps = []
for ocn, omethods in ocls.items():
    if not ocn.startswith("Oracle"):
        continue
    dcn = db2_name(ocn)
    overridden = all_db2_overrides.get(dcn, set())
    for mname, msrc in omethods.items():
        if mname.startswith("_") and mname != "__init__":
            # still check private helpers (e.g. _registry_rows) — they may leak
            pass
        if mname in overridden:
            continue
        masked = msrc  # scan raw: Oracle-isms live INSIDE SQL string literals
        hits = sorted({label for rx, label in ISMS if rx.search(masked)})
        if hits:
            gaps.append((ocn, mname, hits))

print("=== TRUE DB2-NATIVE GAPS AT CURRENT HEAD ===")
print(f"{len(gaps)} inherited-not-overridden methods contain Oracle-isms:\n")
for ocn, mname, hits in sorted(gaps):
    print(f"  {ocn}.{mname:40s} {', '.join(hits)}")
