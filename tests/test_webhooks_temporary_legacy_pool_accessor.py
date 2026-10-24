"""The temporary legacy-webhook-pool accessor: what it returns and how far
it may spread.

``mnemos.core.lifecycle.get_temporary_legacy_webhook_pool()`` is a
*temporary* Postgres-only bridge that the F-06 slice-3 unwire routes the
webhook worker's legacy raw-asyncpg fallbacks through. Two properties are
pinned here:

1. it hands out the raw pool only while the legacy migration window is
   open (no persistence backend installed) and returns ``None`` — never a
   fabricated handle — as soon as any of the six backends is installed;
2. it does not spread: the number of call sites outside
   ``mnemos/persistence/`` and the webhooks module is pinned to a fixed
   ceiling, so a new caller cannot appear silently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
_ACCESSOR = "get_temporary_legacy_webhook_pool"
_OLD_NAME = "get_legacy_webhook_pool"
# The accessor is owned by the lifecycle module; the persistence package
# and the webhooks package are its legitimate users.
_SCOPE_EXCLUDED = ("mnemos/persistence/", "mnemos/webhooks/")
# Fixed ceiling: the accessor is defined once (in
# ``mnemos/core/lifecycle.py``) and called from the webhooks module only.
# Any call site outside those directories fails this test.
_MAX_CALL_SITES_OUTSIDE = 0


def _call_sites_outside_the_webhooks_module() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "mnemos").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(rel.startswith(prefix) for prefix in _SCOPE_EXCLUDED):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _ACCESSOR not in line:
                continue
            if line.lstrip().startswith("def "):
                # The definition itself, not a call site.
                continue
            sites.append((rel, lineno, line.strip()))
    return sites


def test_temporary_accessor_returns_the_pool_only_without_a_backend(
    monkeypatch,
) -> None:
    """Backend installed -> ``None``; no backend -> the pool itself.

    This is the accessor's whole contract: the raw asyncpg pool is a
    legacy-window handle only, and the moment any persistence backend is
    installed the ``WebhookRepository`` ABC owns the webhook call paths,
    so the accessor must not hand the raw pool out any more.
    """
    from mnemos.core import lifecycle

    class _FakePool:
        pass

    pool = _FakePool()

    # No backend installed -> the legacy window is open -> the pool.
    monkeypatch.setattr(lifecycle, "_pool", pool)
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    assert lifecycle.get_temporary_legacy_webhook_pool() is pool

    # A persistence backend installed -> the legacy window is closed ->
    # None, even though the raw pool is still set on the lifecycle.
    monkeypatch.setattr(lifecycle, "_persistence_backend", SimpleNamespace())
    assert lifecycle.get_temporary_legacy_webhook_pool() is None

    # No backend and no pool -> None (never a fabricated handle).
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "_pool", None)
    assert lifecycle.get_temporary_legacy_webhook_pool() is None


def test_temporary_accessor_call_sites_cannot_spread() -> None:
    """The accessor stays a bridge for the webhooks module only.

    The count of call sites outside ``mnemos/persistence/`` and
    ``mnemos/webhooks/`` is pinned to a fixed ceiling (today: 0), so a new
    caller in another package cannot appear without this test failing.
    """
    sites = _call_sites_outside_the_webhooks_module()
    assert len(sites) <= _MAX_CALL_SITES_OUTSIDE, (
        "temporary legacy webhook pool accessor call sites outside "
        f"mnemos/persistence/ and mnemos/webhooks/ grew past "
        f"{_MAX_CALL_SITES_OUTSIDE}: {sites}"
    )


def test_the_accessor_keeps_its_old_name_only_as_the_renamed_one() -> None:
    """The pre-rename name is gone from the tree.

    The old name is what the first review asked to be renamed away, so a
    surviving ``get_legacy_webhook_pool`` reference would mean the rename
    was only half applied (two accessors, one contract).
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("master-baseline/", "tests/")):
            continue
        if _OLD_NAME in path.read_text():
            offenders.append(rel)
    assert offenders == [], f"old accessor name still present: {offenders}"
