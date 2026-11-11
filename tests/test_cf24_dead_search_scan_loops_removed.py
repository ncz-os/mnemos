"""CF-24 / MN-31 audit: the dead ``mnemos:search:*`` SCAN loops must stay dead.

DEC M4 (refactor-research-20261031) removed the per-user search cache
(commit ``9f5a0409`` — search stopped reading ``_lc._cache`` and the
visibility epoch). The runtime hard-wires ``_lc._cache`` to ``None``
(no Redis), so the ``SCAN MATCH "mnemos:search:*"`` invalidation sweeps
in the five invalidation helpers (``acl``, ``admin``, ``dag``,
``document_import``, ``deletion_ops``) only ever matched keys of a cache
that never existed.

This suite is the mechanical-removal audit:

* no ``scan_iter`` loop remains in any of the five helpers;
* the dead search-key namespace ``mnemos:search:`` does not linger in
  any of the five production modules;
* the dead cache-key/epoch-read helpers (``_get_cache_key`` and
  ``_vis_epoch_current``) are gone from ``mnemos.core.lifecycle``;
* the behavioural contract that replaced the sweeps survives on every
  affected path: the ``/stats``-entry invalidation where it belongs and
  the visibility-epoch bump that the MCP principal-context cache
  (role / namespace authorization inputs) subscribes to.

Run it first: it must FAIL on the base commit where the dead machinery
still sits, and PASS once the loops and helpers are deleted.
"""

import inspect
from unittest.mock import AsyncMock

import pytest

from mnemos.api import memory_mutations
from mnemos.api.routes import admin as admin_route
from mnemos.api.routes import acl as acl_route
from mnemos.api.routes import dag as dag_route
from mnemos.api.routes import document_import as di_route
from mnemos.core import lifecycle
from mnemos.persistence import deletion_ops

# The five helpers named in CF-24 (acl, admin, dag, document_import,
# deletion_ops) plus the shared mutation invalidation they mirror.
# ``dag`` and ``document_import`` are covered module-wide (see
# ``test_document_import_route_never_scans_search_keys`` /
# ``test_dag_route_never_scans_search_keys``): after the CF-24 removal
# they carry no ``scan_iter``/``mnemos:search:`` residue at all, and
# neither exposes a named invalidation helper anymore.
_INVALIDATION_HELPERS = [
    (acl_route, "_invalidate_search_caches_after_acl_change"),
    (admin_route, "_invalidate_memory_read_caches"),
    (deletion_ops, "invalidate_deletion_scope_caches"),
    (memory_mutations, "_invalidate_caches_after_mutation"),
]


def _module_source(module) -> str:
    """Read the module's source file directly.

    ``inspect.getsource`` requires a function/class definition (module
    objects are not supported in that form), so for module-wide checks
    we read ``module.__file__`` and return the raw text.
    """
    with open(module.__file__, "r", encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(src: str) -> str:
    """Strip pure comment lines so prose about the removal does not
    register as live code."""
    return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))


def _function_source(module, name: str) -> str:
    """Source of a specific function exported by ``module``."""
    obj = getattr(module, name)
    return _strip_comments(inspect.getsource(obj))


@pytest.mark.parametrize(
    ("module", "name"),
    _INVALIDATION_HELPERS,
    ids=[n for _, n in _INVALIDATION_HELPERS],
)
def test_invalidation_helper_has_no_dead_scan_loop(module, name):
    """CF-24: the ``SCAN MATCH "mnemos:search:*"`` loop is dead code."""
    src = _function_source(module, name)
    assert "scan_iter" not in src, f"{module.__name__}.{name} still runs the dead search-cache SCAN sweep"


def test_document_import_route_never_scans_search_keys():
    """CF-24 site: document_import.py used to sweep search keys after import."""
    src = _strip_comments(_module_source(di_route))
    assert "scan_iter" not in src
    assert "mnemos:search:" not in src


def test_dag_route_never_scans_search_keys():
    """CF-24 site: dag.py used to sweep search keys after branch merges."""
    src = _strip_comments(_module_source(dag_route))
    assert "scan_iter" not in src
    assert "mnemos:search:" not in src


