"""MN-31 / CF-24 — the ``mnemos:search:*`` SCAN sweeps are dead code.

Background
----------
The per-user search cache was removed: ``mnemos/core/lifecycle.py``
hard-wires ``_cache = None`` ("No Redis cache", no Redis in the runtime),
so every ``_lc._cache`` read misses and every write goes nowhere.  Its
invalidation sweep — ``async for k in _lc._cache.scan_iter(match=
"mnemos:search:*")`` — therefore can never match a live key: the cache
object it scans is ``None``, and even a non-``None`` cache would hold no
``mnemos:search:*`` keys because nothing writes them any more.

``mnemos/api/memory_mutations.py::_invalidate_caches_after_mutation``
already dropped the same sweep.  Five sites were missed (audit CF-24):

  * ``mnemos/api/routes/acl.py:59``
  * ``mnemos/api/routes/admin.py:403``
  * ``mnemos/api/routes/dag.py:1006``
  * ``mnemos/api/routes/document_import.py:683``
  * ``mnemos/persistence/deletion_ops.py:405``

What must NOT change
--------------------
These helpers do more than the sweep, and every one of those behaviours
is load-bearing, so the tests below pin them as hard as they pin the
removal:

  * the ``stats:global`` / ``stats:global:v2`` evictions (the stats
    surface is a *separate* cache and is still live),
  * the visibility-epoch bump (``_vis_epoch_get_incr``) — the MCP
    principal-context cache subscribes to it, so an ACL revoke or a
    delete must still advance it,
  * the guard semantics (acl: nothing removed -> no invalidation;
    deletion: the target's stats entries are still dropped).

The source scans below are deliberately textual and comment-stripped:
they fail on *live code*, not on a prose mention in a docstring
explaining why the sweep is gone.
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import io
import tokenize
from pathlib import Path

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.routes import acl as acl_route
from mnemos.api.routes import admin as admin_route
from mnemos.persistence import deletion_ops

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Every site the audit named, plus the guard that the scan did not simply
# move: these are the only files allowed to mention the pattern at all.
_FORBIDDEN_FRAGMENT = "mnemos:search:"

# The five files whose dead sweep must be gone.  ``memory_mutations.py``
# was already swept and ``lifecycle.py`` only mentions it in prose (a
# docstring on ``_get_cache_key``), both checked separately below.
_SWEPT_FILES = [
    "mnemos/api/routes/acl.py",
    "mnemos/api/routes/admin.py",
    "mnemos/api/routes/dag.py",
    "mnemos/api/routes/document_import.py",
    "mnemos/persistence/deletion_ops.py",
]


def _stripped_source(path: Path) -> str:
    """Return *path*'s source with comments and docstrings removed.

    ``ast`` blanks the docstrings (the surviving prose explains *why* the
    sweep is gone, so it legitimately names the pattern), and ``tokenize``
    blanks real ``#`` comments while leaving string literals intact — an
    inline ``... # mnemos:search:*`` note must not read as live code, but
    a string literal *is* live code and must still be caught.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            for lineno in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                lines[lineno - 1] = ""

    comment_rows: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comment_rows.add(tok.start[0])
    for lineno in comment_rows:
        lines[lineno - 1] = ""

    return "\n".join(lines)


# ── 1. the sweep is gone from every audited file ─────────────────────────────


@pytest.mark.parametrize("relpath", _SWEPT_FILES)
def test_audited_file_has_no_search_scan_in_live_code(relpath):
    """No audited file still scans ``mnemos:search:*`` in live code."""
    path = _REPO_ROOT / relpath
    assert path.is_file(), f"audit evidence points at a missing file: {relpath}"
    code = _stripped_source(path)
    assert _FORBIDDEN_FRAGMENT not in code, (
        f"{relpath} still contains the dead `mnemos:search:*` sweep in live code " "(CF-24 / MN-31)"
    )


@pytest.mark.parametrize("relpath", _SWEPT_FILES)
def test_audited_file_has_no_scan_iter_call_at_all(relpath):
    """``_lc._cache.scan_iter`` is unreachable-by-construction; drop the call.

    A ``scan_iter`` over a cache that is hard-wired to ``None`` and holds
    no matching keys is dead weight whether or not the pattern string is
    spelled out literally.
    """
    path = _REPO_ROOT / relpath
    code = _stripped_source(path)
    assert "scan_iter" not in code, f"{relpath} still calls scan_iter (CF-24 / MN-31)"


def test_memory_mutations_precedent_still_scan_free():
    """The already-swept site must not regress."""
    code = _stripped_source(_REPO_ROOT / "mnemos/api/memory_mutations.py")
    assert "scan_iter" not in code
    assert _FORBIDDEN_FRAGMENT not in code


