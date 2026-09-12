"""Transactional webhook outbox inserts (item 7).

After item 7, the per-backend ``WebhookRepository.dispatch_event``
owns the durable outbox insert (row-per-attempt via the underlying
ABC transaction). This module keeps the public ``_dispatch_on_conn``
shape so legacy callers keep importing it during the rollout, but
internally it just opens a backend ``transactional()`` block and routes
to ``dispatch_event``.

The post-commit visibility poll + NATS-nudge fallback that used to live
here moved into the dispatcher layer's no-tx path: when
``dispatch(...)`` is called without ``tx=``, it commits the
``dispatch_event`` insert, then publishes best-effort NATS nudges AFTER
the backend transaction commits. The caller can opt into the
transactional shape by passing the open backend ``Transaction`` via
``tx=`` (preferred, post item-7) or the legacy ``conn=`` alias.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from mnemos.core import lifecycle as _lc  # noqa: WPS433

logger = logging.getLogger(__name__)


async def _dispatch_on_conn(
    conn: Any,
    event_type: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    """Back-compat wrapper: treats a raw asyncpg.Connection as a
    transaction handle and routes through ``backend.webhooks.dispatch_event``.

    New code should call ``mnemos.webhooks.dispatcher.dispatch(...)``
    with the open backend ``Transaction`` (or ``conn=``) directly so
    the same call site also benefits from the post-commit NATS nudge.
    """
    backend = _lc._persistence_backend
    if backend is None:
        from mnemos.persistence.base import BackendCapabilityMissing, WEBHOOKS_CAPABILITY

        raise BackendCapabilityMissing(WEBHOOKS_CAPABILITY, None)
    intents = await backend.webhooks.dispatch_event(
        conn,
        event_type,
        payload,
        owner_id=owner_id,
        namespace=namespace,
    )
    return [intent.delivery_id for intent in intents]


# --- Stubs removed during the item-7 migration ------------------------------------
# These helpers used to handle subscription matching + payload
# serialization + NATS notification + post-commit visibility polling.
# All of that is now provided by ``dispatch_event`` (insert) and the
# dispatcher layer (NATS nudge, post-commit semantics). Stubs left so
# callers and tests that import them see a real ``AttributeError``-
# style ``NotImplementedError``.


async def _matching_subscriptions(
    _conn: Any,
    _event_type: str,
    _owner_id: Optional[str],
    _namespace: Optional[str],
) -> Any:
    raise NotImplementedError(
        "outbox._matching_subscriptions was removed in item 7; "
        "subscription matching is owned by backend.webhooks.dispatch_event"
    )


async def _publish_delivery_nats_notifications(
    *_args: Any,
    **_kwargs: Any,
) -> None:
    raise NotImplementedError(
        "outbox._publish_delivery_nats_notifications was removed in item 7; "
        "the dispatcher layer schedules NATS nudges after dispatch_event returns"
    )


async def _visible_delivery_ids(_delivery_ids: list[str]) -> set[str]:
    raise NotImplementedError(
        "outbox._visible_delivery_ids was removed in item 7; "
        "the dispatcher layer publishes NATS nudges after dispatch_event commits"
    )


async def _publish_delivery_nats_notifications_after_commit(
    *_args: Any,
    **_kwargs: Any,
) -> None:
    raise NotImplementedError(
        "outbox._publish_delivery_nats_notifications_after_commit was removed in item 7; "
        "NATS nudges are scheduled by the dispatcher layer after commit"
    )


__all__ = ("_dispatch_on_conn",)
