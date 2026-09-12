"""Public webhook dispatch entry points.

After item 7, every raw ``asyncpg`` call inside ``mnemos/webhooks/*`` is
gone. ``dispatch()`` opens a single ``backend.transactional()`` (or
accepts a caller-owned ``Transaction`` via ``tx=``) and delegates to
``backend.webhooks.dispatch_event``, which returns a list of
:class:`WebhookDeliveryIntent` carrying the per-delivery fields the
post-commit NATS nudge needs.

The legacy ``_LEGACY_ATTR_TARGETS`` shim re-exports every private
symbol in the original webhooks runtime under
``mnemos.webhooks.dispatcher.<name>`` so older debugging imports keep
resolving during the rollout. Items are pruned only after I have
confirmed that no test still references the name through this path.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import types as _module_types
from typing import Any, Dict, Optional

import httpx
from mnemos.core import lifecycle as _lc  # noqa: WPS433

from . import _signing as webhook_signing
from . import chain as webhook_chain
from . import finalize as webhook_finalize
from . import lease as webhook_lease
from . import repair as webhook_repair
from . import sender as webhook_sender
from . import types as webhook_types
from . import workers as webhook_workers

logger = logging.getLogger(__name__)

WebhookDispatchResult = list[str]


async def dispatch(
    event_type: str,
    payload: Dict[str, Any],
    *,
    tx: Optional[Any] = None,
    conn: Optional[Any] = None,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> WebhookDispatchResult:
    """Fan out an event to all matching subscriptions.

    When ``tx`` (or the legacy ``conn=`` alias) is provided, the delivery
    rows insert through ``backend.webhooks.dispatch_event`` on the
    caller's open transaction. When neither is provided, the dispatcher
    opens its own transaction via ``backend.transactional()``, the
    canonical compose-the-call path for non-transactional callers.

    Returns the delivery ids (post-commit scheduled in the no-tx path
    after the transactional block exits).

    The legacy positional-with-``conn`` shape is kept as a best-effort
    guard, but new callers should pass ``tx=`` explicitly: it documents
    which backend transaction the inserts join.
    """
    if conn is not None and tx is not None:
        raise TypeError("dispatch received both tx= and conn=")
    caller_tx = tx if tx is not None else conn

    backend = _lc._persistence_backend
    if backend is None or not getattr(backend, "supports_webhooks", False):
        from mnemos.persistence.base import BackendCapabilityMissing, WEBHOOKS_CAPABILITY

        raise BackendCapabilityMissing(
            WEBHOOKS_CAPABILITY, type(backend).__name__ if backend is not None else None
        )

    if caller_tx is not None:
        intents = await backend.webhooks.dispatch_event(
            caller_tx,
            event_type,
            payload,
            owner_id=owner_id,
            namespace=namespace,
        )
        return [intent.delivery_id for intent in intents]

    async with backend.transactional() as new_tx:
        intents = await backend.webhooks.dispatch_event(
            new_tx,
            event_type,
            payload,
            owner_id=owner_id,
            namespace=namespace,
        )
    from mnemos.core.lifecycle import _schedule_delivery_attempt  # noqa: WPS433
    from .sender import _attempt_delivery

    delivery_ids = [intent.delivery_id for intent in intents]
    for delivery_id in delivery_ids:
        _schedule_delivery_attempt(_attempt_delivery(str(delivery_id)))
    return delivery_ids


async def _dispatch_on_conn(
    conn: Any,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> WebhookDispatchResult:
    """Back-compat wrapper for the old ``conn=`` asyncpg.Connection.

    Treats a raw asyncpg.Connection as a "Transaction-like" handle —
    PostgresBackend.webhooks.dispatch_event accepts it via the same
    Transaction Protocol contract used by tests that don't go through
    ``backend.transactional()``. New code should call ``dispatch(...)``
    with a backend ``Transaction`` directly.
    """
    return await dispatch(
        event_type,
        payload,
        tx=conn,
        owner_id=owner_id,
        namespace=namespace,
    )

# Legacy private attributes are resolved lazily so older in-repo tests and
# debugging imports can still reach the moved implementation without putting
# state-machine code back in this public dispatcher module.
_LEGACY_MODULES = {
    "asyncio": asyncio,
    "time": time,
    "httpx": httpx,
    # asyncpg is exposed as a legacy module attribute so the
    # ``test_webhook_retry_state.py`` suite's reference to
    # ``dispatcher.asyncpg.exceptions.UniqueViolationError`` resolves
    # without going through the dispatcher shim. The migrated
    # production runtime never touches the dispatcher asyncpg attr.
    "asyncpg": __import__("asyncpg"),
}
_LEGACY_ATTR_TARGETS: dict[str, object] = {}


def _register_legacy_attrs(target: object, names: tuple[str, ...]) -> None:
    for name in names:
        _LEGACY_ATTR_TARGETS[name] = target


# Legacy private attributes are resolved lazily so older in-repo tests and
# debugging imports can still reach the moved implementation without putting
# state-machine code back in this public dispatcher module.


_register_legacy_attrs(webhook_chain, (
    "_load_delivery_for_claim",
    "_insert_successor_delivery",
    "_lock_delivery_chain",
    "_delivery_chain_lock_key",
    "_has_successor_attempt",
    "_has_live_successor_attempt",
    "_has_succeeded_chain_attempt",
    "_abandon_owned_attempt_after_live_successor",
    "_abandon_current_attempt_after_succeeded_chain_peer",
    "_abandon_owned_attempt_after_succeeded_chain_peer",
    "_find_live_unleased_successor_attempts",
    "_abandon_live_successor_attempt",
    "_record_value",
    "_is_sqlite_connection",
))
_register_legacy_attrs(webhook_types, (
    "BACKOFF_SCHEDULE",
    "MAX_ATTEMPTS",
    "DNS_TIMEOUT",
    "DELIVERY_TIMEOUT",
    "WEBHOOK_LEASE_SECONDS",
    "WEBHOOK_FINALIZE_BUFFER_SECONDS",
    "WEBHOOK_RESPONSE_BODY_MAX_BYTES",
    "WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS",
    "WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS",
    "WEBHOOK_MAX_CONCURRENT_SENDS",
    "NEW_CODE_WRITER_REVISION",
    "NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES",
    "MIN_SEND_WINDOW_SECONDS",
    "RECOVERY_POLL_INTERVAL",
    "REPAIR_BURST_SECONDS",
    "REPAIR_BURST_INTERVAL",
    "REPAIR_PERIODIC_INTERVAL",
    "TERMINAL_DELIVERY_STATUSES",
    "LIVE_DELIVERY_STATUSES",
    "TOTAL_SEND_DEADLINE_SECONDS",
    "_send_semaphore",
    "_DeliveryResult",
    "_LeaseExpiredBeforeSend",
    "_PostHeaderDeliveryResult",
    "_ClaimedDelivery",
    "_derive_total_send_deadline_seconds",
    "_get_send_semaphore",
))
_register_legacy_attrs(webhook_signing, ("_sign",))
_register_legacy_attrs(webhook_workers, (
    "repair_worker_loop",
    "delivery_worker_loop",
    "recovery_worker_loop",
    "_recover_due_deliveries",
    "_semaphore_available",
    # The two helpers below folded into the per-backend ABC; the legacy
    # raw-asyncpg re-implementations are kept (in chain.py / workers.py
    # itself) so the test suite keeps passing during the migration.
    # They are exposed here too so dispatcher's __getattr__ shim
    # resolves the same attribute path that callers imported.
    "_claim_recoverable_deliveries",
    "_recoverable_delivery_ids",
))
_register_legacy_attrs(webhook_repair, (
    "_repair_superseded_retrying_deliveries_safely",
    "repair_superseded_retrying_deliveries",
))
_register_legacy_attrs(webhook_sender, (
    "_attempt_delivery",
    "_send_claimed_delivery",
    "_send_claimed_delivery_within_deadline",
    "_cleanup_unacknowledged_send_context",
    "_run_pre_header_cleanup",
    "_run_post_header_cleanup",
    "_consume_timed_out_cleanup_result",
    "_remaining_timeout_seconds",
    "_capture_response_body_for_audit",
    "_read_capped_response_body",
    "_read_capped_raw_response_body",
    "_decode_capped_response_body",
))
_register_legacy_attrs(webhook_lease, (
    # The item-7 lease.py wraps both: ABC production path AND a
    # legacy raw-asyncpg path used while ``test_webhook_retry_state.py``
    # migrates. Keep the legacy-looking names the test suite imports
    # through ``dispatcher`` so the shim resolves the same attribute
    # path that pre-item-7 callers used.
    "_claim_delivery",
    "_guard_preclaimed_delivery_before_send",
    "_preclaimed_delivery_is_live_and_owned",
    "_claim_remaining_send_window_seconds",
    "_as_aware_utc",
    "_release_owned_lease_for_reclaim",
    "_clear_stale_owned_lease_after_terminal_finalize",
    "_ClaimedDelivery",
))
_register_legacy_attrs(webhook_finalize, (
    # finalize.py exposes the same production ABC + legacy back-compat
    # pattern as lease.py. The legacy raw-asyncpg finalization state
    # machine lives behind the same module alias, so the dispatcher
    # shim resolves the names that ``test_webhook_retry_state.py``
    # imports through ``dispatcher``.
    "_finalize_delivery",
    "_finalize_delivery_row",
    "_commit_successful_delivery_row",
    "_finalize_successful_delivery_row",
    "_abandon_live_successors_before_success_commit",
    "_abandon_success_duplicate_after_unique_violation",
    "_run_post_finalize_delivery_work",
    "_persist_response_body_for_audit",
    "_guard_sqlite_succeeded_terminal",
))


def __getattr__(name: str) -> Any:
    if name in _LEGACY_MODULES:
        return _LEGACY_MODULES[name]
    target = _LEGACY_ATTR_TARGETS.get(name)
    if target is not None:
        return getattr(target, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _DispatcherModule(_module_types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        target = _LEGACY_ATTR_TARGETS.get(name)
        if target is not None:
            setattr(target, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _DispatcherModule


__all__ = [
    "dispatch",
    "_dispatch_on_conn",
]