def test_no_search_key_namespace_remains_in_any_of_the_five_modules():
    """``mnemos:search:`` belonged to the removed cache; it must not linger."""
    for module in (acl_route, admin_route, dag_route, di_route, deletion_ops):
        src = _strip_comments(_module_source(module))
        assert "mnemos:search:" not in src, f"dead search-key namespace still present in {module.__name__}"


def test_dead_cache_helpers_are_gone_from_lifecycle():
    """``_get_cache_key`` / ``_vis_epoch_current`` existed only for the cache."""
    assert not hasattr(lifecycle, "_get_cache_key")
    assert not hasattr(lifecycle, "_vis_epoch_current")
    src = _strip_comments(_module_source(lifecycle))
    assert "_get_cache_key" not in src
    assert "_vis_epoch_current" not in src


# ── behavioural contract: what replaced the dead sweeps must still run ──


class _SpyCache:
    """A fake cache that records deletes; scan_iter would fail loudly."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def scan_iter(self, *, match: str, count: int):
        pytest.fail(f"dead search-cache SCAN sweep issued: match={match!r}")
        yield  # pragma: no cover — keeps this an async generator

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_acl_path_bumps_epoch_without_any_cache(monkeypatch):
    """With ``_lc._cache`` None (the production path) the epoch still bumps."""
    monkeypatch.setattr(lifecycle, "_cache", None)
    bumps: list[int] = []
    monkeypatch.setattr(lifecycle, "_vis_epoch_get_incr", AsyncMock(side_effect=lambda: bumps.append(1) or 0))
    await acl_route._invalidate_search_caches_after_acl_change()
    assert bumps == [1], "ACL grant/revoke must bump the visibility epoch"


@pytest.mark.asyncio
async def test_deletion_scope_invalidation_bumps_epoch_without_any_cache(monkeypatch):
    """Same contract on the deletion-worker invalidation path."""
    monkeypatch.setattr(lifecycle, "_cache", None)
    bumps: list[int] = []
    monkeypatch.setattr(lifecycle, "_vis_epoch_get_incr", AsyncMock(side_effect=lambda: bumps.append(1) or 0))
    await deletion_ops.invalidate_deletion_scope_caches("alice", None)
    assert bumps == [1]


@pytest.mark.asyncio
async def test_admin_stats_invalidator_keeps_stats_and_epoch(monkeypatch):
    """Admin/compression path: stats entry dropped, epoch bumped, no SCAN."""
    cache = _SpyCache()
    monkeypatch.setattr(lifecycle, "_cache", cache)
    bumps: list[int] = []
    monkeypatch.setattr(lifecycle, "_vis_epoch_get_incr", AsyncMock(side_effect=lambda: bumps.append(1) or 0))
    await admin_route._invalidate_memory_read_caches()
    assert cache.deleted == ["stats:global:v2"]
    assert bumps == [1]


def test_dag_merge_branch_keeps_epoch_bump_in_live_merge_path():
    """The merge branch that touches live tracks must keep the epoch bump."""
    src = _module_source(dag_route)
    assert "_vis_epoch_get_incr" in src, "the live-merge path must keep bumping the visibility epoch"


def test_document_import_keeps_stats_invalidation_once_per_document():
    """The post-import block must still drop the stats entry once."""
    src = _module_source(di_route)
    assert "stats:global" in src


@pytest.mark.asyncio
async def test_memory_mutation_path_keeps_stats_and_epoch(monkeypatch):
    """The shared mutation invalidation (mirrors the five routes)."""
    cache = _SpyCache()
    monkeypatch.setattr(lifecycle, "_cache", cache)
    bumps: list[int] = []
    monkeypatch.setattr(lifecycle, "_vis_epoch_get_incr", AsyncMock(side_effect=lambda: bumps.append(1) or 0))
    await memory_mutations._invalidate_caches_after_mutation()
    assert cache.deleted == ["stats:global:v2"]
    assert bumps == [1]
