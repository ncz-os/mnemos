"""Static-analysis invariants for the transactional-outbox contract.

The transactional outbox pattern (memory write + webhook delivery
row insert atomic, send task scheduled post-commit) is the only
way to satisfy corpus-review-2026-04-29 finding #2: domain writes
must NEVER commit without their corresponding event row, and
event rows must NEVER fire on rolled-back data.

After item 7 (webhook runtime ABC migration), the call shape moved
from ``dispatch(event, payload, *, conn=conn)`` to
``dispatch(event, payload, *, tx=...)``: the contract that the delivery
row joins the caller's transaction is unchanged, just expressed via the
backend-neutral Transaction Protocol instead of a raw asyncpg
connection. The legacy ``conn=conn`` keyword is preserved as a
back-compat alias (forwarded to ``tx=``) so tests and call sites that
have not yet migrated keep passing without re-introducing the raw
asyncpg path.

This file pins the invariant statically: every call site of
``mnemos.webhooks.dispatcher.dispatch`` (aliased as
``_dispatch_webhook`` at most call sites) MUST pass a transactional
handle. AST-walk catches the pattern across both literal and aliased
imports; catches a future addition that introduces a non-transactional
call before code review does.

Allow-list: the dispatcher module ITSELF imports ``dispatch`` as
a public re-export. Tests are excluded.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MNEMOS_ROOT = REPO_ROOT / "mnemos"

# Files that may import ``dispatch`` without using it as the
# transactional caller pattern (the dispatcher module itself,
# package re-exports, etc.).
EXEMPT_FILES = {
    "mnemos/webhooks/dispatcher.py",
    "mnemos/webhooks/__init__.py",
}


def _module_path_relative(file: pathlib.Path) -> str:
    return str(file.relative_to(REPO_ROOT))


def _calls_with_local_aliases(tree: ast.AST) -> dict[str, list[ast.Call]]:
    """Return every Call node grouped by the local name of the
    callable — so an ``import dispatch as _dispatch_webhook``
    is found whether the call uses ``_dispatch_webhook`` or
    ``dispatch``.
    """
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module.startswith("mnemos.webhooks"):
                continue
            for alias in node.names:
                if alias.name == "dispatch":
                    aliases.add(alias.asname or alias.name)

    if not aliases:
        return {}

    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            calls.setdefault(func.id, []).append(node)
        elif isinstance(func, ast.Attribute):
            if func.attr == "dispatch":
                calls.setdefault("dispatch", []).append(node)
    return calls


def test_every_dispatch_call_passes_a_transactional_handle():
    """Every call to ``mnemos.webhooks.dispatcher.dispatch`` (or
    its locally-aliased import) must include a ``tx=`` (post item-7)
    or legacy ``conn=`` keyword argument so the webhook_deliveries
    INSERT joins the caller's transaction.

    A call without one writes the delivery row on a freshly-acquired
    backend transaction. That is the expected path for non-transactional
    callers (the no-arg path opens its own backend.transactional() and
    schedules the send task post-commit). For callers that DO need
    atomicity with their own writes, this test catches a forgotten
    transactional handle.
    """
    failures: list[tuple[str, int, str]] = []

    for path in MNEMOS_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        if rel in EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for alias, calls in _calls_with_local_aliases(tree).items():
            for call in calls:
                kwarg_names = {
                    kw.arg for kw in call.keywords if kw.arg is not None
                }
                has_handle = "tx" in kwarg_names or "conn" in kwarg_names
                if not has_handle:
                    failures.append(
                        (rel, call.lineno, alias)
                    )

    assert not failures, (
        "non-transactional webhook dispatch detected (every call to "
        "mnemos.webhooks.dispatcher.dispatch MUST pass tx=<transaction> "
        "or legacy conn= so the delivery row joins the caller's "
        "transaction when one is open):\n"
        + "\n".join(
            f"  {rel}:{lineno}  → {alias}(...)"
            for (rel, lineno, alias) in failures
        )
        + "\n\nFix: pass ``tx=tx`` from inside the data transaction; "
        "schedule the send task after commit via "
        "``_schedule_delivery_attempt(_attempt_delivery(delivery_id))``."
    )


def test_known_call_sites_present():
    """Sanity check that the AST scanner sees the call sites we
    know exist. If this assertion goes empty, the scanner is
    silently failing and the invariant test above wouldn't catch
    a real regression."""
    found: list[str] = []
    for path in MNEMOS_ROOT.rglob("*.py"):
        rel = _module_path_relative(path)
        if rel in EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for alias, calls in _calls_with_local_aliases(tree).items():
            for _call in calls:
                found.append(rel)
                break
            break

    assert len(found) >= 1, (
        "AST scanner found zero dispatcher.dispatch call sites — the "
        "invariant test cannot detect regressions if it can't find any "
        "calls in the first place. Check the import-pattern matcher."
    )


@pytest.mark.parametrize(
    ("snippet", "should_pass"),
    [
        (
            "from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook\n"
            "async def f(tx):\n"
            "    await _dispatch_webhook('memory.created', {}, tx=tx)\n",
            True,
        ),
        (
            "from mnemos.webhooks.dispatcher import dispatch\n"
            "async def f(tx):\n"
            "    await dispatch('e', {}, tx=tx)\n",
            True,
        ),
        (
            "from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook\n"
            "async def f(conn):\n"
            "    await _dispatch_webhook('memory.created', {}, conn=conn)\n",
            True,
        ),
        (
            "from mnemos.webhooks.dispatcher import dispatch as _dispatch_webhook\n"
            "async def f():\n"
            "    await _dispatch_webhook('memory.created', {})\n",
            False,
        ),
        (
            "from mnemos.webhooks import dispatcher\n"
            "async def f(tx):\n"
            "    await dispatcher.dispatch('e', {}, tx=tx)\n",
            True,
        ),
    ],
)
def test_scanner_classifies_canonical_shapes(snippet, should_pass, tmp_path):
    """Negative + positive fixtures for the AST scanner."""
    fake_module = tmp_path / "fake.py"
    fake_module.write_text(snippet)
    tree = ast.parse(snippet)
    aliases = _calls_with_local_aliases(tree)

    for _alias, calls in aliases.items():
        for call in calls:
            kwarg_names = {
                kw.arg for kw in call.keywords if kw.arg is not None
            }
            has_handle = "tx" in kwarg_names or "conn" in kwarg_names
            if should_pass:
                assert has_handle, f"transactional snippet missing tx=/conn=: {snippet!r}"
            else:
                assert not has_handle, f"non-transactional snippet should fail: {snippet!r}"
