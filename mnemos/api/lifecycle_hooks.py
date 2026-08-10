"""API-owned lifespan integrations for domain, webhook, and worker packages."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any

from mnemos.api.dependencies import configure_auth
from mnemos.core import lifecycle
from mnemos.core.extras import EXTERNAL_EXTRA_DISTS, is_extra_installed
from mnemos.core.services import service_enabled

logger = logging.getLogger(__name__)
_registered = False


async def _reload_provider_manifest(pool: Any) -> None:
    if not is_extra_installed("graeae"):
        return
    from mnemos.domain.graeae.engine import get_graeae_engine

    await get_graeae_engine().reload_from_registry(pool)


async def _close_graeae_engine() -> None:
    if not is_extra_installed("graeae"):
        return
    from mnemos.domain.graeae.engine import get_graeae_engine

    await get_graeae_engine().close()


async def _close_pantheon_http_client() -> None:
    from mnemos.core.config import get_settings

    if not service_enabled(get_settings(), "pantheon"):
        return
    if not is_extra_installed("pantheon"):
        return

    from mnemos.domain.pantheon.gateway import aclose_http_client, aclose_runtime

    await aclose_runtime()
    await aclose_http_client()


async def _run_distillation_worker(_pool: Any) -> None:
    """Supervise the distillation worker loop with bounded restart backoff."""
    from mnemos.core.config import get_settings

    if not service_enabled(get_settings(), "distillation_worker"):
        logger.info("Distillation worker disabled by profile service manifest")
        lifecycle._worker_status["distillation_worker"] = "disabled"
        return

    try:
        from mnemos.workers.distillation import MemoryDistillationWorker
    except ImportError as e:
        logger.warning(f"Distillation worker not available: {e}")
        lifecycle._worker_status["distillation_worker"] = "unavailable"
        return

    backoff = 1.0
    while True:
        worker = MemoryDistillationWorker(
            _pool,
            on_started=lambda: lifecycle._worker_status.__setitem__("distillation_worker", "healthy"),
            on_heartbeat=lambda: lifecycle._worker_status.__setitem__("last_heartbeat", time.time()),
        )
        try:
            lifecycle._worker_status["distillation_worker"] = "starting"
            await worker.start()
            lifecycle._worker_status["distillation_worker"] = "idle"
            return
        except asyncio.CancelledError:
            logger.info("Distillation worker cancelled (shutdown)")
            lifecycle._worker_status["distillation_worker"] = "idle"
            raise
        except Exception as e:
            lifecycle._worker_status["distillation_worker"] = "error"
            logger.exception(f"Distillation worker crashed: {e} - restarting in {backoff:.0f}s")
        finally:
            try:
                if getattr(worker, "db_pool", None) and getattr(worker, "_owns_pool", True):
                    await worker.db_pool.close()
            except Exception:
                pass
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            lifecycle._worker_status["distillation_worker"] = "idle"
            raise
        backoff = min(backoff * 2, 300.0)


def _webhook_repair_worker(pool: Any):
    from mnemos.webhooks import repair_worker_loop

    return repair_worker_loop(pool)


def _webhook_delivery_worker(pool: Any):
    from mnemos.webhooks import delivery_worker_loop

    return delivery_worker_loop(pool)


def _federation_sync_worker(pool: Any):
    from mnemos.core.config import get_settings

    if not service_enabled(get_settings(), "federation_sync_worker"):
        logger.info("federation sync worker disabled by profile service manifest")
        return None

    from mnemos.domain.federation import federation_worker_loop

    backend = lifecycle._persistence_backend
    if backend is None:
        raise RuntimeError("federation worker requires an initialized persistence backend")
    return federation_worker_loop(backend)


def _deletion_request_worker(pool: Any):
    from mnemos.core.config import get_settings

    if not service_enabled(get_settings(), "deletion_request_worker"):
        logger.info("deletion request worker disabled by profile service manifest")
        return None

    from mnemos.workers.deletion_request_worker import deletion_request_worker_loop

    return deletion_request_worker_loop(pool)


def _persephone_archival_worker(pool: Any):
    from mnemos.core.config import get_settings

    if not service_enabled(get_settings(), "persephone_archival_worker"):
        logger.info("PERSEPHONE archival worker disabled by profile service manifest")
        return None

    from mnemos.workers.persephone_archival_worker import persephone_archival_worker_loop

    return persephone_archival_worker_loop(pool)


async def _federation_nats_post_db_hook(pool: Any, settings: Any) -> None:
    """Launch one federation NATS consumer per configured peer.

    Optional and additive: HTTP federation polling remains active for
    backfill and safety regardless of NATS availability.

    Audit Finding 9 (handoff queue #3): warn at boot if peers are
    configured but ``MNEMOS_NODE_NAME`` is unset. Without an
    explicit name the source-node tag falls back to
    ``socket.gethostname()``, which is fine for a single host but
    can collide on identical container hostnames across a federation
    and cause loop-back filtering to mis-fire. Operators with peers
    really want a stable, unique node name.
    """
    if not service_enabled(settings, "federation_nats_consumers"):
        logger.info("federation nats consumers disabled by profile service manifest")
        return

    from mnemos.federation.nats_consumer import (
        configured_nats_peers,
        consumer_loop,
    )
    from mnemos.workers.federation_memory_nats_consumer import run_configured_consumers

    peers = list(configured_nats_peers(settings))
    if peers and not settings.nats.node_name.strip():
        logger.warning(
            "[NATS] %d federation peer(s) configured but MNEMOS_NODE_NAME is unset; "
            "falling back to hostname for source_node tagging. Set MNEMOS_NODE_NAME "
            "to a stable, deployment-unique value to avoid loop-back filter misses "
            "if peer hostnames collide.",
            len(peers),
        )

    queue_group = (settings.federation.nats_queue_group or "").strip()
    if queue_group:
        logger.info(
            "[NATS] federation queue group enabled: queue_group=%s "
            "(JetStream load-balances messages across replicas in this group)",
            queue_group,
        )

    for peer in peers:
        logger.info("Launching federation nats consumer for peer %s", peer.name)
        lifecycle.schedule_worker(consumer_loop(pool, peer, queue_group=queue_group))

    # Repository-level memory writes publish the v0.3 direct-upsert contract on
    # MNEMOS_FEDERATION, while route writes still emit the legacy MNEMOS_MEMORY
    # nudges above. Run both consumers until the legacy contract is retired.
    lifecycle.schedule_worker(run_configured_consumers(pool, settings=settings))


async def _webhook_nats_post_db_hook(pool: Any, settings: Any) -> None:
    """Launch the webhook NATS push trigger.

    Optional and additive: the polling recovery worker remains the
    durable fallback path regardless of NATS availability.
    """
    if not service_enabled(settings, "webhook_nats_trigger"):
        logger.info("webhook nats trigger disabled by profile service manifest")
        return
    if not (settings.nats.url or "").strip():
        logger.info("webhook nats trigger disabled (MNEMOS_NATS_URL unset)")
        return

    from mnemos.webhooks.nats_trigger import consumer_loop as webhook_nats_trigger_loop

    logger.info("Launching webhook nats trigger consumer")
    lifecycle.schedule_worker(webhook_nats_trigger_loop(pool, settings=settings))


async def _pantheon_routing_audit_post_db_hook(pool: Any, settings: Any) -> None:
    """Launch the optional PANTHEON routing audit NATS consumer."""
    if not service_enabled(settings, "pantheon_routing_audit_consumer"):
        return
    if not is_extra_installed("pantheon"):
        logger.info("PANTHEON routing audit consumer disabled; pantheon add-on is not installed")
        return

    module_path = "mnemos.workers.pantheon_routing_audit_consumer"
    dist_name = EXTERNAL_EXTRA_DISTS["pantheon"]
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        logger.warning(
            "PANTHEON routing audit NATS consumer unavailable; skipping consumer startup "
            "because %s could not be imported: %s. Reinstall or repair %s, or disable "
            "MNEMOS_NATS_AUDIT_CONSUMER_ENABLED.",
            module_path,
            exc,
            dist_name,
        )
        return
    if not hasattr(module, "consumer_loop"):
        logger.warning(
            "PANTHEON routing audit NATS consumer unavailable; skipping consumer startup "
            "because %s does not expose consumer_loop. Reinstall or repair %s, or disable "
            "MNEMOS_NATS_AUDIT_CONSUMER_ENABLED.",
            module_path,
            dist_name,
        )
        return
    consumer_loop = module.consumer_loop

    audit_handle = lifecycle._persistence_backend or pool
    logger.info("Launching PANTHEON routing audit NATS consumer")
    lifecycle.schedule_worker(consumer_loop(audit_handle, settings=settings))


def register_lifespan_hooks() -> None:
    """Register high-level integrations once per process."""
    global _registered
    if _registered:
        return
    from mnemos.mcp.tools._runtime import _close_rest_client
    from mnemos.mcp.tools._security import drain_pending_audit_tasks

    async def _drain_audit_tasks() -> None:
        """Round-3 residual #2 of #146 (#149): drain in-flight MCP
        audit persist tasks before the lifecycle pool closes."""
        drained = await drain_pending_audit_tasks(timeout=5.0)
        if drained:
            import logging as _logging

            _logging.getLogger("mnemos.mcp.audit").info(
                "drained %d pending mcp_audit_log persist task(s) on shutdown",
                drained,
            )

    lifecycle.register_auth_configurer(configure_auth)
    lifecycle.register_provider_manifest_reloader(_reload_provider_manifest)
    lifecycle.register_lifespan_cleanup_hook("mcp rest client", _close_rest_client)
    lifecycle.register_lifespan_cleanup_hook("graeae engine", _close_graeae_engine)
    lifecycle.register_lifespan_cleanup_hook("pantheon http client", _close_pantheon_http_client)
    lifecycle.register_lifespan_cleanup_hook("mcp audit drain", _drain_audit_tasks)
    lifecycle.register_lifespan_worker(
        "distillation_worker",
        _run_distillation_worker,
        honor_worker_enabled=True,
    )
    lifecycle.register_lifespan_worker(
        "deletion_request_worker",
        _deletion_request_worker,
        honor_worker_enabled=True,
    )
    lifecycle.register_lifespan_worker("persephone archival worker", _persephone_archival_worker)
    lifecycle.register_lifespan_worker("webhook retry repair worker", _webhook_repair_worker)
    lifecycle.register_lifespan_worker("webhook delivery recovery worker", _webhook_delivery_worker)
    lifecycle.register_lifespan_worker("federation sync worker", _federation_sync_worker)
    lifecycle.register_post_db_startup_hook("federation nats consumers", _federation_nats_post_db_hook)
    lifecycle.register_post_db_startup_hook("webhook nats trigger", _webhook_nats_post_db_hook)
    lifecycle.register_post_db_startup_hook(
        "PANTHEON routing audit NATS consumer", _pantheon_routing_audit_post_db_hook
    )
    _registered = True
