"""M7 — one config.toml reader, and dead keys that say so.

Two defects motivated these tests:

* ``mnemos.core.lifecycle._load_config`` kept its own candidate list
  (``<repo>/config.toml`` and ``/etc/mnemos/config.toml``), so a deployment
  pointed at a file with ``MNEMOS_CONFIG_PATH`` read *no* config at all and
  ``[multiuser] rls_enabled`` silently stayed at its default.
* ``warn_unknown_toml_keys`` validated keys against ``_TOML_SECTION_MODELS``,
  which used to be populated inside ``_build_settings`` — i.e. *after* the
  warn call at the top of that same function, so the per-key check was a
  no-op and only whole unknown tables were reported.
"""

from __future__ import annotations

import pytest

from mnemos.core import config


def _write(tmp_path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_lifecycle_load_config_honours_mnemos_config_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The topology reader must see the file the settings loader sees."""
    from mnemos.core import lifecycle

    path = _write(tmp_path, "[multiuser]\nrls_enabled = true\n")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(path))
    config.reload_settings()

    assert lifecycle._load_config().get("multiuser", {}).get("rls_enabled") is True


def test_settings_loader_and_lifecycle_agree_on_the_file(monkeypatch, tmp_path) -> None:
    from mnemos.core import lifecycle

    path = _write(tmp_path, "[server]\nport = 5999\n")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(path))
    config.reload_settings()

    assert config.loaded_toml_path() == path
    assert lifecycle._load_config() == config.load_toml_config()


def test_unknown_sections_and_keys_are_reported_before_settings_are_built(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Regression: the per-key check ran before its model map existed."""
    path = _write(
        tmp_path,
        "[hooks]\npost = 'x'\n[memory]\nbackend = 'y'\n[server]\nport = 5002\nnot_a_key = 1\n",
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(path))
    config.reload_settings()

    unknown = config.warn_unknown_toml_keys(config.load_toml_config(), path)

    assert "hooks" in unknown
    assert "memory" in unknown
    assert "server.not_a_key" in unknown
    # A key a settings model does consume is never reported.
    assert "server.port" not in unknown


def test_known_keys_of_fixed_models_are_not_reported(monkeypatch, tmp_path) -> None:
    path = _write(tmp_path, "[server]\nport = 5002\n[multiuser]\nrls_enabled = true\n")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(path))
    config.reload_settings()

    assert config.warn_unknown_toml_keys(config.load_toml_config(), path) == []


def test_free_form_provider_tables_are_not_reported(monkeypatch, tmp_path) -> None:
    """``[graeae.providers.<id>]`` — the child key IS a provider id."""
    path = _write(tmp_path, "[graeae.providers.perplexity]\napi_key = 'k'\n")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(path))
    config.reload_settings()

    assert config.warn_unknown_toml_keys(config.load_toml_config(), path) == []
