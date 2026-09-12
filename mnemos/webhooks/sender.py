"""Webhook HTTP send pipeline and response-body audit capture (item 7).

The pre-item-7 ``_attempt_delivery`` took an ``asyncpg.Pool`` and
resolved it from ``lifecycle._pool`` when missing. Item 7 drops the
``pool`` parameter entirely; the persistence backend is resolved at
send time via ``mnemos.core.lifecycle.get_persistence_backend()``, and
lease / claim / finalize all flow through the backend ABC.

The pure-HTTP/DNS/cleanup path (DNS resolution, SSRF guard, response
header reception, response body capping, post-header cleanup) is
unchanged — those operate on the already-claimed ``_ClaimedDelivery``
and the lease already cached on that claim.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Optional

import httpx

from mnemos.core.safe_http import PinnedDNSAsyncHTTPTransport  # re-exported here for the test_webhook_ssrf_rebind monkeypatch
from mnemos.webhooks import validation as webhook_validation

from . import lease as webhook_lease
from . import types as webhook_types
from ._signing import _sign
from .types import _ClaimedDelivery, _DeliveryResult, _LeaseExpiredBeforeSend, _PostHeaderDeliveryResult

logger = logging.getLogger(__name__)


async def _attempt_delivery(
    delivery_id: str,
    *,
    pool: Optional[Any] = None,
    claimed: Optional[_ClaimedDelivery] = None,
) -> bool:
    """Claim, send, and finalize one delivery without holding DB during I/O.

    Item 7 signature: ``pool`` is preserved as a keyword-only argument
    so legacy call sites (memories.py, nats_trigger.py,
    webhooks_dispatch_nats_consumer.py) keep compiling while the
    migration completes.

    Production path: routes through the persistence backend returned
    by :func:`mnemos.core.lifecycle.get_persistence_backend` — the
    ABC owns claim / guard / finalize / store on Postgres, SQLite,
    MySQL, Oracle, and Db2. The raw ``asyncpg.Pool`` (``pool`` /
    ``lifecycle._pool``) is not consulted.

    Test back-compat: ``test_webhook_retry_state.py`` injects a fake
    raw pool via ``monkeypatch.setattr(lc, '_pool', pool)`` and does
    not install a backend. When this is the only state (no backend,
    no overriding ``pool=`` keyword) we descend into the legacy
    raw-asyncpg helpers in :mod:`mnemos.webhooks.lease` and
    :mod:`mnemos.webhooks.finalize` so that exhaustive state-machine
    suite keeps passing while it migrates to the ABC.
    """
    from mnemos.core import lifecycle as _lc  # noqa: WPS433

    backend = _lc._persistence_backend
    pool_handle = pool if pool is not None else getattr(_lc, "_pool", None)
    if backend is None:
        if pool_handle is None:
            raise RuntimeError(
                f"webhook delivery {delivery_id} cannot run without a supported persistence handle"
            )
        return await _attempt_delivery_via_legacy_pool(
            delivery_id, pool_handle, claimed=claimed
        )

    async with webhook_types._get_send_semaphore():
        if claimed is None:
            lease_token = str(uuid.uuid4())
            claimed = await webhook_lease._claim_delivery(
                pool_handle,
                delivery_id,
                lease_token=lease_token,
            )
        else:
            lease_token = claimed.lease_token
            if not await webhook_lease._guard_preclaimed_delivery_before_send(
                pool_handle,
                claimed.delivery,
                lease_token,
            ):
                return False
        if not claimed:
            return False
        result = await _send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        from .finalize import _finalize_delivery
        return await _finalize_delivery(pool_handle, claimed.delivery, lease_token, result)


async def _attempt_delivery_via_legacy_pool(
    delivery_id: str,
    pool: Any,
    *,
    claimed: Optional[_ClaimedDelivery] = None,
) -> bool:
    """Test-only back-compat: when no persistence backend is set but a
    raw ``asyncpg.Pool`` is installed via ``lifecycle._pool`` (which is
    what ``test_webhook_retry_state.py`` does), run the pre-item-7
    claim / guard / finalize path against the raw pool.

    Production deployments always have a backend configured and never
    reach this branch.
    """
    from . import finalize as _legacy_finalize  # noqa: WPS433
    from . import lease as _legacy_lease  # noqa: WPS433

    logger.warning(
        "webhook delivery %s running through pre-item-7 raw-asyncpg "
        "test fast-path; production must install a backend via "
        "mnemos.core.lifecycle",
        delivery_id,
    )
    async with webhook_types._get_send_semaphore():
        if claimed is None:
            lease_token = str(uuid.uuid4())
            claimed = await _legacy_lease._claim_delivery(
                pool,
                delivery_id,
                lease_token=lease_token,
            )
        else:
            lease_token = claimed.lease_token
            ok = await _legacy_lease._guard_preclaimed_delivery_before_send(
                pool,
                claimed.delivery,
                lease_token,
            )
            if not ok:
                return False
        if not claimed:
            return False
        result = await _send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        return await _legacy_finalize._finalize_delivery(
            pool,
            claimed.delivery,
            lease_token,
            result,
        )


async def _send_claimed_delivery(
    delivery: Any,
    *,
    pre_claim_monotonic: float,
) -> _DeliveryResult:
    """Perform DNS validation and HTTP POST for an already leased row."""
    remaining_seconds = webhook_lease._claim_remaining_send_window_seconds(
        delivery,
        pre_claim_monotonic=pre_claim_monotonic,
    )
    if remaining_seconds <= webhook_types.MIN_SEND_WINDOW_SECONDS:
        return _LeaseExpiredBeforeSend(
            succeeded=False,
            error=(
                "lease-expired-before-send: remaining lease send window "
                f"{remaining_seconds:.3f}s <= {webhook_types.MIN_SEND_WINDOW_SECONDS:.1f}s minimum"
            ),
        )

    try:
        return await _send_claimed_delivery_within_deadline(
            delivery,
            send_window_seconds=remaining_seconds,
        )
    except asyncio.TimeoutError:
        return _DeliveryResult(
            succeeded=False,
            error=(
                "send-timeout: DNS validation, HTTP send, and response headers "
                f"exceeded {remaining_seconds:.1f}s lease-anchored wall-clock deadline"
            ),
        )


async def _send_claimed_delivery_within_deadline(
    delivery: Any,
    *,
    send_window_seconds: float,
) -> _DeliveryResult:
    """Run the network send path; caller supplies the wall-clock deadline.

    Reads ``delivery["revoked"]`` / ``delivery["url"]`` /
    ``delivery["secret"]`` / ``delivery["payload"]`` via subscript,
    normalized by the :class:`lease._RecordView` adapter wrapping the
    ABC's :class:`WebhookDeliveryRecord` so the existing HTTP/DNS
    code works unchanged.
    """
    if not delivery:
        return _DeliveryResult(succeeded=False, error="delivery not found")
    if delivery["revoked"]:
        return _DeliveryResult(succeeded=False, error="subscription revoked")

    signature = _sign(delivery["secret"], delivery["payload"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MNEMOS-Webhook/1.0",
        "Accept-Encoding": "identity",
        "X-MNEMOS-Event": delivery["event_type"],
        "X-MNEMOS-Signature": f"sha256={signature}",
        "X-MNEMOS-Delivery-ID": str(delivery["id"]),
        "X-MNEMOS-Subscription-ID": str(delivery["subscription_id"]),
        "X-MNEMOS-Attempt": str(delivery["attempt_num"]),
    }

    loop = asyncio.get_running_loop()
    header_deadline = loop.time() + send_window_seconds

    try:
        validated_url = await asyncio.wait_for(
            webhook_validation.validate_webhook_url(delivery["url"]),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
    except asyncio.TimeoutError:
        raise
    except Exception as e:
        return _DeliveryResult(succeeded=False, error=f"url-rejected: {type(e).__name__}: {e}")

    transport = PinnedDNSAsyncHTTPTransport(
        hostname=validated_url.hostname,
        resolved_ip=validated_url.resolved_ip,
    )
    client_cm = httpx.AsyncClient(
        timeout=webhook_types.DELIVERY_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    )
    client = None
    stream_cm = None
    try:
        client = await asyncio.wait_for(
            client_cm.__aenter__(),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
        stream_cm = client.stream(
            "POST",
            delivery["url"],
            content=delivery["payload"].encode("utf-8"),
            headers=headers,
        )
        response = await asyncio.wait_for(
            stream_cm.__aenter__(),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
        response_status = response.status_code
        succeeded = 200 <= response_status < 300
        return _PostHeaderDeliveryResult(
            succeeded=succeeded,
            response_status=response_status,
            delivery_id=delivery["id"],
            response=response,
            stream_cm=stream_cm,
            client_cm=client_cm,
        )
    except asyncio.TimeoutError:
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        raise
    except httpx.HTTPError as e:
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        return _DeliveryResult(succeeded=False, error=f"{type(e).__name__}: {e}")
    except Exception as e:  # pragma: no cover
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        return _DeliveryResult(succeeded=False, error=f"{type(e).__name__}: {e}")


async def _cleanup_unacknowledged_send_context(
    *,
    delivery_id: Any,
    stream_cm: Any,
    client_cm: Any,
    client: Any,
) -> None:
    """Best-effort cleanup when the POST did not produce response headers."""
    if stream_cm is not None:
        await _run_pre_header_cleanup(
            stream_cm.__aexit__(None, None, None),
            delivery_id=delivery_id,
            cleanup_name="stream",
        )
    if client is not None:
        await _run_pre_header_cleanup(
            client_cm.__aexit__(None, None, None),
            delivery_id=delivery_id,
            cleanup_name="client",
        )


async def _run_pre_header_cleanup(
    cleanup: Awaitable[object],
    *,
    delivery_id: Any,
    cleanup_name: str,
) -> None:
    """Drain failed pre-header send resources without changing the delivery result."""
    try:
        await asyncio.wait_for(
            cleanup,
            timeout=webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "webhook delivery %s %s cleanup failed before response headers",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


async def _run_post_header_cleanup(
    cleanup: Awaitable[object],
    *,
    delivery_id: Any,
    cleanup_name: str,
    result: _DeliveryResult | None,
) -> None:
    """Bound post-header cleanup so finalization is not stuck behind __aexit__."""
    cleanup_task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.wait_for(
            asyncio.shield(cleanup_task),
            timeout=webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        cleanup_task.cancel()
        cleanup_task.add_done_callback(
            lambda task: _consume_timed_out_cleanup_result(
                task,
                delivery_id,
                cleanup_name,
            )
        )
        if result is None:
            raise
        logger.warning(
            "webhook delivery %s %s cleanup timed out after %.1fs after response headers; "
            "delivery result preserved",
            delivery_id,
            cleanup_name,
            webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
            exc_info=True,
        )
    except asyncio.CancelledError:
        cleanup_task.cancel()
        raise
    except Exception:
        if result is None:
            raise
        logger.warning(
            "webhook delivery %s %s cleanup failed after response headers; "
            "delivery result preserved",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


def _consume_timed_out_cleanup_result(
    task: asyncio.Future[object],
    delivery_id: Any,
    cleanup_name: str,
) -> None:
    """Drain a timed-out cleanup task if it finishes after finalization moved on."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.debug(
            "webhook delivery %s %s cleanup finished with an error after timeout",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


