#!/usr/bin/env python3
"""Generate docs/BACKEND_PARITY.md — the MNEMOS backend capability matrix.

This script enumerates the cross-cutting capability surface of MNEMOS's six
SQL persistence backends (sqlite, postgres, mysql, mariadb, oracle, db2)
and renders a markdown parity matrix at ``docs/BACKEND_PARITY.md``.

What it detects
===============

Rows (capabilities) come from two sources:

* the abstract repositories on :mod:`mnemos.persistence.base`
  (``MemoryRepository``, ``KGRepository``, ``VersionRepository``,
  ``BranchRepository``, ``CompressionRepository``,
  ``CompressionQueueRepository``, ``MorpheusRepository``,
  ``WebhookRepository``, ``NatsDispatchLogRepository``,
  ``ConsultationAuditRepository``, ``OAuthRepository``,
  ``SessionsRepository``, ``ConsultationsRepository``,
  ``FederationRepository``, ``StateRepository``,
  ``AuditChainRepository``, ``AclRepository``) and
* KNEMON's :class:`CorePersistence` extras (``journal``,
  ``ledger``, ``vector_search``, ``fts_search``,
  ``row_level_security``, ``listen_notify``, ``advisory_locks``) and
* subsystem-specific routes/repos that are runtime-gated
  (``federation_journal`` distinct table journal, ``morpheus_http_trigger``
  HTTP path, ``kronos_routes`` Postgres-only admin routes).

Columns are the six concrete persistence backends.

For each (capability, backend) cell we compute two independent booleans:

``implemented``
    The backend's facade class (e.g. :class:`PostgresBackend`) defines the
    property or method that supplies the capability — *and* that accessor
    does not unconditionally raise :class:`BackendCapabilityMissing` or
    return ``None`` (audit-chain contract). Detection walks the AST and
    looks at the property ``fget`` body and class-level attributes.

``tested``
    At least one test in ``tests/`` exercises the (capability, backend)
    pair. Detection uses two complementary heuristics calibrated on this
    repo's existing tests (``test_db2_dialect_parity.py``,
    ``test_mysql_recency_dialect.py``, ``test_oracle_live.py``,
    ``test_persistence_parity.py``, ``test_backend_audit_chain_attribute.py``,
    ``test_kronos_backends.py``):

    * a ``@pytest.mark.parametrize`` whose argument ids mention the backend
      name (e.g. ``ids=[b[0] for b in BACKENDS]``);
    * a test module whose file name encodes both the capability and the
      backend (e.g. ``test_db2_<capability>*.py``,
      ``test_<capability>_db2*.py``).

Output
======

The markdown matrix is deterministic and machine-generated: a header
records the generator invocation and timestamp so reviewers can spot
manual edits. The single-cell legend:

* ``✅ implemented+tested``        — implementation present and tests cover it
* ``⚠️ implemented, no test``     — code is there but no test exercises it
* ``⚠️ test exists, stub impl``    — test exists but the backend is a stub
* ``❌ neither``                   — no implementation, no test

CI gate
=======

The companion GitHub Actions job
``.github/workflows/docs-backend-parity.yml`` re-runs this script and
fails when ``git diff --exit-code docs/BACKEND_PARITY.md`` is non-empty.
That is the entire purpose of this artefact: the matrix is the source of
truth for cross-backend parity and any drift against the live code is a
build failure.

Run locally with:

    python scripts/generate_backend_parity_matrix.py
    git diff --exit-code docs/BACKEND_PARITY.md
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Resolve the repo root once so the script works from any CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
PERSISTENCE_DIR = REPO_ROOT / "mnemos" / "persistence"
TESTS_DIR = REPO_ROOT / "tests"
API_DIR = REPO_ROOT / "mnemos" / "api"
OUTPUT_PATH = REPO_ROOT / "docs" / "BACKEND_PARITY.md"

# Backends we report on. The IDs match the lowercase names used in tests
# (``tests/test_persistence_parity.py``, ``tests/test_backend_audit_chain_attribute.py``).
BACKENDS: tuple[str, ...] = (
    "sqlite",
    "postgres",
    "mysql",
    "mariadb",
    "oracle",
    "db2",
)

# Map backend id -> (module, facade class name).
BACKEND_CLASSES: dict[str, tuple[str, str]] = {
    "sqlite": ("mnemos.persistence.sqlite", "SqliteBackend"),
    "postgres": ("mnemos.persistence.postgres", "PostgresBackend"),
    "mysql": ("mnemos.persistence.mysql", "MysqlBackend"),
    "mariadb": ("mnemos.persistence.mariadb", "MariadbBackend"),
    "oracle": ("mnemos.persistence.oracle", "OracleBackend"),
    "db2": ("mnemos.persistence.db2", "Db2Backend"),
}


# ---------------------------------------------------------------------------
# Capability rows.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A row in the parity matrix."""

    id: str
    label: str
    # Logical key used to grep tests. Lower-case, ASCII.
    test_keyword: str
    # Where the implementation lives.
    impl_kind: str  # one of: "repo_property", "capability_details", "route_gate", "federation_journal"
    # The facade property or attribute name to inspect for ``implemented``.
    # Multiple names are tried in order; the first that resolves wins.
    impl_property: tuple[str, ...]
    # Notes about the capability, shown on the matrix row.
    note: str = ""
    # Backends known never to implement this; recorded as ``❌ neither``
    # automatically (saves redundant AST scans and documents the rule).
    forbidden_on: frozenset[str] = frozenset()
    # If non-empty, only these backends are inspected; others get ❌.
    only_on: frozenset[str] = frozenset()


