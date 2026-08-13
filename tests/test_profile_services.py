from pathlib import Path

from mnemos.core.services import resolve_profile_services
from mnemos.installer import wizard
from mnemos.installer.wizard import (
    Config,
    apply_component_selection,
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
    assert "knemon" in spec
    assert "graeae" in spec
    assert "charon" in spec
    assert "morpheus" in spec
    assert "pantheon" in spec


def test_full_bundle_includes_all_split_subsystems():
    spec = pip_extra_spec(("full",))

    assert "pantheon" in spec
    assert "knemon" in spec
    assert "graeae" in spec
    assert "charon" in spec


def test_installer_service_flags_map_to_existing_env_names():
    flags = service_flags_for_selection("server", ("server",))
    env = env_flags_for_services(flags)

    assert env["MNEMOS_PROFILE_SERVICES_ENABLED"] is True
    assert env["MNEMOS_NATS_WEBHOOKS_ENABLED"] is True
    assert env["MNEMOS_NATS_FEDERATION_ENABLED"] is True
    assert env["MNEMOS_PANTHEON_ENABLED"] is False


def test_blank_wizard_component_selection_preserves_legacy_startup(monkeypatch):
    def fake_prompt(question: str, default: str = "", secret: bool = False) -> str:
        if question.startswith("Install component bundles/extras"):
            return ""
        return default

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_prompt_bool", lambda question, default=True: default)
    monkeypatch.setattr(wizard, "_prompt_int", lambda question, default, min_val=1, max_val=65535: default)
    monkeypatch.setattr(wizard, "check_port_free", lambda port: True)

    cfg = wizard.run_wizard(wizard.SystemInfo(), selected_profile="edge")

    assert cfg.selected_components == ()
    assert cfg.profile_services_enabled is False
    assert cfg.service_flags == {}


def test_blank_component_selection_still_installs_local_project():
    from mnemos.installer.__main__ import _project_install_spec

    assert _project_install_spec(Config(selected_components=())) == "."


def test_apply_component_selection_only_enables_managed_services_when_selected():
    cfg = Config(profile="server")

    apply_component_selection(cfg, ())
    assert cfg.selected_components == ()
    assert cfg.profile_services_enabled is False
    assert cfg.service_flags == {}

    apply_component_selection(cfg, ("server",))
    assert cfg.profile_services_enabled is True
    assert cfg.service_flags["nats_webhooks"] is True
    assert cfg.service_flags["pantheon"] is False


def test_launchd_env_skips_service_flags_when_profile_services_unmanaged(monkeypatch, tmp_path):
    from mnemos.installer import service

    cfg = Config(profile="edge", service_flags=service_flags_for_selection("edge", ()))
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)

    assert service.install_launchd(cfg, str(tmp_path)) is True

    env_content = (tmp_path / ".mnemos" / "mnemos.env").read_text()
    assert "MNEMOS_PROFILE_SERVICES_ENABLED" not in env_content
    assert "MNEMOS_NATS_WEBHOOKS_ENABLED" not in env_content
    assert "MNEMOS_DISTILLATION_WORKER_ENABLED" not in env_content


def test_install_sh_launches_packaged_installer_module():
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert "-m mnemos.installer" in script
    assert "-m installer" not in script
