"""Characterisation tests for ``mnemos/domain/providers.py``.

``mnemos/domain/providers.py`` had 0 test references before this file. It is
the single neutral home for LLM provider API-key and non-secret
provider-metadata resolution shared across the MNEMOS namespace, so its
resolution order (environment variable first, then the Provider Registry
File) is a contract several callers lean on.

These tests PIN today's behaviour — normal paths, empty/None/boundary inputs,
every branch that is reachable with plain fixtures, and the failure modes —
rather than proposing a design. If a value changes intentionally, update the
pin together with the change.

Determinism: every test that touches key resolution replaces
``mnemos.domain.providers.get_settings`` with a stub, so the ambient
environment (a developer's exported ``OPENAI_API_KEY`` and friends) can
neither satisfy nor break an assertion, and no test reads the operator's real
``~/.config/mnemos/api_keys.json``. Registry files live in ``tmp_path``.

One suspected defect is pinned as ``xfail(strict=True)`` — see
``test_unusable_override_candidate_falls_through_to_the_usable_standard_file``
at the end of the file for the reasoning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.core.config import _ProviderSettings
from mnemos.domain import providers


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _Settings:
    """A stand-in for ``Settings`` with a controllable ``providers`` section.

    ``api_key_values`` stands in for the *fields* of ``_ProviderSettings``:
    it maps a canonical provider name to the value the settings object
    reports for that provider's environment variable. A name absent from the
    dict reads as "" ("nothing exported"), which keeps every assertion
    independent of the ambient environment.
    """

    def __init__(
        self,
        api_keys_file: Path,
        keys_path: Path | None = None,
        api_key_values: dict[str, str] | None = None,
    ) -> None:
        self.api_key_values = api_key_values if api_key_values is not None else {}
        self.providers = SimpleNamespace(
            keys_path=keys_path,
            api_keys_file=api_keys_file,
            api_key_for=self.api_key_for,
        )

    def api_key_for(self, provider: str) -> str:
        """Stand in for ``_ProviderSettings.api_key_for``.

        A plain name -> value table, not a copy of the real field map: each
        test seeds one *variable* through :func:`env_override`, which writes
        every provider name that shares it. That the real class agrees with
        this module's name list is pinned separately by
        ``test_every_provider_this_module_names_has_a_settings_field_beside_it``.
        """
        return self.api_key_values.get(provider, "")


@pytest.fixture(autouse=True)
def no_ambient_key_sources(monkeypatch, tmp_path):
    """Make ambient key sources irrelevant to every test in this file.

    Three things outside the test can otherwise decide the result: an
    exported ``*_API_KEY`` (read through the settings object), a
    ``$MNEMOS_KEYS_PATH`` / ``$API_KEYS_FILE`` pointing at a real registry
    file, and ``$HOME`` for ``~`` expansion. Remove all three so the
    determinism claim in the module docstring holds by construction, not by
    accident of every fixture happening to use an absolute tmp path.
    """
    for name in (
        "MNEMOS_KEYS_PATH",
        "API_KEYS_FILE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "PERPLEXITY_API_KEY",
        "TOGETHER_API_KEY",
        "NVIDIA_API_KEY",
        "EIH_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def env_override(monkeypatch, tmp_path, no_ambient_key_sources):
    """Install a settings stub whose env-derived keys are settable per test.

    ``env_override("nvidia", "nvapi")`` models ``export NVIDIA_API_KEY=...``:
    it seeds the provider names the settings object resolves ``NVIDIA_API_KEY``
    for (``nvidia`` and ``ngc``), so ``get_key`` is never handed a fabricated
    asymmetry between two names that share one variable.
    """
    values: dict[str, str] = {}
    stub = _Settings(api_keys_file=tmp_path / "unused" / "api_keys.json", api_key_values=values)
    monkeypatch.setattr(providers, "get_settings", lambda: stub)

    def _set(provider: str, value: str) -> None:
        values[provider] = value
        for name, env_var in providers._PROVIDER_ENV_VARS.items():
            if env_var == providers._PROVIDER_ENV_VARS.get(provider):
                values[name] = value

    return _set


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Point both search paths at per-test temp locations (no ambient file)."""
    settings = _Settings(api_keys_file=tmp_path / "standard" / "api_keys.json")
    monkeypatch.setattr(providers, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """Install a registry dict, keeping the ambient env out of the result.

    Returns a factory: ``registry({"openai": {"api_key": "sk-file"}})`` swaps
    the import-time ``_LLM_PROVIDERS`` dict for the given one.
    """
    stub = _Settings(api_keys_file=tmp_path / "unused" / "api_keys.json")
    monkeypatch.setattr(providers, "get_settings", lambda: stub)

    def _install(llm_providers: dict) -> dict:
        monkeypatch.setattr(providers, "_LLM_PROVIDERS", llm_providers)
        return llm_providers

    return _install


def _write_registry(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


# ---------------------------------------------------------------------------
# Module-level constants: aliases and env-var map
# ---------------------------------------------------------------------------


def test_provider_aliases_map_caller_names_to_canonical_names():
    assert providers._PROVIDER_ALIASES == {
        "claude-opus": "anthropic",
        "claude": "anthropic",
        "gemini": "google_gemini",
    }


def test_provider_env_vars_cover_every_documented_provider():
    assert providers._PROVIDER_ENV_VARS == {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google_gemini": "GEMINI_API_KEY",
        "xai": "XAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY",
        "together_ai": "TOGETHER_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
        "ngc": "NVIDIA_API_KEY",
        "eih": "EIH_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "deepseek-direct": "DEEPSEEK_API_KEY",
    }


def test_every_alias_target_has_an_environment_variable():
    """An alias points at a canonical name, and that name must have an env
    var or ``get_key`` would silently skip the environment step for it."""
    for canonical in providers._PROVIDER_ALIASES.values():
        assert canonical in providers._PROVIDER_ENV_VARS


def test_every_provider_this_module_names_has_a_settings_field_beside_it(monkeypatch):
    """``get_key`` resolves the environment through
    ``_ProviderSettings.api_key_for``, so a provider this module names but
    the settings class has never heard of would silently never see its env
    var: the mapping would be a superset of what can actually be read. Pin
    that the two agree, name for name, by exporting each provider's own
    variable and asking the real settings class what it sees.
    """
    missing = []
    for name, env_var in providers._PROVIDER_ENV_VARS.items():
        monkeypatch.setenv(env_var, f"{name}-probe")
        if _ProviderSettings().api_key_for(name) != f"{name}-probe":
            missing.append(name)
        monkeypatch.delenv(env_var)
    assert missing == []


# ---------------------------------------------------------------------------
# _search_paths
# ---------------------------------------------------------------------------


def test_search_paths_is_just_the_standard_file_when_keys_path_is_unset(isolated_settings):
    assert providers._search_paths() == [isolated_settings.providers.api_keys_file]


def test_search_paths_puts_the_explicit_keys_path_first(isolated_settings, tmp_path):
    override = tmp_path / "override" / "keys.json"
    isolated_settings.providers.keys_path = override
    assert providers._search_paths() == [override, isolated_settings.providers.api_keys_file]


def test_search_paths_expands_a_home_relative_keys_path(isolated_settings, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    isolated_settings.providers.keys_path = Path("~/mnemos/keys.json")
    assert providers._search_paths()[0] == tmp_path / "home" / "mnemos" / "keys.json"


def test_search_paths_expands_a_home_relative_standard_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    settings = _Settings(api_keys_file=Path("~/.config/mnemos/api_keys.json"))
    monkeypatch.setattr(providers, "get_settings", lambda: settings)
    assert providers._search_paths() == [tmp_path / "home" / ".config" / "mnemos" / "api_keys.json"]


def test_search_paths_builds_a_fresh_list_each_call(isolated_settings):
    first = providers._search_paths()
    second = providers._search_paths()
    assert first == second
    assert first is not second


# ---------------------------------------------------------------------------
# _find_key_file
# ---------------------------------------------------------------------------


def test_find_key_file_returns_none_when_no_candidate_exists(isolated_settings):
    assert providers._find_key_file() is None


def test_find_key_file_returns_the_standard_file_when_it_exists(isolated_settings):
    path = _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": {}})
    assert providers._find_key_file() == path


def test_find_key_file_prefers_the_explicit_keys_path(isolated_settings, tmp_path):
    _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": {}})
    override = _write_registry(tmp_path / "override" / "keys.json", {"llm_providers": {}})
    isolated_settings.providers.keys_path = override
    assert providers._find_key_file() == override


def test_find_key_file_falls_through_when_the_keys_path_is_absent(isolated_settings, tmp_path):
    isolated_settings.providers.keys_path = tmp_path / "missing" / "keys.json"
    standard = _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": {}})
    assert providers._find_key_file() == standard


def test_find_key_file_accepts_a_directory_as_a_candidate(isolated_settings, tmp_path):
    """``Path.exists()`` is true for a directory; the read failure surfaces
    one layer down, in ``load_provider_registry`` (pinned)."""
    directory = tmp_path / "keys_dir"
    directory.mkdir()
    isolated_settings.providers.keys_path = directory
    assert providers._find_key_file() == directory


# ---------------------------------------------------------------------------
# load_provider_registry — happy paths
# ---------------------------------------------------------------------------


def test_load_registry_returns_the_llm_providers_mapping(isolated_settings):
    _write_registry(
        isolated_settings.providers.api_keys_file,
        {
            "llm_providers": {
                "openai": {"api_key": "sk-openai"},
                "anthropic": {"api_key": "sk-ant", "weight": 3},
            }
        },
    )
    assert providers.load_provider_registry() == {
        "openai": {"api_key": "sk-openai"},
        "anthropic": {"api_key": "sk-ant", "weight": 3},
    }


def test_load_registry_returns_empty_dict_for_an_empty_mapping(isolated_settings):
    _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": {}})
    assert providers.load_provider_registry() == {}


def test_load_registry_ignores_unrelated_top_level_keys(isolated_settings):
    _write_registry(
        isolated_settings.providers.api_keys_file,
        {"version": 1, "llm_providers": {"groq": {"api_key": "gsk_x"}}, "other": [1, 2]},
    )
    assert providers.load_provider_registry() == {"groq": {"api_key": "gsk_x"}}


def test_load_registry_returns_a_fresh_mapping_on_each_call(isolated_settings):
    """The file is re-read per call — the returned mapping is not cached or
    shared with the module-level ``_LLM_PROVIDERS`` snapshot."""
    _write_registry(
        isolated_settings.providers.api_keys_file,
        {"llm_providers": {"openai": {"api_key": "sk-a"}}},
    )
    first = providers.load_provider_registry()
    second = providers.load_provider_registry()
    assert first == second
    assert first is not second
    assert first is not providers._LLM_PROVIDERS


def test_load_registry_reads_the_explicit_keys_path(isolated_settings, tmp_path):
    override = _write_registry(tmp_path / "override.json", {"llm_providers": {"xai": {"api_key": "xai-1"}}})
    isolated_settings.providers.keys_path = override
    assert providers.load_provider_registry() == {"xai": {"api_key": "xai-1"}}


def test_load_registry_logs_the_provider_count_at_debug(isolated_settings, caplog):
    _write_registry(
        isolated_settings.providers.api_keys_file,
        {"llm_providers": {"openai": {"api_key": "a"}, "groq": {"api_key": "b"}}},
    )
    with caplog.at_level(logging.DEBUG, logger=providers.logger.name):
        providers.load_provider_registry()
    records = [r for r in caplog.records if "loaded Provider Registry File" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].args[1] == 2


# ---------------------------------------------------------------------------
# load_provider_registry — no file / unreadable file
# ---------------------------------------------------------------------------


def test_load_registry_returns_empty_dict_and_logs_info_when_no_file_exists(isolated_settings, caplog):
    with caplog.at_level(logging.INFO, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    messages = [r.getMessage() for r in caplog.records]
    assert any("no Provider Registry File found in search paths" in m for m in messages)
    assert any("falling back to per-provider environment variables" in m for m in messages)


def test_load_registry_logs_the_paths_it_searched(isolated_settings, caplog):
    with caplog.at_level(logging.INFO, logger=providers.logger.name):
        providers.load_provider_registry()
    record = next(r for r in caplog.records if "no Provider Registry File found" in r.getMessage())
    assert record.args[0] == [str(isolated_settings.providers.api_keys_file)]


def test_load_registry_logs_the_keys_path_too_when_one_is_configured(isolated_settings, tmp_path, caplog):
    override = tmp_path / "missing" / "keys.json"
    isolated_settings.providers.keys_path = override
    with caplog.at_level(logging.INFO, logger=providers.logger.name):
        providers.load_provider_registry()
    record = next(r for r in caplog.records if "no Provider Registry File found" in r.getMessage())
    assert record.args[0] == [str(override), str(isolated_settings.providers.api_keys_file)]


@pytest.mark.parametrize(
    "body",
    [
        "{not json at all",
        "",
        '{"llm_providers": {"openai": {"api_key": "sk-1"}}',
    ],
)
def test_load_registry_returns_empty_dict_and_logs_error_on_unparseable_json(isolated_settings, caplog, body):
    path = isolated_settings.providers.api_keys_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    with caplog.at_level(logging.ERROR, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "failed to parse Provider Registry File" in errors[0].getMessage()
    assert errors[0].exc_info is not None


def test_load_registry_treats_parseable_json_of_the_wrong_shape_as_a_shape_warning(isolated_settings, caplog):
    """``[]`` is valid JSON, so it never reaches the parse-error branch; it is
    rejected by the "no 'llm_providers' wrapper" warning instead."""
    _write_registry(isolated_settings.providers.api_keys_file, [])
    with caplog.at_level(logging.WARNING, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("has no 'llm_providers' wrapper" in r.getMessage() for r in caplog.records)


def test_load_registry_error_log_names_the_offending_file(isolated_settings, caplog):
    path = isolated_settings.providers.api_keys_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    with caplog.at_level(logging.ERROR, logger=providers.logger.name):
        providers.load_provider_registry()
    error = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert str(path) in error.getMessage()


def test_load_registry_handles_a_non_utf8_file_as_a_parse_error(isolated_settings, caplog):
    path = isolated_settings.providers.api_keys_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00not utf-8")
    with caplog.at_level(logging.ERROR, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    assert any("failed to parse Provider Registry File" in r.getMessage() for r in caplog.records)


def test_load_registry_handles_a_directory_candidate_as_a_parse_error(isolated_settings, tmp_path, caplog):
    directory = tmp_path / "keys_dir"
    directory.mkdir()
    isolated_settings.providers.keys_path = directory
    with caplog.at_level(logging.ERROR, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    assert any("failed to parse Provider Registry File" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# load_provider_registry — wrong shapes are rejected loudly, still {}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("document", [[], "a string", 42, None, True])
def test_load_registry_warns_when_the_top_level_is_not_an_object(isolated_settings, caplog, document):
    _write_registry(isolated_settings.providers.api_keys_file, document)
    with caplog.at_level(logging.WARNING, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "has no 'llm_providers' wrapper" in warnings[0].getMessage()
    assert "expected shape is" in warnings[0].getMessage()


def test_load_registry_warns_when_the_wrapper_is_missing(isolated_settings, caplog):
    _write_registry(isolated_settings.providers.api_keys_file, {"providers": {"openai": {"api_key": "sk-1"}}})
    with caplog.at_level(logging.WARNING, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "has no 'llm_providers' wrapper" in warnings[0].getMessage()


@pytest.mark.parametrize("wrapper", [[], "openai", 7, None, True])
def test_load_registry_warns_when_the_wrapper_is_not_an_object(isolated_settings, caplog, wrapper):
    _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": wrapper})
    with caplog.at_level(logging.WARNING, logger=providers.logger.name):
        assert providers.load_provider_registry() == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "llm_providers is not an object" in warnings[0].getMessage()


def test_load_registry_never_raises_on_a_malformed_file(isolated_settings):
    """The public contract is "returns a dict" — a bad file degrades, it does
    not raise into the caller's import."""
    path = isolated_settings.providers.api_keys_file
    for body in ("", "x", "[]", "{}", '{"llm_providers": 3}', '{"llm_providers": {}}'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        assert providers.load_provider_registry() == {}


# ---------------------------------------------------------------------------
# The import-time snapshot
# ---------------------------------------------------------------------------


def test_module_level_registry_is_a_dict():
    """``_LLM_PROVIDERS`` is the one-shot load performed at import time."""
    assert isinstance(providers._LLM_PROVIDERS, dict)


# ---------------------------------------------------------------------------
# get_provider_config
# ---------------------------------------------------------------------------


def test_get_provider_config_returns_the_entry_for_a_known_provider(registry):
    registry({"openai": {"api_key": "sk-1", "base_url": "http://gw/v1", "weight": 2}})
    assert providers.get_provider_config("openai") == {
        "api_key": "sk-1",
        "base_url": "http://gw/v1",
        "weight": 2,
    }


def test_get_provider_config_returns_empty_dict_for_an_unknown_provider(registry):
    registry({"openai": {"api_key": "sk-1"}})
    assert providers.get_provider_config("nope") == {}


def test_get_provider_config_returns_empty_dict_when_the_registry_is_empty(registry):
    registry({})
    assert providers.get_provider_config("openai") == {}


def test_get_provider_config_returns_a_copy_of_the_registry_entry(registry):
    entry = {"api_key": "sk-keep"}
    registry({"openai": entry})
    config = providers.get_provider_config("openai")
    config["api_key"] = "sk-mutated"
    assert entry == {"api_key": "sk-keep"}
    assert providers.get_provider_config("openai") == {"api_key": "sk-keep"}


def test_get_provider_config_copy_is_shallow(registry):
    nested = {"headers": {"x": "1"}}
    registry({"openai": {"api_key": "sk-1", "extra": nested}})
    providers.get_provider_config("openai")["extra"]["headers"]["x"] = "2"
    assert nested == {"headers": {"x": "2"}}


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("claude", "anthropic"), ("claude-opus", "anthropic"), ("gemini", "google_gemini")],
)
def test_get_provider_config_resolves_aliases(registry, alias, canonical):
    registry({canonical: {"api_key": f"{canonical}-key"}})
    assert providers.get_provider_config(alias) == {"api_key": f"{canonical}-key"}


def test_get_provider_config_uses_the_canonical_entry_when_both_names_exist(registry):
    registry({"anthropic": {"api_key": "canon"}, "claude": {"api_key": "alias"}})
    assert providers.get_provider_config("claude") == {"api_key": "canon"}


@pytest.mark.parametrize("value", ["not-a-dict", 5, None, ["a"], True])
def test_get_provider_config_returns_empty_dict_when_the_entry_is_not_an_object(registry, value):
    registry({"openai": value})
    assert providers.get_provider_config("openai") == {}


def test_get_provider_config_returns_empty_dict_for_an_empty_entry(registry):
    registry({"openai": {}})
    assert providers.get_provider_config("openai") == {}


def test_get_provider_config_accepts_an_empty_provider_name(registry):
    registry({"": {"api_key": "nameless"}})
    assert providers.get_provider_config("") == {"api_key": "nameless"}


def test_get_provider_config_is_case_sensitive(registry):
    registry({"OpenAI": {"api_key": "mixed"}})
    assert providers.get_provider_config("openai") == {}
    assert providers.get_provider_config("OpenAI") == {"api_key": "mixed"}


def test_get_provider_config_does_not_strip_the_provider_name(registry):
    registry({"openai": {"api_key": "sk-1"}})
    assert providers.get_provider_config(" openai ") == {}


# ---------------------------------------------------------------------------
# get_key — registry path
# ---------------------------------------------------------------------------


def test_get_key_returns_the_registry_api_key(registry):
    registry({"openai": {"api_key": "sk-from-file"}})
    assert providers.get_key("openai") == "sk-from-file"


def test_get_key_returns_empty_string_for_an_unknown_provider(registry):
    registry({"openai": {"api_key": "sk-1"}})
    assert providers.get_key("unknown-provider") == ""


def test_get_key_returns_empty_string_when_the_registry_is_empty(registry):
    registry({})
    assert providers.get_key("openai") == ""


def test_get_key_returns_empty_string_when_the_entry_has_no_api_key(registry):
    registry({"openai": {"base_url": "http://gw/v1"}})
    assert providers.get_key("openai") == ""


@pytest.mark.parametrize("value", ["not-a-dict", 5, None, ["sk-1"]])
def test_get_key_raises_attribute_error_when_the_entry_is_not_an_object(registry, value):
    """Unlike ``get_provider_config`` — which type-checks the entry and returns
    ``{}`` — ``get_key`` calls ``.get("api_key", "")`` on it directly, so a
    non-object entry raises ``AttributeError`` (pinned; see the report).
    ``load_provider_registry`` only guarantees the ``llm_providers`` *wrapper*
    is an object, not the per-provider entries inside it."""
    registry({"openai": value})
    with pytest.raises(AttributeError, match="has no attribute 'get'"):
        providers.get_key("openai")


@pytest.mark.parametrize("api_key", ["", "   ", "\n\t "])
def test_get_key_does_not_strip_the_registry_value(registry, api_key):
    """Only the environment value is stripped; a file value is returned
    verbatim (pinned)."""
    registry({"openai": {"api_key": api_key}})
    assert providers.get_key("openai") == api_key


def test_get_key_returns_a_non_string_registry_value_verbatim(registry):
    """No coercion or type check on the file value (pinned)."""
    registry({"openai": {"api_key": 12345}})
    assert providers.get_key("openai") == 12345


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("claude", "anthropic"), ("claude-opus", "anthropic"), ("gemini", "google_gemini")],
)
def test_get_key_resolves_aliases_through_the_registry(registry, alias, canonical):
    registry({canonical: {"api_key": f"{canonical}-key"}})
    assert providers.get_key(alias) == f"{canonical}-key"


def test_get_key_uses_the_canonical_entry_when_both_names_exist(registry):
    registry({"anthropic": {"api_key": "canon"}, "claude": {"api_key": "alias"}})
    assert providers.get_key("claude") == "canon"


def test_get_key_is_case_sensitive(registry):
    registry({"OpenAI": {"api_key": "mixed"}})
    assert providers.get_key("openai") == ""


def test_get_key_returns_empty_string_for_an_empty_provider_name(registry):
    registry({})
    assert providers.get_key("") == ""


def test_get_key_returns_empty_string_for_none(registry):
    """``None`` is not in the alias table; both lookups simply miss."""
    registry({"openai": {"api_key": "sk-1"}})
    assert providers.get_key(None) == ""


# ---------------------------------------------------------------------------
# get_key — environment override path
# ---------------------------------------------------------------------------


def test_get_key_returns_the_stripped_environment_value(registry, env_override):
    registry({"openai": {"api_key": "sk-from-file"}})
    env_override("openai", "  sk-from-env  ")
    assert providers.get_key("openai") == "sk-from-env"


def test_get_key_environment_value_wins_over_the_registry(registry, env_override):
    registry({"anthropic": {"api_key": "sk-ant-file"}})
    env_override("anthropic", "sk-ant-env")
    assert providers.get_key("anthropic") == "sk-ant-env"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_get_key_falls_back_to_the_registry_when_the_environment_value_is_blank(registry, env_override, blank):
    registry({"groq": {"api_key": "gsk-file"}})
    env_override("groq", blank)
    assert providers.get_key("groq") == "gsk-file"


def test_get_key_returns_empty_string_when_a_blank_environment_value_has_no_file_fallback(registry, env_override):
    registry({})
    env_override("groq", "   ")
    assert providers.get_key("groq") == ""


def test_get_key_looks_up_the_environment_by_the_canonical_name(registry, env_override):
    registry({})
    env_override("google_gemini", "AIza-env")
    assert providers.get_key("gemini") == "AIza-env"


def test_get_key_does_not_look_up_the_environment_by_the_alias_name(registry, env_override):
    """Env values are keyed by canonical name only (pinned)."""
    registry({})
    env_override("claude", "sk-ant-alias")
    assert providers.get_key("claude") == ""


@pytest.mark.parametrize("provider", ["nvidia", "ngc"])
def test_get_key_shares_the_nvidia_value_between_names(registry, env_override, provider):
    """``ngc`` and ``nvidia`` both resolve ``NVIDIA_API_KEY`` — the two halves
    of the settings object agree on that pair (verified against the real
    ``_ProviderSettings.api_key_for``)."""
    registry({})
    env_override("nvidia", "nvapi-env")
    assert providers.get_key(provider) == "nvapi-env"


@pytest.mark.parametrize("provider", ["deepseek", "deepseek-direct"])
def test_get_key_shares_the_deepseek_value_between_names(registry, env_override, provider):
    registry({})
    env_override("deepseek", "ds-env")
    assert providers.get_key(provider) == "ds-env"


def test_get_key_skips_the_environment_for_providers_without_an_env_var(registry, env_override):
    registry({"custom_provider": {"api_key": "custom-file"}})
    env_override("custom_provider", "custom-env")
    assert providers.get_key("custom_provider") == "custom-file"


def test_get_key_reads_the_environment_for_every_provider_in_its_env_var_map(registry, env_override):
    """Every ``_PROVIDER_ENV_VARS`` key goes through the settings lookup.

    Pinned per name so a name that is added to one map but forgotten in the
    other (``_PROVIDER_ENV_VARS`` here, ``_ProviderSettings.api_key_for`` in
    ``mnemos/core/config.py``) shows up as a failing test rather than as a
    silently ignored environment variable.
    """
    for provider in providers._PROVIDER_ENV_VARS:
        registry({})
        env_override(provider, f"{provider}-env")
        assert providers.get_key(provider) == f"{provider}-env", provider


def test_get_key_returns_the_raw_environment_value_after_stripping_once(registry, env_override):
    """``.strip()`` is the only transformation applied to the env value."""
    registry({})
    env_override("xai", "\t xai-1 \n")
    assert providers.get_key("xai") == "xai-1"


def test_get_key_asks_the_settings_for_the_canonical_name(registry, monkeypatch, tmp_path):
    asked: list[str] = []
    stub = _Settings(api_keys_file=tmp_path / "unused" / "api_keys.json")
    stub.providers.api_key_for = lambda provider: asked.append(provider) or ""
    monkeypatch.setattr(providers, "get_settings", lambda: stub)
    monkeypatch.setattr(providers, "_LLM_PROVIDERS", {})
    providers.get_key("openai")
    assert asked == ["openai"]


# ---------------------------------------------------------------------------
# The ambient-source guard is load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["MNEMOS_KEYS_PATH", "API_KEYS_FILE"])
def test_search_paths_ignores_ambient_key_path_variables(monkeypatch, tmp_path, isolated_settings, variable):
    """The autouse ``no_ambient_key_sources`` guard is what makes the ambient
    ``$MNEMOS_KEYS_PATH`` / ``$API_KEYS_FILE`` invisible to the search paths.
    Re-exporting one must not change them, or a developer's environment — not
    the test — would decide which registry file is read."""
    monkeypatch.setenv(variable, str(tmp_path / "ambient" / "keys.json"))
    assert providers._search_paths() == [isolated_settings.providers.api_keys_file]


# ---------------------------------------------------------------------------
# Suspected defect (xfail, strict) — see the report
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "defect: one unusable candidate in the search path shadows every later one. "
        "_find_key_file returns the FIRST path whose .exists() is true, and "
        "load_provider_registry then wraps the single open()/json.load in a broad "
        "except that returns {} — so a $MNEMOS_KEYS_PATH that exists but is "
        "unreadable (a directory, a permission-denied file, invalid JSON) or is "
        "readable but the wrong shape makes an otherwise valid "
        "~/.config/mnemos/api_keys.json invisible. The documented 'first READABLE "
        "path' wording in the module docstring is not what the code does. Pinned as "
        "current-and-wrong; when it is fixed this xpasses and the strict marker "
        "flags the pin for update."
    ),
)
@pytest.mark.parametrize(
    "obstructing_candidate",
    ["invalid-json", "wrong-shape", "empty-object", "directory"],
)
def test_unusable_override_candidate_falls_through_to_the_usable_standard_file(
    isolated_settings, tmp_path, obstructing_candidate
):
    override = tmp_path / "override"
    if obstructing_candidate == "invalid-json":
        override.write_text("{not json")
    elif obstructing_candidate == "wrong-shape":
        override.write_text('{"providers": {"openai": {"api_key": "sk-nope"}}}')
    elif obstructing_candidate == "empty-object":
        override.write_text('{"llm_providers": {}}')
    else:
        override.mkdir()
    isolated_settings.providers.keys_path = override
    _write_registry(isolated_settings.providers.api_keys_file, {"llm_providers": {"openai": {"api_key": "sk-good"}}})
    assert providers.load_provider_registry() == {"openai": {"api_key": "sk-good"}}