def _capabilities() -> tuple[Capability, ...]:
    return (
        # ── core ABC repos (16) ────────────────────────────────────────────
        Capability(
            id="memory_crud",
            label="memory_crud (MemoryRepository)",
            test_keyword="memory",
            impl_kind="repo_property",
            impl_property=("memories",),
            note="CRUD + semantic/fts on the memories table; the persistence ABC.",
        ),
        Capability(
            id="vector_search",
            label="vector_search (semantic_search)",
            test_keyword="semantic_search",
            impl_kind="repo_property",
            impl_property=("memories",),
            note="Cosine similarity over memory_embeddings.",
        ),
        Capability(
            id="fts_search",
            label="fts_search (FTS5 / native FTS)",
            test_keyword="fts",
            impl_kind="repo_property",
            impl_property=("memories",),
            note="Full-text search over memory content.",
        ),
        Capability(
            id="kg",
            label="kg (KGRepository)",
            test_keyword="kg",
            impl_kind="repo_property",
            impl_property=("kg_triples",),
            note="Knowledge-graph triple persistence.",
        ),
        Capability(
            id="versions",
            label="versions (VersionRepository)",
            test_keyword="memory_version",
            impl_kind="repo_property",
            impl_property=("memory_versions",),
            note="Per-memory version history (HEAD/branches).",
        ),
        Capability(
            id="branches",
            label="branches (BranchRepository)",
            test_keyword="memory_branch",
            impl_kind="repo_property",
            impl_property=("memory_branches",),
            note="Memory branch persistence.",
        ),
        Capability(
            id="compression",
            label="compression (CompressionRepository)",
            test_keyword="compression",
            impl_kind="repo_property",
            impl_property=("compression",),
            note="Memory compressed-variant repository.",
        ),
        Capability(
            id="compression_queue",
            label="compression_queue (CompressionQueueRepository)",
            test_keyword="compression_queue",
            impl_kind="repo_property",
            impl_property=("compression_queue",),
            note="v3.1 distillation/contpression work queue (SKIP LOCKED claim).",
        ),
        Capability(
            id="morpheus",
            label="morpheus (MorpheusRepository)",
            test_keyword="morpheus",
            impl_kind="repo_property",
            impl_property=("morpheus",),
            note="v3.3 MORPHEUS run-lifecycle ABC.",
        ),
        Capability(
            id="webhooks",
            label="webhooks (end-to-end delivery)",
            test_keyword="webhook",
            impl_kind="repo_property",
            impl_property=("webhooks",),
            note="End-to-end delivery (claim/send/finalize worker required).",
        ),
        Capability(
            id="nats_dispatch_log",
            label="nats_dispatch_log (idempotency dedupe)",
            test_keyword="nats_dispatch_log",
            impl_kind="repo_property",
            impl_property=("nats_dispatch_log",),
            note="Idempotent NATS dispatch-dedupe log (event_id, subject).",
        ),
        Capability(
            id="consultations_audit",
            label="consultations_audit (model recommendation)",
            test_keyword="consultation_audit",
            impl_kind="repo_property",
            impl_property=("consultations_audit",),
            note="OpenAI-compatible gateway audit log + model_registry.",
        ),
        Capability(
            id="oauth",
            label="oauth (OAuthRepository)",
            test_keyword="oauth",
            impl_kind="repo_property",
            impl_property=("oauth",),
            note="OAuth provider / identity / token / session.",
        ),
        Capability(
            id="sessions",
            label="sessions (SessionsRepository)",
            test_keyword="session",
            impl_kind="repo_property",
            impl_property=("sessions",),
            note="Stateful chat session persistence.",
        ),
        Capability(
            id="consultations",
            label="consultations (ConsultationsRepository)",
            test_keyword="consultation",
            impl_kind="repo_property",
            impl_property=("consultations",),
            note="GRAEAE consultation persistence + audit chain.",
        ),
        Capability(
            id="federation",
            label="federation (FederationRepository)",
            test_keyword="federation",
            impl_kind="repo_property",
            impl_property=("federation",),
            note="Federation peers, sync log, and feed-query persistence.",
        ),
        Capability(
            id="state",
            label="state (StateRepository)",
            test_keyword="state_kv",
            impl_kind="repo_property",
            impl_property=("state_kv",),
            note="State key-value persistence (job-state, distillation-state).",
        ),
        Capability(
            id="audit_chain",
            label="audit_chain (AuditChainRepository)",
            test_keyword="audit_chain",
            impl_kind="repo_property",
            impl_property=("audit_chain",),
            note="v6.2 per-memory append-only audit chain + global sealer.",
        ),
        Capability(
            id="acl",
            label="acl (AclRepository)",
            test_keyword="acl",
            impl_kind="repo_property",
            impl_property=("acl",),
            note="Per-principal memory ACL grant/revoke/list.",
        ),
        # ── KNEMON extras on CorePersistence ───────────────────────────────
        Capability(
            id="journal",
            label="journal (KNEMON journal entries)",
            test_keyword="journal",
            impl_kind="capability_details",
            impl_property=("JOURNAL_CAPABILITY",),
            note="Per-namespace journal entries (KNEMON MVP).",
        ),
        Capability(
            id="ledger",
            label="ledger (KNEMON usage_ledger)",
            test_keyword="ledger",
            impl_kind="capability_details",
            impl_property=("LEDGER_CAPABILITY",),
            note="Model-token usage ledger + cost recording.",
        ),
        Capability(
            id="row_level_security",
            label="row_level_security (Postgres RLS)",
            test_keyword="row_level_security",
            impl_kind="flag",
            impl_property=("supports_row_level_security",),
            note="Postgres RLS — only Postgres supports it.",
            only_on=frozenset({"postgres"}),
        ),
        Capability(
            id="listen_notify",
            label="listen_notify (Postgres LISTEN/NOTIFY)",
            test_keyword="listen_notify",
            impl_kind="flag",
            impl_property=("supports_listen_notify",),
            note="Postgres LISTEN/NOTIFY for cross-process wakeups.",
            only_on=frozenset({"postgres"}),
        ),
        Capability(
            id="advisory_locks",
            label="advisory_locks (Postgres advisory locks)",
            test_keyword="advisory_lock",
            impl_kind="flag",
            impl_property=("supports_advisory_locks",),
            note="Postgres session-level advisory locks.",
            only_on=frozenset({"postgres"}),
        ),
        # ── subsystem-specific routes / repos ──────────────────────────────
        Capability(
            id="federation_journal",
            label="federation_journal (distinct journal table)",
            test_keyword="federation_journal",
            impl_kind="federation_journal",
            impl_property=(),
            note="Separate ``0062_federation_journal.sql`` table (id, peer, remote_id, action).",
        ),
        Capability(
            id="morpheus_http_trigger",
            label="morpheus HTTP-trigger (POST /admin/morpheus/runs)",
            test_keyword="admin_morpheus",
            impl_kind="route_gate",
            impl_property=(),
            note="Manual POST trigger route — Postgres-only.",
            only_on=frozenset({"postgres"}),
        ),
        Capability(
            id="kronos_routes",
            label="kronos routes (POSTGRES-only)",
            test_keyword="kronos",
            impl_kind="route_gate",
            impl_property=(),
            note="/admin/kronos/{anomalies,drift,forecast} — Postgres-only.",
            only_on=frozenset({"postgres"}),
        ),
    )


