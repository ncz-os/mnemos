"""Offline conformance gate for the persistence ABC <-> concrete backends.

Python's ABC machinery only rejects a concrete class that leaves an
``@abstractmethod`` unimplemented, and only at *instantiation*. It does not
catch signature drift between an ABC method and its override, nor a backend
that exposes a repository accessor for a capability it does not declare (a
silent-partial backend). There is no ``mypy``/``pyright`` gate in this repo, so
that drift is otherwise unguarded.

This module enforces, with **no live database**:

1. ``__abstractmethods__`` is empty on every concrete ``*Repository`` -- the
   backend implements the whole contract it inherits.
2. Each override's call signature matches the ABC method (parameter names,
   kinds, and defaults; annotations and return type are intentionally ignored
   so a concrete impl may narrow types).
3. **claim => serve**: for every coarse capability a backend *declares*, each
   accessor mapped to that capability returns an instance of the correct repo
   ABC.
4. **serve => claim**: an accessor that returns a real repository while its
   governing capability is undeclared is flagged (seeded allowlist documents
   known drift that later phases retire).

Oracle / Db2 arms run wherever their drivers import (CI runners). They skip
cleanly on hosts without ``oracledb`` / ``ibm_db`` -- mirroring the
DSN-gated arms in ``tests/test_persistence_parity.py``.
"""

from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence import base as pbase

# ── The repository ABCs that form the contract ──────────────────────────────
REPO_ABCS: dict[str, type] = {
    name: obj
    for name, obj in vars(pbase).items()
    if inspect.isclass(obj)
    and issubclass(obj, object)
    and name.endswith("Repository")
    and getattr(obj, "__abstractmethods__", None) is not None
    and inspect.isabstract(obj)
}

# ── Coarse capability -> [(accessor attribute, expected repo ABC)] ───────────
# Mirrors the facade Protocols in base.py (CorePersistence, OAuthPersistence,
# …). ``audit_chain`` is allowed to be ``None`` (AuditPersistence contract).
CAP_ACCESSORS: dict[str, list[tuple[str, type]]] = {
    pbase.CORE_CAPABILITY: [
        ("memories", pbase.MemoryRepository),
        ("kg_triples", pbase.KGRepository),
        ("memory_versions", pbase.VersionRepository),
        ("memory_branches", pbase.BranchRepository),
        ("compression", pbase.CompressionRepository),
        ("compression_queue", pbase.CompressionQueueRepository),
        ("webhooks", pbase.WebhookRepository),
        ("consultations_audit", pbase.ConsultationAuditRepository),
    ],
    pbase.OAUTH_CAPABILITY: [("oauth", pbase.OAuthRepository)],
    pbase.SESSIONS_CAPABILITY: [("sessions", pbase.SessionsRepository)],
    pbase.CONSULTATIONS_CAPABILITY: [("consultations", pbase.ConsultationsRepository)],
    pbase.FEDERATION_CAPABILITY: [("federation", pbase.FederationRepository)],
    pbase.STATE_CAPABILITY: [("state_kv", pbase.StateRepository)],
    pbase.AUDIT_CAPABILITY: [("audit_chain", pbase.AuditChainRepository)],
}

# Reverse map: accessor -> governing coarse capability.
ACCESSOR_CAP: dict[str, str] = {accessor: cap for cap, pairs in CAP_ACCESSORS.items() for accessor, _ in pairs}
ACCESSOR_ABC: dict[str, type] = {accessor: abc for pairs in CAP_ACCESSORS.values() for accessor, abc in pairs}

# Accessors that return a real repository on a backend whose governing
# capability is (currently) undeclared. Seeded with documented drift; phases
# after P1 retire entries by either declaring the capability (when the repo is
# real and complete) or making the accessor raise BackendCapabilityMissing.
KNOWN_UNDECLARED: set[tuple[str, str]] = {
    ("mysql", "federation"),
}

# Concrete repo methods whose signature is known to diverge from the ABC.
# Each entry is documented drift slated for repair in a later phase; the
# allowlist keeps the gate green while preventing *new* drift. Retire entries
# as the underlying impl is fixed.
#   key: "<BackendRepoQualname>.<method>"  value: tracking note
KNOWN_SIGNATURE_DRIFT: dict[str, str] = {
    # Keep empty unless a concrete repo signature drift is intentionally
    # tolerated while a documented repair is pending.
}

