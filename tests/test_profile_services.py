from mnemos.core.services import resolve_profile_services
from mnemos.installer.wizard import (
    env_flags_for_services,
    normalize_component_selection,
    pip_extra_spec,
    service_flags_for_selection,
)


def test_legacy_resolution_preserves_existing_defaults():
    resolved = resolve_profile_services(profile="edge", managed=False, env={})

    assert resolved.enabled("distillation_worker") is True
    assert resolved.enabled("federation_sync_worker") is True
    assert resolved.enabled("webhook_nats_trigger") is True
    assert resolved.enabled("pantheon") is False
    assert resolved.enabled("nats_webhooks") is False


def test_managed_edge_disables_server_consumers():
    resolved = resolve_profile_services(profile="edge", managed=True, env={})

    assert resolved.enabled("distillation_worker") is False
    assert resolved.enabled("federation_sync_worker") is False
    assert resolved.enabled("federation_nats_consumers") is False
    assert resolved.enabled("webhook_nats_trigger") is False
    assert resolved.enabled("pantheon") is False


def test_managed_server_enables_nats_and_distillation_but_not_pantheon():
    resolved = resolve_profile_services(profile="server", managed=True, selected_components=("server",), env={})

    assert resolved.enabled("distillation_worker") is True
    assert resolved.enabled("persephone_archival_worker") is True
    assert resolved.enabled("federation_sync_worker") is True
    assert resolved.enabled("federation_nats_consumers") is True
    assert resolved.enabled("webhook_nats_trigger") is True
    assert resolved.enabled("nats_webhooks") is True
    assert resolved.enabled("nats_federation") is True
    assert resolved.enabled("pantheon") is False


def test_pantheon_is_explicit_opt_in_and_env_overrides_win():
    selected = resolve_profile_services(
        profile="server",
        managed=True,
        selected_components=("server", "pantheon"),
        env={},
    )
    assert selected.enabled("pantheon") is True
    assert selected.enabled("pantheon_routing_audit_consumer") is True

    overridden = resolve_profile_services(
        profile="server",
        managed=True,
        selected_components=("server", "pantheon"),
        env={"MNEMOS_PANTHEON_ENABLED": "false"},
    )
    assert overridden.enabled("pantheon") is False


def test_component_selection_normalization_and_pip_expansion():
    components = normalize_component_selection("server, compression, pantheon")

    assert components == ("server", "ml", "pantheon")
    spec = pip_extra_spec(components)
    assert spec.startswith(".[")
    assert "nats" in spec
    assert "persephone" in spec
    assert "morpheus" in spec
    assert "pantheon" in spec


def test_installer_service_flags_map_to_existing_env_names():
    flags = service_flags_for_selection("server", ("server",))
    env = env_flags_for_services(flags)

    assert env["MNEMOS_PROFILE_SERVICES_ENABLED"] is True
    assert env["MNEMOS_NATS_WEBHOOKS_ENABLED"] is True
    assert env["MNEMOS_NATS_FEDERATION_ENABLED"] is True
    assert env["MNEMOS_PANTHEON_ENABLED"] is False