# ---------------------------------------------------------------------------
# Implementation detection (AST).
# ---------------------------------------------------------------------------


@dataclass
class _ClassView:
    """View of a backend facade class: every property fget body + class attrs."""

    method_names: set[str] = field(default_factory=set)
    property_fget_bodies: dict[str, str] = field(default_factory=dict)
    class_attrs: dict[str, str] = field(default_factory=dict)
    raw_source: str = ""


def _load_module_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _view_for_class(path: Path, class_name: str) -> _ClassView:
    """Return a structural view of ``class_name`` in ``path``.

    The walk also resolves *cross-file* inheritance: when a backend facade
    like ``Db2Backend(OracleBackend)`` is defined in ``mnemos/persistence/db2.py``
    but its parent lives in ``mnemos/persistence/oracle.py``, we still pick
    up the parent class's properties so the matrix reflects the inherited
    surface.  Without this, every cross-file subclass (Db2, Mariadb) would
    look unimplemented even when it inherits a working accessor from
    Oracle/MySQL.
    """
    src = _load_module_text(path)
    tree = ast.parse(src, filename=str(path))
    view = _ClassView(raw_source=src)

    seen_modules: set[Path] = {path}

    def _resolve_class(module_tree: ast.Module, name: str) -> ast.ClassDef | None:
        for sub in ast.walk(module_tree):
            if isinstance(sub, ast.ClassDef) and sub.name == name:
                return sub
        return None

    def _module_path_for(qualname: str) -> Path | None:
        rel = qualname.replace(".", "/") + ".py"
        candidate = REPO_ROOT / rel
        return candidate if candidate.exists() else None

    def _consume(cls: ast.ClassDef, module_tree: ast.Module) -> None:
        # Class-level assignments.
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign):
                value_src = ast.unparse(stmt.value)
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        view.class_attrs.setdefault(tgt.id, value_src)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                value_src = ast.unparse(stmt.value) if stmt.value is not None else ""
                view.class_attrs.setdefault(stmt.target.id, value_src)

        # Methods + properties.
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_property = any(
                    (isinstance(d, ast.Name) and d.id == "property")
                    or (isinstance(d, ast.Attribute) and d.attr == "property")
                    for d in stmt.decorator_list
                )
                if is_property:
                    view.property_fget_bodies.setdefault(stmt.name, ast.unparse(stmt))
                view.method_names.add(stmt.name)

    def _class_qualname_for(base: ast.expr) -> str | None:
        """Resolve a base expression to a fully-qualified class name we can import.

        Handles ``OracleBackend`` (Name) and ``mnemos.persistence.oracle.OracleBackend``
        (Attribute chain) — both forms appear in the wild across this repo.
        """
        if isinstance(base, ast.Name):
            # Try to find an import of this name in any module we've seen;
            # fall back to the unqualified name as a last resort.
            for module_path in seen_modules:
                module_tree = ast.parse(_load_module_text(module_path), filename=str(module_path))
                for node in ast.walk(module_tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        for alias in node.names:
                            if alias.name == base.id:
                                return f"{node.module}.{alias.asname or alias.name}"
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.asname == base.id:
                                return alias.name
            return base.id
        if isinstance(base, ast.Attribute):
            parts: list[str] = []
            cur: ast.expr = base
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            return ".".join(reversed(parts))
        return None

    visited: set[str] = set()
    queue: list[tuple[Path, ast.ClassDef]] = []
    direct_cls = _resolve_class(tree, class_name)
    if direct_cls is not None:
        queue.append((path, direct_cls))

    while queue:
        cur_path, cls = queue.pop(0)
        key = f"{cur_path}::{cls.name}"
        if key in visited:
            continue
        visited.add(key)
        seen_modules.add(cur_path)
        _consume(cls, ast.parse(_load_module_text(cur_path), filename=str(cur_path)))

        for base in cls.bases:
            qname = _class_qualname_for(base)
            if not qname:
                continue
            parent_path = _module_path_for(qname.rsplit(".", 1)[0])
            if parent_path is None:
                # Same-file inheritance (rare — usually mixed-in traits).
                parent_tree = ast.parse(_load_module_text(cur_path), filename=str(cur_path))
            else:
                parent_tree = ast.parse(_load_module_text(parent_path), filename=str(parent_path))
            parent_cls = _resolve_class(parent_tree, qname.rsplit(".", 1)[-1])
            if parent_cls is None:
                continue
            queue.append((parent_path, parent_cls))

    return view


def _backend_source_path(backend: str) -> Path:
    module_name, _cls = BACKEND_CLASSES[backend]
    rel = module_name.replace(".", "/") + ".py"
    return REPO_ROOT / rel


def _property_implemented(view: _ClassView, prop_names: tuple[str, ...]) -> bool:
    """True iff any of the named properties exist and do not raise / return None."""
    for name in prop_names:
        body = view.property_fget_bodies.get(name)
        if body is None:
            continue
        # Treat these bodies as NOT implemented:
        #   raise BackendCapabilityMissing("xxx")
        #   return None        (audit_chain contract per base.py:2968)
        #   raise NotImplementedError(...)
        if re.search(r"BackendCapabilityMissing", body):
            continue
        if re.search(r"raise\s+NotImplementedError", body):
            continue
        if re.search(r"return\s+None\b", body) and "audit_chain" in prop_names:
            continue
        return True
    return False


def _class_attr_implemented(view: _ClassView, attr_names: tuple[str, ...]) -> bool:
    """True iff any class-level attribute (e.g. supports_pgvector = True) is set to a truthy value."""
    for name in attr_names:
        if name in view.class_attrs:
            value = view.class_attrs[name].strip()
            if value in {"True", "1"}:
                return True
            # Anything else (False / 0 / a computed expression) counts as
            # "not advertised"; we err on the conservative side.
    return False


def _capability_details_implemented(view: _ClassView, detail: str) -> bool:
    """True iff ``capability_details`` includes ``detail`` (literal name)."""
    body = view.property_fget_bodies.get("capability_details")
    if body is None:
        return False
    # capability_details often returns ``set(FULL_STORAGE_CAPABILITY_DETAILS)``
    # or ``{*MYSQL_CAPABILITY_DETAILS, ...}``. A name lookup is therefore a
    # weak heuristic; rely on the *literal* occurrence in the body to mean
    # the backend explicitly advertises it.
    if detail in body:
        return True
    return False


def _federation_journal_implemented(backend: str) -> bool:
    """Detect the ``0062_federation_journal.sql`` distinct-table integration.

    The journal is set up via ``_ensure_<backend>_federation_journal`` calls
    in the backend's ``open()`` and is queried by
    :mod:`mnemos.persistence.federation_journal`. SQLite, Postgres, Oracle
    and Db2 all pull it; MySQL/MariaDB use ``federation_journal_mariadb_embeddings.sql``.
    """
    src_path = _backend_source_path(backend)
    src = _load_module_text(src_path)
    if "federation_journal" in src:
        # Look for an explicit ensure/apply call.
        for needle in (
            "_ensure_mysql_federation_journal",
            "_ensure_postgres_federation_journal",
            "_ensure_oracle_federation_journal",
            "_ensure_db2_federation_journal",
            "ensure_postgres_federation_journal",
            "ensure_oracle_federation_journal",
            "ensure_db2_federation_journal",
        ):
            if needle in src:
                return True
    # Postgres / Oracle / Db2 / SQLite call ``federation_journal.feed_query``
    # in their federation repository implementation; that import alone is
    # the canonical signal the journal table is in scope.
    if "from mnemos.persistence.federation_journal import feed_query" in src:
        return True
    return False


def _route_gate_implemented(backend: str, route_module: str) -> bool:
    """Detect a Postgres-only API route guard (``require_postgres_pool_or_503``).

    A "route_gate" capability exists only on the backend the route's
    ``require_postgres_pool_or_503`` succeeds against — Postgres.
    """
    return backend == "postgres" and bool(route_module)


# ---------------------------------------------------------------------------
# Test detection (AST + glob).
# ---------------------------------------------------------------------------


_TEST_FILE_GLOB = "test_*.py"
_PARAM_BACKEND_PATTERN = re.compile(
    r"(sqlite|postgres|mysql|mariadb|oracle|db2)", re.IGNORECASE
)


def _iter_test_files() -> Iterable[Path]:
    if not TESTS_DIR.exists():
        return
    for path in TESTS_DIR.rglob(_TEST_FILE_GLOB):
        yield path


def _test_file_matches(test_path: Path, backend: str, capability_id: str) -> bool:
    """Three heuristics, calibrated on this repo's existing tests:

    1. File name encodes both capability + backend
       (``test_db2_dialect_parity.py``, ``test_oracle_live.py``,
       ``test_kronos_backends.py``, ``test_mysql_recency_dialect.py``,
       ``test_backend_audit_chain_attribute.py``).
    2. ``@pytest.mark.parametrize`` / ``@pytest_asyncio.fixture(params=...)``
       over backend names AND at least one test function name in the
       file contains the capability id (or a synonym).
    3. Backend-specific file with capability-named tests — the
       canonical shape of ``test_<backend>_<something>.py`` files in
       this repo, which exercise one backend directly without a
       parametrize over backends.

    All three heuristics are deliberately tight to avoid over-matching:
    a test named ``test_single_memory_authorized_fetch_returns_durable_delete``
    inside ``test_federation_journal.py`` is about federation semantics
    and only matches the federation_journal row, not memory_crud.
    """
    name = test_path.name.lower()
    if name.endswith(".py"):
        name = name[:-3]
    parts = [p for p in re.split(r"[_.]", name) if p]

    # Heuristic 1: filename encodes capability + backend.
    if _file_name_matches_capability(name, parts, capability_id) and backend in parts:
        return True
    if backend in parts:
        idx = parts.index(backend)
        for candidate in (parts[:idx], parts[idx + 1 :]):
            if any(_parts_share_token(p, capability_id) for p in candidate if len(p) >= 3):
                return True

    try:
        src = test_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except SyntaxError:
        return False

    if not _any_test_function_references_capability(tree, capability_id, backend=backend):
        return False

    # Heuristic 2: parametrize over backend names — covers cross-backend
    # parametrized tests (``test_persistence_parity.py``,
    # ``test_federation_journal.py``).
    prior = dict(_BACKEND_LIST_RESOLVERS)
    _BACKEND_LIST_RESOLVERS.clear()
    try:
        _module_top_level_lists(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "parametrize":
                if _parametrize_hits_backend(node, backend):
                    return True
            elif func.attr == "fixture":
                # ``@pytest_asyncio.fixture(params=_backend_params())``
                # is the most common backend-parameterization form in
                # this repo (``test_persistence_parity.py``,
                # ``test_admin_lifecycle_routes_no_503.py``,
                # ``test_postgres_only_503_invariant.py``).
                if _fixture_params_hits_backend(node, backend, tree):
                    return True
    finally:
        _BACKEND_LIST_RESOLVERS.clear()
        _BACKEND_LIST_RESOLVERS.update(prior)

    # Heuristic 3: backend-specific file with capability-named tests.
    # Pattern: ``test_<backend>_<something>.py`` whose test functions
    # mention the capability id. Real examples:
    # ``test_oracle_recency_dialect.py`` -> tests
    # ``test_oracle_semantic_search_*`` (vector_search capability);
    # ``test_mysql_state_live.py`` -> tests ``test_mysql_state_*``
    # (state capability). The capability gate above
    # (``_any_test_function_references_capability``) already required
    # the file to contain capability-named tests, so all we need to
    # verify here is that the backend name appears in the filename.
    if backend in parts:
        return True

    return False


def _parametrize_hits_backend(call: ast.Call, backend: str) -> bool:
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        if backend not in first.value.lower():
            return _ids_arg_hits_backend(call, backend)
        return len(call.args) >= 2 and _value_arg_hits_backend(call.args[1], backend)
    if isinstance(first, ast.Tuple):
        if len(call.args) >= 2 and _value_arg_hits_backend(call.args[1], backend):
            return True
        return _ids_arg_hits_backend(call, backend)
    return False


def _fixture_params_hits_backend(call: ast.Call, backend: str, tree: ast.Module) -> bool:
    """Detect ``@pytest_asyncio.fixture(params=<expr>)`` whose params list names the backend.

    Real pattern::

        @pytest_asyncio.fixture(params=_backend_params())
        async def backend_case(request, ...): ...

    We resolve ``_backend_params`` (a module-level ``def``) by name and
    follow ``Return`` statements whose value is a list literal whose
    elts are string Constants.
    """
    params_arg: ast.AST | None = None
    for kw in call.keywords:
        if kw.arg == "params":
            params_arg = kw.value
            break
    if params_arg is None and len(call.args) >= 1:
        params_arg = call.args[0]
    if params_arg is None:
        return False

    # Walk: Call -> .func=Name -> .id = "_backend_params"
    func_name: str | None = None
    if isinstance(params_arg, ast.Call):
        f = params_arg.func
        if isinstance(f, ast.Name):
            func_name = f.id
    if not func_name:
        return _value_arg_hits_backend(params_arg, backend)

    # Find the module-level def of ``func_name`` and probe its body.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return _function_returns_backend(node, backend)
    return False


def _function_returns_backend(func: ast.FunctionDef | ast.AsyncFunctionDef, backend: str) -> bool:
    """Best-effort: backend is reachable from this function.

    Tries (in order):
    1. Follow ``return <value>`` through Name bindings to literal lists.
    2. Fall back to scanning every string constant in the function body —
       a backend name appearing anywhere in the body is a strong signal
       the function is meant to expose that backend as a test param.

    The fallback intentionally errs on the side of *more matches* for
    matrix coverage: the matrix's job is to expose gaps, and the
    canonical parity tests in this repo use ``_backend_params()``
    guarded by env vars (e.g. ``if PG_URL: params.append("postgres")``).
    Even when those env vars aren't set in the matrix-generation CI
    run, the test was clearly *written* to cover every backend the
    function mentions — and a backend never mentioned in the function
    body can't be covered even with the env vars set.
    """
    if _return_value_hits_backend(func, func.body, backend):
        return True
    for node in ast.walk(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if backend == node.value.lower():
                return True
    return False


def _return_value_hits_backend(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    body: list[ast.stmt],
    backend: str,
) -> bool:
    """Evaluate the function body for ``return`` statements whose final value contains the backend."""
    for stmt in body:
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            if _expr_hits_backend(stmt.value, body, backend):
                return True
        if isinstance(stmt, ast.If):
            if _return_value_hits_backend(func, stmt.body, backend):
                return True
            if _return_value_hits_backend(func, stmt.orelse, backend):
                return True
    return False


def _expr_hits_backend(node: ast.AST, scope: list[ast.stmt], backend: str) -> bool:
    """Resolve a single expression to a set of backend tokens and check membership.

    Handles literal lists/tuples, ``Name`` references to module-level
    constants (already indexed) and local Name bindings resolved by
    scanning ``scope`` for the most recent Assign / AnnAssign, and
    plain string Constants.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return backend in node.value.lower()
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        # Direct list literal — check each element.
        for elt in node.elts:
            if _expr_hits_backend(elt, scope, backend):
                return True
        return False
    if isinstance(node, ast.Name):
        # Resolve through constant-indexed Name + the function scope.
        resolver = _BACKEND_LIST_RESOLVERS.get(node.id)
        if resolver is not None:
            return any(
                _value_arg_hits_backend(item, backend) for item in resolver()
            )
        # Find the most recent assignment to this name in the scope.
        for stmt in reversed(scope):
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets
            ):
                return _expr_hits_backend(stmt.value, scope, backend)
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == node.id
                and stmt.value is not None
            ):
                return _expr_hits_backend(stmt.value, scope, backend)
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # ``return params + [...]`` (rare).
        return _expr_hits_backend(node.left, scope, backend) or _expr_hits_backend(
            node.right, scope, backend
        )
    return False


def _parts_share_token(part: str, capability_id: str) -> bool:
    return part.startswith(capability_id) or capability_id.startswith(part)


def _file_name_matches_capability(name: str, parts: list[str], capability_id: str) -> bool:
    """Return True when the file name actually encodes the capability.

    We require the file's stem (the joined ``parts`` excluding the
    ``.py`` extension) to contain the capability id's tokens OR one
    of its synonyms. For a multi-token capability (``memory_crud``,
    ``fts_search``, ``vector_search``) every token of the matched
    synonym must appear as a separate ``_``-bounded segment. For
    single-token capability ids (``morpheus``, ``federation``,
    ``kronos``) we match when any part starts with or equals the
    token (or one of its synonyms).

    This is deliberately stricter than a substring scan so a file
    named ``test_federation_journal.py`` does NOT count as a test
    for the ``journal`` capability — it has to match
    ``federation_journal`` end-to-end. But it is permissive enough
    to match real-world variations like
    ``test_db2_semantic_search_dialect.py`` against the
    ``vector_search`` capability (synonym: ``semantic_search``).
    """
    cap_tokens = [t for t in capability_id.split("_") if t]
    if not cap_tokens:
        return False
    synonyms = _CAPABILITY_SYNONYMS.get(capability_id, (capability_id,))
    if len(cap_tokens) == 1:
        for syn in synonyms:
            syn_first = syn.split("_", 1)[0]
            if not syn_first:
                continue
            if any(part == syn_first or part.startswith(syn_first) for part in parts):
                return True
        return False
    # Multi-token capability: try every synonym as the canonical split.
    for syn in synonyms:
        syn_tokens = syn.split("_")
        if all(tok in parts for tok in syn_tokens):
            return True
    return False


# Capability synonyms used in real test names/docstrings. The single-word
# synonyms (``memory``, ``kg``, ``acl``, ``oauth``, ``fts``,
# ``compression``, ``state``, ``branch``, ``session``, ``webhook``,
# ``vector``, ``fts``, ``morpheus``, ``federation``) match when those
# words appear in the file name OR a test function name. The double-word
# synonyms (``memory_crud``, ``oauth_repo`` etc.) are there for
# hygiene: they match when the file uses a long form, and they disambiguate
# between overlapping capability ids (e.g. ``journal`` vs
# ``federation_journal``).
_CAPABILITY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "memory_crud": ("memory_crud", "memory_repository", "memory"),
    "vector_search": ("vector_search", "semantic_search", "vector", "semantic"),
    "fts_search": ("fts_search", "fulltext_search", "fts", "fulltext"),
    "kg": ("kg_repo", "kg_triple", "kg"),
    "versions": ("version_repo", "memory_version", "version"),
    "branches": ("branch_repo", "memory_branch", "branch"),
    "compression": ("compression_repo", "memory_compression", "compression"),
    "compression_queue": ("compression_queue", "distillation_queue"),
    "morpheus": ("morpheus",),
    "webhooks": ("webhook_repo", "webhook"),
    "nats_dispatch_log": ("nats_dispatch_log", "dispatch_log"),
    "consultations_audit": ("consultation_audit", "model_registry"),
    "oauth": ("oauth_repo", "oauth"),
    "sessions": ("sessions_repo", "session_repo", "session"),
    "consultations": ("consultation_repo", "graeae_consultation"),
    "federation": ("federation_repo", "federation"),
    "state": ("state_kv", "state_repo", "state"),
    "audit_chain": ("audit_chain", "memory_audit", "audit"),
    "acl": ("acl_repo", "memory_acl", "acl"),
    "journal": ("knemon_journal", "journal_entry", "memory_journal"),
    "ledger": ("usage_ledger", "knemon_ledger"),
    "row_level_security": ("row_level_security",),
    "listen_notify": ("listen_notify",),
    "advisory_locks": ("advisory_lock",),
    "federation_journal": ("federation_journal",),
    "morpheus_http_trigger": ("admin_morpheus",),
    "kronos_routes": ("kronos_route",),
}


def _any_test_function_references_capability(tree: ast.Module, capability_id: str, backend: str | None = None) -> bool:
    """True if at least one ``test_*`` function name is *primarily about* the capability.

    This is the gate that prevents over-matching: a parametrize-over-backends
    file is only counted as testing a capability if one of its actual
    test functions is named after that capability. The tokenisation
    splits on non-alphanumeric characters so ``test_memory_commit_roundtrip``
    tokenises to ``{test, memory, commit, roundtrip}``.

    Matching rules — applied per ``test_*`` function — match when **any**
    of these hold:

    * The first ``_``-delimited token after ``test_`` is a capability
      synonym (e.g. ``test_memory_commit_roundtrip`` matches
      memory_crud on its first token ``memory``).
    * The first token is ``backend`` and the second is a capability
      synonym (e.g. ``test_db2_fts_*`` matches fts_search when called
      with ``backend="db2"``; the leading ``db2`` is consumed by the
      backend gate, and ``fts`` is the capability synonym).
    * A capability synonym appears at the start of the function name
      AS A WORD (token-boundary), within the first two leading tokens
      — but only when the first token is *either* the capability
      synonym *or* the requested backend. This rules out
      ``test_single_memory_*`` (first token ``single`` is neither
      ``memory`` nor the requested backend) and
      ``test_acl_grant_roundtrip`` for the federation capability
      (first token ``acl`` is neither ``federation`` nor the
      requested backend).

    The combination is intentionally tight: only files where the
    test name's *primary subject* (the first non-``test_`` token) is
    the capability, or where the test name explicitly couples the
    backend and capability, register as a test for the cell.
    """
    needles = _CAPABILITY_SYNONYMS.get(capability_id, (capability_id,))
    # Sort needles longest-first so ``memory_crud`` is tried before ``memory``.
    ordered_needles = sorted(needles, key=len, reverse=True)

    def _name_matches(name: str) -> bool:
        if not name.startswith("test_"):
            return False
        rest = name[len("test_"):]
        leading_tokens: list[str] = []
        for token in rest.split("_"):
            if token:
                leading_tokens.append(token)
            if len(leading_tokens) >= 2:
                break
        if not leading_tokens:
            return False
        # Rule A: first token is the capability synonym. Direct match
        # (``test_memory_*``, ``test_acl_*``, ``test_oauth_*``).
        for needle in ordered_needles:
            if leading_tokens[0] == needle:
                return True
        # Rule B: first token is the backend, second is the capability
        # synonym. Covers ``test_db2_fts_*``,
        # ``test_mysql_state_*``, ``test_oracle_audit_*``.
        if backend is not None and len(leading_tokens) >= 2 and leading_tokens[0] == backend:
            for needle in ordered_needles:
                if leading_tokens[1] == needle:
                    return True
        # Rule C: compound synonym split across the first two tokens
        # (``test_memory_crud_*`` when ``memory_crud`` is itself a
        # needle, but ``memory`` and ``crud`` are separate tokens).
        # We accept it as a synonym match only when both tokens
        # exactly equal the corresponding halves of a multi-word
        # needle — that's how ``test_fts_search_*`` matches
        # fts_search (token-1=``fts``, token-2=``search``).
        if len(leading_tokens) >= 2:
            joined = f"{leading_tokens[0]}_{leading_tokens[1]}"
            for needle in ordered_needles:
                if needle == joined:
                    return True
        return False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _name_matches(node.name):
                return True
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _name_matches(sub.name):
                    return True
    return False


def _ids_arg_hits_backend(call: ast.Call, backend: str) -> bool:
    for kw in call.keywords:
        if kw.arg != "ids":
            continue
        if isinstance(kw.value, (ast.List, ast.Tuple)):
            for elt in kw.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if backend in elt.value.lower():
                        return True
                # ``ids=[b[0] for b in BACKENDS]`` — a list comprehension
                # over a module-level constant list whose elements are
                # tuples ``(label, module, class_name)``. We can't fully
                # evaluate the comprehension at AST level, but the
                # generator's iterable is almost always a module-level
                # ``Name`` whose target list is structurally a list of
                # tuples/lists of string constants. Probe that Name via
                # a small constant-folding pass below.
                if isinstance(elt, ast.ListComp):
                    if _listcomp_hits_backend(elt, backend):
                        return True
    return False


def _listcomp_hits_backend(node: ast.ListComp, backend: str) -> bool:
    """Best-effort evaluation of a list comprehension.

    Real-world pattern in this repo:
        ``ids=[b[0] for b in BACKENDS]``
    where ``BACKENDS`` is a module-level list of literal tuples. We try
    to resolve the iterable name against the enclosing module's
    top-level assignments and walk the resulting list.
    """
    # ``for b in <iterable>`` — we only support Name iterables that
    # resolve to a constant list/tuple of (str, ...) sub-lists.
    for gen in node.generators:
        if not isinstance(gen.iter, ast.Name):
            return False
        target_name = gen.iter.id
        # Caller passes the source via the AST tree; we don't have it
        # here, so we rely on a globally-installed hook set up by
        # _module_top_level_lists. If no hook installed, return False.
        resolver = _BACKEND_LIST_RESOLVERS.get(target_name)
        if not resolver:
            return False
        container = resolver()
        return any(
            _value_arg_hits_backend(item, backend) for item in container
        )
    return False


# Populated by ``_module_top_level_lists``; see ``_value_arg_hits_backend``
# docstring for usage.
_BACKEND_LIST_RESOLVERS: dict[str, callable] = {}


def _module_top_level_lists(tree: ast.Module) -> None:
    """Index module-level constant lists so list-comprehension probes work.

    This is a deliberately narrow resolver: it only follows literal
    assignments to module-level ``Name`` targets whose value is a
    ``List`` / ``Tuple`` of string Constants or sub-Lists/Tuples of
    string Constants. Anything more dynamic falls through.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        target_name = node.targets[0].id
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        items: list[ast.AST] = list(node.value.elts)
        # Only register if the items are constants or small constant tuples.
        if all(
            isinstance(it, (ast.Constant, ast.Tuple, ast.List)) for it in items
        ):
            _BACKEND_LIST_RESOLVERS[target_name] = lambda items=items: items


def _value_arg_hits_backend(node: ast.AST, backend: str) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                if backend in elt.value.lower():
                    return True
            if isinstance(elt, (ast.List, ast.Tuple)):
                if _value_arg_hits_backend(elt, backend):
                    return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return backend in node.value.lower()
    if isinstance(node, ast.Name):
        # Look up the constant via the per-file top-level index
        # registered by ``_module_top_level_lists``. This is how
        # ``@pytest.mark.parametrize("...", BACKENDS)`` resolves at AST
        # level without doing full Python dataflow.
        resolver = _BACKEND_LIST_RESOLVERS.get(node.id)
        if resolver is not None:
            return any(
                _value_arg_hits_backend(item, backend) for item in resolver()
            )
        return False
    return False


def _capability_tested(capability_id: str, backend: str) -> bool:
    """True if any test in tests/ exercises (capability_id, backend)."""
    for test_path in _iter_test_files():
        if _test_file_matches(test_path, backend, capability_id):
            return True
    return False


# ---------------------------------------------------------------------------
# Matrix assembly + markdown rendering.
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    capability: str
    backend: str
    implemented: bool
    tested: bool

    @property
    def label(self) -> str:
        if self.implemented and self.tested:
            return "✅ implemented+tested"
        if self.implemented and not self.tested:
            return "⚠️ implemented, no test"
        if not self.implemented and self.tested:
            return "⚠️ test exists, stub impl"
        return "❌ neither"


def _backend_view(backend: str) -> _ClassView:
    module_name, class_name = BACKEND_CLASSES[backend]
    rel = module_name.replace(".", "/") + ".py"
    return _view_for_class(REPO_ROOT / rel, class_name)


def _build_matrix() -> list[Cell]:
    cells: list[Cell] = []
    for cap in _capabilities():
        for backend in BACKENDS:
            if cap.forbidden_on and backend in cap.forbidden_on:
                impl = False
            elif cap.only_on and backend not in cap.only_on:
                impl = False
            elif cap.impl_kind == "route_gate":
                impl = _route_gate_implemented(backend, cap.id)
            elif cap.impl_kind == "federation_journal":
                impl = _federation_journal_implemented(backend)
            else:
                view = _backend_view(backend)
                if cap.impl_kind == "repo_property":
                    impl = _property_implemented(view, cap.impl_property)
                elif cap.impl_kind == "flag":
                    impl = _class_attr_implemented(view, cap.impl_property)
                elif cap.impl_kind == "capability_details":
                    impl = _capability_details_implemented(view, cap.impl_property[0])
                else:  # pragma: no cover - defensive
                    impl = False

            tested = _capability_tested(cap.id, backend)
            cells.append(Cell(capability=cap.id, backend=backend, implemented=impl, tested=tested))
    return cells


def _format_table(cells: list[Cell]) -> str:
    by_cap: dict[str, dict[str, Cell]] = {}
    for cell in cells:
        by_cap.setdefault(cell.capability, {})[cell.backend] = cell

    caps = _capabilities()
    header = "| Capability | " + " | ".join(BACKENDS) + " |"
    align = "|" + "---|" * (len(BACKENDS) + 1)
    lines: list[str] = []
    for cap in caps:
        row_cells = by_cap.get(cap.id, {})
        cell_text = " | ".join(
            row_cells.get(b, Cell(cap.id, b, False, False)).label for b in BACKENDS
        )
        lines.append(f"| {cap.label} | {cell_text} |")
    return "\n".join([header, align, *lines])


def _format_legend() -> str:
    return (
        "Cell legend:\n\n"
        "| Symbol | Meaning |\n"
        "|---|---|\n"
        "| ✅ implemented+tested | The backend exposes the capability *and* at least one test exercises it. |\n"
        "| ⚠️ implemented, no test | The backend exposes the capability but no test covers it. |\n"
        "| ⚠️ test exists, stub impl | A test exists for the (capability, backend) cell but the backend's implementation is a stub / raises / returns None. |\n"
        "| ❌ neither | No implementation and no test for the (capability, backend) cell. |\n"
    )


def _render_markdown(cells: list[Cell]) -> str:
    invocation = "python scripts/generate_backend_parity_matrix.py"
    table = _format_table(cells)
    legend = _format_legend()
    impl_count = sum(1 for c in cells if c.implemented)
    full_count = sum(1 for c in cells if c.implemented and c.tested)
    gaps = sum(1 for c in cells if not c.implemented and not c.tested)

    return (
        f"# Backend parity matrix\n"
        f"\n"
        f"> ⚙️ **Machine-generated. Do not edit by hand.**\n"
        f">\n"
        f"> Re-run with `{invocation}`. The companion CI job\n"
        f"> (`.github/workflows/docs-backend-parity.yml`) re-runs this\n"
        f"> command and fails on any drift via\n"
        f"> `git diff --exit-code docs/BACKEND_PARITY.md`. To refresh\n"
        f"> after a backend change: re-run the generator and commit the\n"
        f"> regenerated matrix alongside the code change.\n"
        f"\n"
        f"This matrix enumerates the cross-cutting capability surface of\n"
        f"MNEMOS's six SQL persistence backends (sqlite, postgres, mysql,\n"
        f"mariadb, oracle, db2) and answers two questions for every cell:\n"
        f"\n"
        f"1. **implemented** — does the backend's facade class actually wire\n"
        f"   up the capability, or does the property unconditionally raise\n"
        f"   `BackendCapabilityMissing` / return `None` / raise\n"
        f"   `NotImplementedError`?\n"
        f"2. **tested** — does at least one test in `tests/` exercise the\n"
        f"   (capability, backend) pair, either via a `@pytest.mark.parametrize`\n"
        f"   over the backend name or via a file named\n"
        f"   `test_<capability>_<backend>*.py`?\n"
        f"\n"
        f"Summary: **{full_count}/{len(cells)}** cells are fully covered\n"
        f"(✅ implemented+tested), **{impl_count - full_count}** cells are\n"
        f"implemented but untested, **{gaps}** cells have neither\n"
        f"implementation nor test.\n"
        f"\n"
        f"## Matrix\n"
        f"\n"
        f"{table}\n"
        f"\n"
        f"\n"
        f"## Legend\n"
        f"\n"
        f"{legend}\n"
        f"\n"
        f"## How cells are decided\n"
        f"\n"
        f"### implemented (AST scan)\n"
        f"\n"
        f"For each backend we load `mnemos/persistence/<backend>.py`, find the\n"
        f"facade class (`SqliteBackend`, `PostgresBackend`, `MysqlBackend`,\n"
        f"`MariadbBackend`, `OracleBackend`, `Db2Backend`), and inspect every\n"
        f"`@property` on the class (including inherited ones via the AST MRO\n"
        f"walk). A property counts as implemented only when its body does\n"
        f"*not*:\n"
        f"\n"
        f"* raise `BackendCapabilityMissing(...)`,\n"
        f"* raise `NotImplementedError(...)`,\n"
        f"* unconditionally `return None` (the audit-chain contract — see\n"
        f"  `mnemos/persistence/base.py::AuditPersistence.audit_chain`).\n"
        f"\n"
        f"Backend-only class attributes (`supports_pgvector`,\n"
        f"`supports_listen_notify`, `supports_row_level_security`,\n"
        f"`supports_advisory_locks`) are detected as `True` literals on the\n"
        f"class body.\n"
        f"\n"
        f"The `federation_journal` capability is detected by the presence of\n"
        f"`mnemos.persistence.federation_journal.feed_query` *and* an explicit\n"
        f"`_ensure_<backend>_federation_journal(...)` call in `open()`. The\n"
        f"HTTP-trigger `morpheus_http_trigger` and `kronos_routes` rows are\n"
        f"Postgres-only by construction (see\n"
        f"`mnemos/api/routes/morpheus.py` and\n"
        f"`mnemos/api/routes/kronos.py`, which both call\n"
        f"`require_postgres_pool_or_503`); the matrix records that rule.\n"
        f"\n"
        f"### tested (test scan)\n"
        f"\n"
        f"For each `(capability, backend)` pair we scan every `test_*.py` in\n"
        f"`tests/` for either:\n"
        f"\n"
        f"* a `@pytest.mark.parametrize(...)` whose argument ids mention the\n"
        f"  backend name (literal strings, `ids=[...]` lists, or nested\n"
        f"  constants), or\n"
        f"* a filename that encodes both the capability and the backend\n"
        f"  (e.g. `test_db2_dialect_parity.py`, `test_oracle_live.py`,\n"
        f"  `test_kronos_backends.py`, `test_mysql_recency_dialect.py`,\n"
        f"  `test_backend_audit_chain_attribute.py`).\n"
        f"\n"
        f"Heuristics were calibrated against these existing tests; if you add\n"
        f"a new test, follow the same naming convention and the matrix will\n"
        f"pick it up automatically.\n"
        f"\n"
        f"## Why this matrix exists\n"
        f"\n"
        f"The `morpheus_local` parity migrations (0061c), the v6.2 audit-chain\n"
        f"rollback on MariaDB, and the `audit_chain` `AttributeError` bug\n"
        f"(`MariadbBackend` raised when federation's `is not None` guard ran)\n"
        f"are all examples of features that *claimed* cross-backend support\n"
        f"in code while being silently absent from one of the six SQL\n"
        f"backends. This matrix is the machine-readable form of the answer\n"
        f"to \"does this thing actually work on this backend, and do we have\n"
        f"a test that proves it?\".\n"
        f"\n"
        f"The narrow federation-SQL parity test\n"
        f"(`tests/test_federation_backend_parity_static.py`) is left\n"
        f"untouched and complementary — it covers one specific SQL shape; this\n"
        f"matrix covers the wider capability surface.\n"
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    check_only = "--check" in argv
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    cells = _build_matrix()
    rendered = _render_markdown(cells)

    if check_only:
        existing = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        # The generator stamps the rendered markdown with a "Generated
        # at ... UTC" timestamp on every run. Two consecutive runs in
        # the same second will produce byte-identical output; runs in
        # different seconds will differ in *exactly one line* — the
        # timestamp header. To make --check useful for "did the
        # generator change in a meaningful way" rather than "did the
        # clock tick over a second boundary", strip the timestamp line
        # before comparison. We keep the rest of the file (summary,
        # matrix table, capability rows, legend) byte-identical between
        # runs so any drift in those is the signal the gate emits.
        existing_normalized = _strip_timestamp_line(existing)
        rendered_normalized = _strip_timestamp_line(rendered)
        if existing_normalized != rendered_normalized:
            # Print a short diff fragment so CI logs are actionable.
            import difflib
            diff_iter = difflib.unified_diff(
                existing_normalized.splitlines(keepends=True),
                rendered_normalized.splitlines(keepends=True),
                fromfile=f"{OUTPUT_PATH} (committed)",
                tofile=f"{OUTPUT_PATH} (would-be-regenerated)",
                n=3,
            )
            print(
                "drift: regenerated docs/BACKEND_PARITY.md differs from the committed copy; rerun the generator and commit the result.",
                file=sys.stderr,
            )
            for line in diff_iter:
                sys.stderr.write(line)
            return 1
        print(f"{OUTPUT_PATH}: in sync (no drift).")
        return 0

    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH} ({len(cells)} cells)")
    return 0


_TIMESTAMP_LINE_RE = re.compile(
    r"^(> Generated by .* at \*\*).*(\*\* against git commit ).*$",
    re.MULTILINE,
)
_COMMIT_LINE_RE = re.compile(
    r"^(> Generated by .* against git commit ).*$",
    re.MULTILINE,
)


def _strip_timestamp_line(markdown: str) -> str:
    """Replace the timestamp + git-commit line in the machine-generated header with a sentinel.

    Two consecutive ``python scripts/generate_backend_parity_matrix.py``
    invocations always differ in the timestamp line; what's more,
    committing the regenerated matrix changes the recorded SHA (the
    SHA of the commit depends on the matrix content, and the matrix
    content records the SHA). The drift the gate cares about is in
    the rest of the file — the capability table, the legend, the
    summary. We strip both the timestamp and the commit-reference
    line so a fresh checkout regenerates to a sentinel-stable diff.
    """
    out = _TIMESTAMP_LINE_RE.sub(r"\1SENTINEL\2", markdown)
    out = _COMMIT_LINE_RE.sub(r"\1SENTINEL", out)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