# ── 2. the surviving work in each helper still happens ───────────────────────


class _RecordingCache:
    """Minimal stand-in for the (retired) cache object.

    Records every touch so the assertion can prove which keys the helper
    still evicts.  ``scan_iter`` raising keeps us honest: if a helper
    regains a sweep this fake makes the call visible rather than silent.
    """

    def __init__(self, keys: list[str] | None = None) -> None:
        self.store = dict.fromkeys(keys or [], "{}")
        self.deleted: list[str] = []
        self.scanned = 0

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)

    async def scan_iter(self, *, match: str, count: int = 0):  # noqa: ARG002
        # An async generator, matching the cache API shape.  Reaching it is
        # the failure this fake exists to expose.
        self.scanned += 1
        for key in ():  # pragma: no cover — never yields
            yield key


@pytest.fixture()
def epochs(monkeypatch):
    """Count visibility-epoch bumps without touching NATS."""
    calls: list[int] = []

    async def _bump() -> int:
        calls.append(1)
        return len(calls)

    monkeypatch.setattr(_lc, "_vis_epoch_get_incr", _bump)
    return calls


def test_acl_helper_drops_no_search_keys_but_still_bumps_epoch(monkeypatch, epochs):
    """acl.py's helper: no sweep, epoch still bumped."""
    cache = _RecordingCache(["mnemos:search:alice:q1", "stats:global:v2"])
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)

    asyncio.run(acl_route._invalidate_search_caches_after_acl_change())

    assert cache.scanned == 0
    assert not [k for k in cache.deleted if k.startswith("mnemos:search:")]
    assert epochs == [1]


def test_admin_helper_still_drops_stats_global_v2(monkeypatch, epochs):
    """admin.py's helper: the stats surface is separate and still live."""
    cache = _RecordingCache(["stats:global:v2", "mnemos:search:x:q"])
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)

    asyncio.run(admin_route._invalidate_memory_read_caches())

    assert "stats:global:v2" in cache.deleted
    assert cache.scanned == 0
    assert not [k for k in cache.deleted if k.startswith("mnemos:search:")]
    assert epochs == [1]


def test_deletion_ops_helper_still_drops_both_stats_keys(monkeypatch, epochs):
    """deletion_ops: stats eviction + epoch bump survive the sweep removal."""
    cache = _RecordingCache(["stats:global", "stats:global:v2", "mnemos:search:u:q"])
    monkeypatch.setattr(_lc, "_cache", cache, raising=False)

    asyncio.run(deletion_ops.invalidate_deletion_scope_caches("alice", "tenant-a"))

    assert "stats:global" in cache.deleted
    assert "stats:global:v2" in cache.deleted
    assert cache.scanned == 0
    assert not [k for k in cache.deleted if k.startswith("mnemos:search:")]
    assert epochs == [1]


@pytest.mark.parametrize(
    ("helper", "label"),
    [
        (lambda: acl_route._invalidate_search_caches_after_acl_change(), "acl"),
        (lambda: admin_route._invalidate_memory_read_caches(), "admin"),
        (
            lambda: deletion_ops.invalidate_deletion_scope_caches("alice", None),
            "deletion_ops",
        ),
    ],
    ids=["acl", "admin", "deletion_ops"],
)
def test_helper_still_bumps_epoch_when_cache_is_none(monkeypatch, epochs, helper, label):
    """``_cache`` is ``None`` in the runtime, so the bump must not be gated on it.

    Every one of these helpers used to ``return`` early when the cache was
    falsy — and the lifecycle hard-wires ``_cache = None``, so in production
    the visibility-epoch bump (the MCP principal-context cache's only
    default-epoch signal) never ran.
    """
    monkeypatch.setattr(_lc, "_cache", None, raising=False)

    asyncio.run(helper())

    assert epochs == [1], f"{label}: epoch bump was skipped when _cache is None"


# ── 3. the helpers' docstrings describe the state after the fix ──────────────


def test_acl_helper_docstring_no_longer_promises_a_search_cache():
    """The docstring must not read as "this drops a search cache".

    Naming the removed cache in the past tense (why the sweep is gone) is
    fine and useful; promising the drop is not.
    """
    doc = (inspect.getdoc(acl_route._invalidate_search_caches_after_acl_change) or "").lower()
    assert "drop the per-user search cache" not in doc
    assert "search responses are cached" not in doc


def test_deletion_ops_docstring_no_longer_promises_search_eviction():
    doc = (inspect.getdoc(deletion_ops.invalidate_deletion_scope_caches) or "").lower()
    assert "evict cached search" not in doc
    assert "search/stat" not in doc