# ── Backend construction (dummy pool; no connection opened) ──────────────────
# value: (module attribute for the Backend class, factory taking the class).
BACKENDS: dict[str, tuple[str, Any]] = {
    "sqlite": ("SqliteBackend", lambda C: C(":memory:", SimpleNamespace())),
    "postgres": ("PostgresBackend", lambda C: C(None, SimpleNamespace())),
    "mysql": ("MysqlBackend", lambda C: C(None, SimpleNamespace())),
    "oracle": ("OracleBackend", lambda C: C(None, SimpleNamespace())),
    "db2": ("Db2Backend", lambda C: C(None, SimpleNamespace())),
}

OPTIONAL_DRIVERS: dict[str, set[str]] = {
    "oracle": {"oracledb"},
    "db2": {"ibm_db", "ibm_db_dbi"},
}


def _import_backend_module(name: str):
    """Import a backend module or skip the arm if its driver is absent."""
    try:
        return importlib.import_module(f"mnemos.persistence.{name}")
    except ModuleNotFoundError as exc:
        optional_drivers = OPTIONAL_DRIVERS.get(name, set())
        if exc.name in optional_drivers:
            pytest.skip(f"{name} backend module missing optional driver {exc.name!r}: {exc!r}")
        raise


def _concrete_repos(module) -> list[type]:
    """Concrete ``*Repository`` classes defined in this backend module."""
    out: list[type] = []
    for _, obj in vars(module).items():
        if not inspect.isclass(obj):
            continue
        if obj.__module__ != module.__name__:
            continue
        if not obj.__name__.endswith("Repository"):
            continue
        if not any(issubclass(obj, abc) for abc in REPO_ABCS.values()):
            continue
        out.append(obj)
    return out


def _abc_base_of(concrete: type) -> type | None:
    """The repository ABC a concrete repo implements (most-derived match)."""
    candidates = [abc for abc in REPO_ABCS.values() if issubclass(concrete, abc)]
    if not candidates:
        return None
    # Most-derived ABC wins (e.g. if a future ABC subclasses another).
    candidates.sort(key=lambda c: len(c.__mro__), reverse=True)
    return candidates[0]


_VAR_KINDS = (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
_POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def _parameter_kind_is_compatible(abc_kind: inspect._ParameterKind, impl_kind: inspect._ParameterKind) -> bool:
    """Whether impl accepts every call shape allowed for an ABC parameter."""
    if impl_kind == inspect.Parameter.VAR_POSITIONAL:
        return abc_kind == inspect.Parameter.POSITIONAL_ONLY
    if impl_kind == inspect.Parameter.VAR_KEYWORD:
        return abc_kind == inspect.Parameter.KEYWORD_ONLY
    if abc_kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
        return impl_kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    if abc_kind == inspect.Parameter.KEYWORD_ONLY:
        return impl_kind in {
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    if abc_kind == inspect.Parameter.POSITIONAL_ONLY:
        return impl_kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    return abc_kind == impl_kind


def _signature_incompatibilities(abc_sig: inspect.Signature, impl_sig: inspect.Signature) -> list[str]:
    """Return reasons an impl signature is *incompatible* with the ABC.

    A ``**kwargs`` / ``*args`` impl that absorbs the ABC parameters is treated
    as compatible (a legitimate dispatch style). Flagged as drift only when:
      - an explicitly-named param has a different default than the ABC
        (silent behavioral divergence at the contract boundary),
      - an ABC-optional param is made required by the impl,
      - an ABC param is neither named nor absorbed by ``*args``/``**kwargs``,
      - the impl *requires* a param the ABC never supplies.
    """
    abc_params = [p for p in abc_sig.parameters.values() if p.name != "self"]
    impl_params = [p for p in impl_sig.parameters.values() if p.name != "self"]
    impl_by_name = {p.name: p for p in impl_params}
    impl_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in impl_params)
    impl_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in impl_params)
    empty = inspect.Parameter.empty
    problems: list[str] = []

    shared_positional_names = {
        p.name
        for p in abc_params
        if p.kind in _POSITIONAL_KINDS and (ip := impl_by_name.get(p.name)) is not None and ip.kind in _POSITIONAL_KINDS
    }
    abc_positional = [p.name for p in abc_params if p.name in shared_positional_names]
    impl_positional = [p.name for p in impl_params if p.name in shared_positional_names]
    if abc_positional != impl_positional:
        for name in abc_positional:
            if abc_positional.index(name) != impl_positional.index(name):
                problems.append(f"{name}: positional order differs from ABC")
                break

    for p in abc_params:
        if p.kind in _VAR_KINDS:
            continue
        ip = impl_by_name.get(p.name)
        if ip is not None:
            if not _parameter_kind_is_compatible(p.kind, ip.kind):
                problems.append(f"{p.name}: kind {p.kind.description}(abc) narrowed to {ip.kind.description}(impl)")
            if p.default is not empty and ip.default is not empty and p.default != ip.default:
                problems.append(f"{p.name}: default {p.default!r}(abc) != {ip.default!r}(impl)")
            if p.default is not empty and ip.default is empty and ip.kind not in _VAR_KINDS:
                problems.append(f"{p.name}: abc-optional but impl makes it required")
            continue
        # Not named in impl -> must be absorbed by a var-kind, else missing.
        if p.kind == inspect.Parameter.KEYWORD_ONLY and impl_var_kw:
            continue
        if p.kind == inspect.Parameter.POSITIONAL_ONLY and impl_var_pos:
            continue
        if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD and impl_var_pos and impl_var_kw:
            continue
        problems.append(f"{p.name}: ABC param absent from impl (no */** to absorb it)")

    abc_names = {p.name for p in abc_params}
    for ip in impl_params:
        if ip.kind in _VAR_KINDS or ip.name in abc_names:
            continue
        if ip.default is empty:
            problems.append(f"{ip.name}: impl requires a param the ABC never supplies")
    return problems