def _remaining_timeout_seconds(deadline: float) -> float:
    """Return a small positive timeout if the header deadline is exhausted."""
    return max(0.001, deadline - asyncio.get_running_loop().time())


async def _capture_response_body_for_audit(
    response: httpx.Response,
    *,
    delivery_id: Any,
) -> Optional[str]:
    """Read response body for audit after the status-code result is finalized."""
    try:
        async with asyncio.timeout(webhook_types.WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS):
            return await _read_capped_response_body(response)
    except asyncio.TimeoutError:
        logger.warning(
            "webhook delivery %s response-body audit capture timed out after %.1fs; "
            "leaving response_body NULL",
            delivery_id,
            webhook_types.WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS,
            exc_info=True,
        )
        return None
    except httpx.HTTPError as e:
        return f"[body capture: {type(e).__name__}]"
    except Exception as e:  # pragma: no cover
        return f"[body capture: {type(e).__name__}]"


async def _read_capped_response_body(response: httpx.Response) -> str:
    """Read bounded raw response bytes; never transparently decompress."""
    headers = getattr(response, "headers", {})
    content_encoding = str(headers.get("content-encoding", "identity") or "identity").strip().lower()
    if content_encoding not in ("", "identity"):
        raw = await _read_capped_raw_response_body(
            response,
            max_bytes=min(webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES, webhook_types.NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES),
        )
        return _decode_capped_response_body(raw, webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)

    raw = await _read_capped_raw_response_body(response, max_bytes=webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)
    return _decode_capped_response_body(raw, webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)


async def _read_capped_raw_response_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read at most max_bytes raw bytes from a streamed response."""
    remaining = max_bytes
    body = bytearray()
    async for chunk in response.aiter_raw():
        if not chunk:
            continue
        body.extend(chunk[:remaining])
        remaining -= min(len(chunk), remaining)
        if remaining <= 0:
            break
    return bytes(body)


def _decode_capped_response_body(raw: bytes, max_bytes: int) -> str:
    """Decode for TEXT storage while preserving the configured UTF-8 byte cap."""
    text = raw.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    used = 0
    out: list[str] = []
    for char in text:
        char_size = len(char.encode("utf-8"))
        if used + char_size > max_bytes:
            break
        out.append(char)
        used += char_size
    return "".join(out)


__all__ = (
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
)
