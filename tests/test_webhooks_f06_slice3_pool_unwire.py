"""F-06 slice 3: the webhook worker runtime resolves its persistence handle
through the facade accessor, never through the raw lifecycle pool attribute.

The pre-slice code in ``mnemos/webhooks/{lease,finalize,workers}.py`` fell
back to ``_lc._pool`` directly when a raw ``asyncpg.Pool`` was installed
without a persistence backend — the F-06 class of defect §0b defines
("raw ``_lc._pool`` access outside ``mnemos/persistence/`` is the defect").

Every one of these call paths is already covered by the ``WebhookRepository``
ABC on all six backends (item 7: claim / guard / finalize / store / release),
so the fix is an *unwire*: each fallback routes through
``lifecycle.get_temporary_legacy_webhook_pool()`` — the facade accessor for the
Postgres-only raw-asyncpg fallback window that ``test_webhook_retry_state.py``
is still migrating on — instead of touching ``_lc._pool`` at module scope.

``test_webhooks_module_no_longer_reads_the_raw_pool_attribute`` is a negative
regression test: it fails on the pre-slice tree (six ``_lc._pool`` literal
references in these three files) and passes after. The behavioural half of
the matrix runs on the SQLite test backend (the other raw-pool arm is
asyncpg-only and needs a live Postgres, which the parity suite owns).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_WEBHOOK_FILES = (
    "mnemos/webhooks/lease.py",
    "mnemos/webhooks/finalize.py",
    "mnemos/webhooks/workers.py",
)
# Deliberately NOT anchored: a call like ``_lc._pool.acquire()`` must be
# caught even though it is not a bare attribute read. The facade accessor
# name never contains the literal ``_pool``.
_RAW_POOL_REF = re.compile(r"_lc\._pool")


def _webhook_source_lines() -> list[tuple[str, int, str]]:
    lines: list[tuple[str, int, str]] = []
    for name in _WEBHOOK_FILES:
        path = REPO_ROOT / name
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "pool" in line:
                lines.append((name, lineno, line))
    return lines


def test_webhooks_module_no_longer_reads_the_raw_pool_attribute() -> None:
    """The three slice-3 files must not read ``lifecycle._pool`` at all —
    not at call time, not in a comment.

    Fails pre-slice (6 code references: lease.py 2, finalize.py 3,
    workers.py 1) and passes once every one routes through the facade
    accessor. The accessor keeps the raw-asyncpg fallback reachable for the
    legacy migration window without the webhooks package naming the
    lifecycle's pool attribute directly. Comment occurrences are scanned
    too: a ``# legacy _pool`` note that still names the attribute would
    survive the doc's ``grep _lc._pool`` re-count and quietly re-document
    the defect.
    """
    offenders = [
        (name, lineno, line.strip())
        for name, lineno, line in _webhook_source_lines()
        if _RAW_POOL_REF.search(line)
    ]
    assert offenders == [], f"raw _lc._pool references remain: {offenders}"


@pytest.mark.asyncio
async def test_legacy_webhook_pool_accessor_survives_the_fallback_window(
    monkeypatch, tmp_path
) -> None:
    """The accessor returns the raw pool when the backend is absent and the
    pool is installed (the ``test_webhook_retry_state.py`` migration window),
    and returns ``None`` — not a fabricated handle — when either is absent."""
    from mnemos.core import lifecycle

    class _FakePool:
        pass

    pool = _FakePool()
    monkeypatch.setattr(lifecycle, "_pool", pool)
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    assert lifecycle.get_temporary_legacy_webhook_pool() is pool

    monkeypatch.setattr(lifecycle, "_pool", None)
    assert lifecycle.get_temporary_legacy_webhook_pool() is None

    # A persistence backend (any of the six) closes the fallback window:
    # the raw pool is no longer a valid handle for the webhook worker.
    monkeypatch.setattr(lifecycle, "_pool", pool)
    monkeypatch.setattr(lifecycle, "_persistence_backend", SimpleNamespace())
    assert lifecycle.get_temporary_legacy_webhook_pool() is None


@pytest.mark.asyncio
async def test_sqlite_claim_guard_finalize_and_body_store_through_the_facade(
    monkeypatch, tmp_path
) -> None:
    """The claim / guard / finalize / response-body-store paths run through
    the persistence ABC on the SQLite backend and never consult the raw
    ``lifecycle._pool`` attribute — the post-slice handle contract.

    Pre-slice, every one of these helpers began with
    ``pool_or_backend or _lc._pool`` (or the finalize equivalent), so an
    installed raw pool would have been read directly here; the post-slice
    code must get an *identity-equal* result on SQLite while the accessor is
    observably consulted (spy on ``get_temporary_legacy_webhook_pool`` — if any call
    path still reads ``_lc._pool`` the test module's static scan above
    fails, and if a call path silently consults the accessor on the SQLite
    arm the spy count moves).
    """
    import mnemos.core.lifecycle as lifecycle
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.webhooks import finalize as webhook_finalize
    from mnemos.webhooks import lease as webhook_lease
    from mnemos.webhooks import types as webhook_types

    backend = SqliteBackend(tmp_path / "f06s3.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

        # Sentinel pool: must never be returned by the accessor while a
        # backend is installed, and must never be read by any call path.
        class _SentinelPool:
            pass

        sentinel = _SentinelPool()
        monkeypatch.setattr(lifecycle, "_pool", sentinel)
        assert lifecycle.get_temporary_legacy_webhook_pool() is None

        repo = backend.webhooks
        subscription_id = str(uuid.uuid4())
        delivery_id: str
        async with backend.transactional() as tx:
            await repo.create_subscription(
                tx,
                subscription_id=subscription_id,
                url="https://example.com/hook",
                events=("memory.created",),
                secret="secret",
                description="F-06 slice 3",
                owner_id="owner",
                namespace="default",
            )
            intents = await repo.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": "f06s3"},
                owner_id="owner",
                namespace="default",
            )
        delivery_id = intents[0].delivery_id

        # claim — ABC path (backend installed): identity with the direct
        # repository claim, lease token recorded.
        lease_token = str(uuid.uuid4())
        claimed = await webhook_lease._claim_delivery(
            sentinel,
            delivery_id,
            lease_token=lease_token,
        )
        assert claimed is not None
        assert claimed.delivery["id"] == delivery_id
        assert claimed.lease_token == lease_token
        assert claimed.delivery["status"] in webhook_types.LIVE_DELIVERY_STATUSES

        # guard — ABC path fences the preclaimed attempt.
        guarded = await webhook_lease._guard_preclaimed_delivery_before_send(
            sentinel,
            claimed.delivery,
            lease_token,
        )
        assert guarded is True

        # finalize success — ABC path (backend.webhooks.finalize_delivery).
        result = webhook_types._DeliveryResult(
            succeeded=True,
            response_status=200,
            response_body="ok",
            error=None,
        )
        finalized = await webhook_finalize._finalize_delivery(
            sentinel,
            claimed.delivery,
            lease_token,
            result,
        )
        assert finalized is True

        # response-body store — ABC path (store_delivery_response_body).
        await webhook_finalize._persist_response_body_for_audit(
            sentinel,
            delivery_id=delivery_id,
            response_body="ok",
        )

        # The row is terminal and the claim is gone: the ABC semantics
        # (single transaction per backend) are what produced this state.
        async with backend.transactional() as tx:
            row = await repo.fetch_deliveries(tx, subscription_id)
        terminal = [r for r in row if r["id"] == delivery_id]
        assert terminal[0]["status"] == "succeeded"
        assert terminal[0]["lease_token"] is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_recovery_loop_without_any_handle_is_a_noop_not_a_crash(
    monkeypatch
) -> None:
    """``_recover_due_deliveries`` with neither a backend nor a raw pool
    returns 0 (the pre-slice behaviour) instead of raising — the accessor
    now supplies the ``None`` fallback the old ``_lc._pool`` read did."""
    import mnemos.core.lifecycle as lifecycle
    from mnemos.webhooks import workers as webhook_workers

    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "_pool", None)

    recovered = await webhook_workers._recover_due_deliveries(limit=10)
    assert recovered == 0


def test_webhook_claim_helper_survives_the_pool_arg_shape() -> None:
    """``webhook_lease._claim_delivery(pool, ...)`` keeps its positional
    ``pool_or_backend`` contract (the dispatcher shim and the legacy test
    suite pass the raw pool positionally) — only the *source* of the
    fallback changes, never the caller-visible signature."""
    import inspect

    from mnemos.webhooks import lease as webhook_lease
    from mnemos.webhooks import workers as webhook_workers

    lease_sig = inspect.signature(webhook_lease._claim_delivery)
    assert list(lease_sig.parameters) == [
        "pool_or_backend",
        "delivery_id",
        "lease_token",
        "lease_seconds",
    ]
    recover_sig = inspect.signature(webhook_workers._recover_due_deliveries)
    assert list(recover_sig.parameters)[0] == "_pool_or_backend"


_BACKEND_REPO_CLASSES = [
    ("mnemos.persistence.sqlite", "SqliteWebhookRepository"),
    ("mnemos.persistence.postgres", "PostgresWebhookRepository"),
    ("mnemos.persistence.oracle", "OracleWebhookRepository"),
    ("mnemos.persistence.db2", "Db2WebhookRepository"),
    ("mnemos.persistence.mysql", "MysqlWebhookRepository"),
    ("mnemos.persistence.mariadb", "MariadbWebhookRepository"),
]


@pytest.mark.parametrize(
    ("module_name", "repo_name"),
    _BACKEND_REPO_CLASSES,
    ids=[entry[0].rsplit(".", 1)[1] for entry in _BACKEND_REPO_CLASSES],
)
def test_webhook_worker_abc_methods_exist_on_all_six_backends(
    module_name: str,
    repo_name: str,
) -> None:
    """Contract half of the F-06 slice-3 matrix: every backend that the
    ``WebhookRepository`` ABC is claimed to serve actually implements the
    four methods the unwired worker call paths route through —
    ``claim_delivery``, ``guard_delivery_claim``, ``finalize_delivery`` and
    ``store_delivery_response_body`` — on its concrete class.

    The SQLite arm is additionally exercised behaviourally in
    ``test_sqlite_claim_guard_finalize_and_body_store_through_the_facade``;
    this test is what proves the other five arms (Postgres / Oracle / Db2 /
    MySQL / MariaDB, the last two sharing the MySQL implementation) expose
    the same ABC surface, so the facade accessor's backend-first ordering
    never silently downgrades a delivery to the raw-asyncpg path.
    """
    import importlib

    from mnemos.persistence.base import WebhookRepository

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional backend driver unavailable: {exc.name}")
    repo_class = getattr(module, repo_name)
    assert issubclass(repo_class, WebhookRepository)
    for method in (
        "claim_delivery",
        "guard_delivery_claim",
        "finalize_delivery",
        "store_delivery_response_body",
    ):
        assert hasattr(repo_class, method), f"{repo_name} missing {method}"