def _param(name: str, kind: inspect._ParameterKind) -> inspect.Parameter:
    return inspect.Parameter(name, kind)


def _sig(*params: inspect.Parameter) -> inspect.Signature:
    return inspect.Signature(params)


@pytest.mark.parametrize(
    ("abc_sig", "impl_sig", "want_problem"),
    [
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(_param("value", inspect.Parameter.KEYWORD_ONLY)),
            True,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(_param("value", inspect.Parameter.POSITIONAL_ONLY)),
            True,
        ),
        (
            _sig(_param("value", inspect.Parameter.KEYWORD_ONLY)),
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.KEYWORD_ONLY)),
            _sig(_param("value", inspect.Parameter.POSITIONAL_ONLY)),
            True,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_ONLY)),
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_ONLY)),
            _sig(_param("value", inspect.Parameter.KEYWORD_ONLY)),
            True,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_ONLY)),
            _sig(_param("args", inspect.Parameter.VAR_POSITIONAL)),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.KEYWORD_ONLY)),
            _sig(_param("kwargs", inspect.Parameter.VAR_KEYWORD)),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(
                _param("args", inspect.Parameter.VAR_POSITIONAL),
                _param("kwargs", inspect.Parameter.VAR_KEYWORD),
            ),
            False,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(_param("args", inspect.Parameter.VAR_POSITIONAL)),
            True,
        ),
        (
            _sig(_param("value", inspect.Parameter.POSITIONAL_OR_KEYWORD)),
            _sig(_param("kwargs", inspect.Parameter.VAR_KEYWORD)),
            True,
        ),
    ],
)
def test_signature_incompatibilities_detects_parameter_kind_drift(
    abc_sig: inspect.Signature,
    impl_sig: inspect.Signature,
    want_problem: bool,
) -> None:
    """Same-name params may only widen, while */** must absorb all call styles."""
    problems = _signature_incompatibilities(abc_sig, impl_sig)
    assert bool(problems) is want_problem


