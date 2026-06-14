"""Declarative runtime-service composition for deployment profiles.

Deployment profiles (server/edge/dev) choose storage/auth/workers defaults in
``mnemos.core.config``. This module describes the *service composition* layer:
which optional background workers, consumers, and NATS fanouts should run for a
selected deployment shape.

For backward compatibility the profile manifest is only applied when operators
or the installer opt in with ``MNEMOS_PROFILE_SERVICES_ENABLED=true``. Without
that opt-in, legacy runtime defaults are preserved and individual service env
flags keep their historical meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SERVICE_NAMES: tuple[str, ...] = (
    "distillation_worker",
    "deletion_request_worker",
    "persephone_archival_worker",
    "federation_sync_worker",
    "federation_nats_consumers",
    "webhook_nats_trigger",
    "nats_webhooks",
    "nats_federation",
    "pantheon",
    "pantheon_routing_audit_consumer",
)

# Historical behavior when the profile-services manifest is not enabled. These
# values intentionally mirror pre-manifest startup: background workers are
# registered and let their own internal gates no-op; explicit NATS/PANTHEON flags
# remain default-off.
LEGACY_SERVICE_DEFAULTS: dict[str, bool] = {
    "distillation_worker": True,
    "deletion_request_worker": True,
    "persephone_archival_worker": True,
    "federation_sync_worker": True,
    "federation_nats_consumers": True,
    "webhook_nats_trigger": True,
    "nats_webhooks": False,
    "nats_federation": False,
    "pantheon": False,
    "pantheon_routing_audit_consumer": False,
}

# Opt-in manifest defaults. PANTHEON is deliberately default-off even for
# server installs; operators enable it with --with pantheon (or an explicit env
# flag) because it is a niche model-proxy surface rather than required server
# substrate.
PROFILE_SERVICE_MANIFEST: dict[str, dict[str, bool]] = {
    "edge": {
        "distillation_worker": False,
        "deletion_request_worker": True,
        "persephone_archival_worker": False,
        "federation_sync_worker": False,
        "federation_nats_consumers": False,
        "webhook_nats_trigger": False,
        "nats_webhooks": False,
        "nats_federation": False,
        "pantheon": False,
        "pantheon_routing_audit_consumer": False,
    },
    "server": {
        "distillation_worker": True,
        "deletion_request_worker": True,
        "persephone_archival_worker": True,
        "federation_sync_worker": True,
        "federation_nats_consumers": True,
        "webhook_nats_trigger": True,
        "nats_webhooks": True,
        "nats_federation": True,
        "pantheon": False,
        "pantheon_routing_audit_consumer": False,
    },
    "dev": {
        "distillation_worker": True,
        "deletion_request_worker": True,
        "persephone_archival_worker": False,
        "federation_sync_worker": False,
        "federation_nats_consumers": False,
        "webhook_nats_trigger": False,
        "nats_webhooks": False,
        "nats_federation": False,
        "pantheon": False,
        "pantheon_routing_audit_consumer": False,
    },
}

# Component selections augment the profile manifest. They do not force endpoint
# configuration such as MNEMOS_NATS_URL or federation peers; they only turn on the
# corresponding runtime services when the substrate is configured.
COMPONENT_SERVICE_ENABLES: dict[str, dict[str, bool]] = {
    "persephone": {"persephone_archival_worker": True},
    "morpheus": {"distillation_worker": True},
    "apollo": {"distillation_worker": True},
    "artemis": {"distillation_worker": True},
    "ml": {"distillation_worker": True, "persephone_archival_worker": True},
    "nats": {
        "federation_nats_consumers": True,
        "webhook_nats_trigger": True,
        "nats_webhooks": True,
        "nats_federation": True,
    },
    "server": {
        "distillation_worker": True,
        "persephone_archival_worker": True,
        "federation_sync_worker": True,
        "federation_nats_consumers": True,
        "webhook_nats_trigger": True,
        "nats_webhooks": True,
        "nats_federation": True,
    },
    "pantheon": {"pantheon": True, "pantheon_routing_audit_consumer": True},
    "full": {
        "distillation_worker": True,
        "persephone_archival_worker": True,
        "federation_sync_worker": True,
        "federation_nats_consumers": True,
        "webhook_nats_trigger": True,
        "nats_webhooks": True,
        "nats_federation": True,
        "pantheon": True,
        "pantheon_routing_audit_consumer": True,
    },
}

# Env flag names intentionally reuse existing operator knobs where they already
# exist. New worker-specific flags are additive and override the manifest only.
SERVICE_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "distillation_worker": ("MNEMOS_DISTILLATION_WORKER_ENABLED", "MNEMOS_WORKER_ENABLED"),
    "deletion_request_worker": ("MNEMOS_DELETION_REQUEST_WORKER_ENABLED", "MNEMOS_WORKER_ENABLED"),
    "persephone_archival_worker": ("MNEMOS_PERSEPHONE_ENABLED",),
    "federation_sync_worker": ("MNEMOS_FEDERATION_ENABLED",),
    "federation_nats_consumers": ("MNEMOS_NATS_FEDERATION_ENABLED",),
    "webhook_nats_trigger": ("MNEMOS_NATS_WEBHOOKS_ENABLED",),
    "nats_webhooks": ("MNEMOS_NATS_WEBHOOKS_ENABLED",),
    "nats_federation": ("MNEMOS_NATS_FEDERATION_ENABLED",),
    "pantheon": ("MNEMOS_PANTHEON_ENABLED",),
    "pantheon_routing_audit_consumer": ("MNEMOS_NATS_AUDIT_CONSUMER_ENABLED",),
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_PROFILE_ALIASES = {"personal": "edge"}


@dataclass(frozen=True)
class ServiceResolution:
    profile: str
    managed: bool
    selected_components: tuple[str, ...]
    services: dict[str, bool]

    def enabled(self, service_name: str) -> bool:
        return bool(self.services.get(service_name, False))


def normalize_service_profile(raw_profile: str | None) -> str:
    profile = (raw_profile or "personal").strip().lower()
    return _PROFILE_ALIASES.get(profile, profile)


def parse_component_selection(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for item in raw.split(","):
        component = item.strip().lower()
        if component:
            seen.setdefault(component, None)
    return tuple(seen)


def parse_bool_flag(raw: Any) -> bool | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def resolve_profile_services(
    *,
    profile: str | None,
    managed: bool = False,
    selected_components: str | tuple[str, ...] = (),
    env: Mapping[str, Any] | None = None,
) -> ServiceResolution:
    """Resolve the effective runtime service set.

    ``managed=False`` preserves legacy startup defaults and only applies explicit
    env overrides. ``managed=True`` starts with the profile manifest, augments it
    from selected components/bundles, then applies env overrides last.
    """
    env = env or {}
    normalized_profile = normalize_service_profile(profile)
    if isinstance(selected_components, str):
        selected = parse_component_selection(selected_components)
    else:
        selected = tuple(dict.fromkeys(component.strip().lower() for component in selected_components if component))

    if managed:
        base = PROFILE_SERVICE_MANIFEST.get(normalized_profile, PROFILE_SERVICE_MANIFEST["edge"])
        services = {name: bool(base.get(name, False)) for name in SERVICE_NAMES}
        for component in selected:
            for service_name, enabled in COMPONENT_SERVICE_ENABLES.get(component, {}).items():
                services[service_name] = enabled
    else:
        services = dict(LEGACY_SERVICE_DEFAULTS)

    for service_name, env_names in SERVICE_ENV_OVERRIDES.items():
        for env_name in env_names:
            if env_name in env:
                parsed = parse_bool_flag(env.get(env_name))
                if parsed is not None:
                    services[service_name] = parsed
                    break

    return ServiceResolution(
        profile=normalized_profile,
        managed=managed,
        selected_components=selected,
        services=services,
    )


def service_enabled(settings: Any, service_name: str) -> bool:
    """Convenience wrapper for callers that have a Settings object."""
    services = getattr(settings, "services", None)
    if services is not None and hasattr(services, "resolution"):
        return services.resolution.enabled(service_name)
    return LEGACY_SERVICE_DEFAULTS.get(service_name, False)