def test_signature_incompatibilities_detects_positional_order_drift() -> None:
    """Same-name positional params must keep ABC left-to-right binding order."""
    abc_sig = _sig(
        _param("left", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        _param("right", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    )
    same_order = _sig(
        _param("left", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        _param("right", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    )
    swapped = _sig(
        _param("right", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        _param("left", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    )

    assert not _signature_incompatibilities(abc_sig, same_order)
    assert "left: positional order differs from ABC" in _signature_incompatibilities(abc_sig, swapped)


def test_repo_abcs_discovered() -> None:
    """Guard: the contract set is non-empty (import / refactor canary)."""
    assert REPO_ABCS, "no repository ABCs discovered in persistence.base"
    assert "MemoryRepository" in REPO_ABCS


@pytest.mark.parametrize("backend_name", list(BACKENDS))
def test_no_unimplemented_abstractmethods(backend_name: str) -> None:
    """Every concrete repo implements the entire ABC contract it inherits."""
    module = _import_backend_module(backend_name)
    repos = _concrete_repos(module)
    assert repos, f"no concrete repositories found in {backend_name} backend"
    offenders = {repo.__qualname__: sorted(repo.__abstractmethods__) for repo in repos if repo.__abstractmethods__}
    assert not offenders, f"{backend_name}: concrete repositories with unimplemented abstractmethods: {offenders}"


@pytest.mark.parametrize("backend_name", list(BACKENDS))
def test_override_signatures_match_abc(backend_name: str) -> None:
    """Concrete override signatures match the ABC (params/kinds/defaults)."""
    module = _import_backend_module(backend_name)
    drift: list[str] = []
    for repo in _concrete_repos(module):
        abc = _abc_base_of(repo)
        if abc is None:
            continue
        for meth_name in sorted(abc.__abstractmethods__):
            abc_attr = inspect.getattr_static(abc, meth_name)
            cur_attr = inspect.getattr_static(repo, meth_name)
            # Properties: compare the getter signature when both are properties.
            if isinstance(abc_attr, property):
                if not isinstance(cur_attr, property):
                    drift.append(f"{repo.__qualname__}.{meth_name}: ABC property overridden by non-property")
                continue
            try:
                abc_sig = inspect.signature(abc_attr)
                cur_sig = inspect.signature(cur_attr)
            except (TypeError, ValueError):
                continue
            if f"{repo.__qualname__}.{meth_name}" in KNOWN_SIGNATURE_DRIFT:
                continue
            problems = _signature_incompatibilities(abc_sig, cur_sig)
            if problems:
                drift.append(f"{repo.__qualname__}.{meth_name}: " + "; ".join(problems))
    assert not drift, f"{backend_name}: signature drift vs ABC:\n  " + "\n  ".join(drift)


def _is_optional_driver_missing(backend_name: str, exc: BaseException) -> bool:
    optional_drivers = OPTIONAL_DRIVERS.get(backend_name, set())
    return isinstance(exc, ModuleNotFoundError) and exc.name in optional_drivers


def _construct(backend_name: str):
    """Construct a backend with a dummy pool, skipping only absent optional drivers."""
    module = _import_backend_module(backend_name)
    attr, factory = BACKENDS[backend_name]
    cls = getattr(module, attr, None)
    if cls is None:
        pytest.fail(f"{backend_name}: {attr} not exported")
    try:
        return module, factory(cls)
    except Exception as exc:
        if _is_optional_driver_missing(backend_name, exc):
            pytest.skip(f"{backend_name}: cannot construct without optional driver {exc.name!r}: {exc!r}")
        raise


@pytest.mark.parametrize("backend_name", list(BACKENDS))
def test_declared_capability_is_served(backend_name: str) -> None:
    """claim => serve: every declared capability's accessor returns its ABC."""
    _, backend = _construct(backend_name)
    caps = set(getattr(backend, "capabilities", set()))
    missing: list[str] = []
    for cap, pairs in CAP_ACCESSORS.items():
        if cap not in caps:
            continue
        for accessor, abc in pairs:
            try:
                value = getattr(backend, accessor)
            except Exception as exc:
                missing.append(f"{accessor}: declared '{cap}' but accessor raised {exc!r}")
                continue
            if accessor == "audit_chain" and value is None:
                continue  # AuditPersistence allows None
            if not isinstance(value, abc):
                missing.append(f"{accessor}: declared '{cap}' but returned {type(value).__name__}, not {abc.__name__}")
    assert not missing, f"{backend_name}: declared-but-unserved capabilities:\n  " + "\n  ".join(missing)


@pytest.mark.parametrize("backend_name", list(BACKENDS))
def test_served_accessor_is_declared(backend_name: str) -> None:
    """serve => claim: a real repo behind an undeclared capability is drift."""
    _, backend = _construct(backend_name)
    caps = set(getattr(backend, "capabilities", set()))
    undeclared: list[str] = []
    for accessor, cap in ACCESSOR_CAP.items():
        if cap in caps:
            continue
        if (backend_name, accessor) in KNOWN_UNDECLARED:
            continue
        try:
            value = getattr(backend, accessor)
        except Exception:
            continue  # raising on an undeclared accessor is correct
        if value is None:
            continue
        # Returns a real repo while the capability is undeclared.
        abc = ACCESSOR_ABC.get(accessor)
        if abc is not None and isinstance(value, abc):
            undeclared.append(f"{accessor}: serves {type(value).__name__} but '{cap}' undeclared")
    assert not undeclared, (
        f"{backend_name}: undeclared-but-served accessors (add to capabilities "
        f"or make the accessor raise BackendCapabilityMissing):\n  " + "\n  ".join(undeclared)
    )
