"""Characterisation tests for ``mnemos/installer/agent.py``.

``mnemos/installer/agent.py`` is the LLM-backed conversational installer
(621 lines) and on 2026-11-05 had exactly one test file referencing it
(``tests/test_installer_agent_entrypoint.py``, two assertions). These tests
PIN its CURRENT behaviour through the public surface — ``SystemInfo``,
``detect_environment``, ``AgentInstaller``, ``run_agent``,
``run_agent_installer`` — across normal paths, edge cases (empty, ``None``,
boundary values), every reachable exception/degradation path, and each branch
that can be reached with plain fixtures.

The module uses only stdlib I/O, so nothing here touches the network, sleeps,
or hits a real database: ``urllib.request.urlopen``, ``subprocess.run``,
``shutil.disk_usage``, ``socket.gethostname``, ``input`` and the clock-free
``sys.version`` are all replaced with deterministic fakes.

These are not design proposals. If a value changes on purpose, update the pin
with the change.

No real defect was found in ``agent.py`` while writing these: every oddity
probed (unvalidated ``db_port``, ``localhostfoo`` accepted as a hostname) is
consistent with the module's stated intent, so both are pinned as ordinary
passing tests rather than as bugs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from unittest import mock

import pytest

from mnemos.installer import agent
from mnemos.installer.agent import (
    AgentInstaller,
    SystemInfo,
    _canonical_profile,
    detect_environment,
    run_agent,
    run_agent_installer,
)
from mnemos.installer.wizard import Config

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __init__(self, payload):
        if isinstance(payload, bytes):
            self._body = payload
        else:
            self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _completed(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if a test reaches the real network."""

    def _boom(*args, **kwargs):
        raise AssertionError("test attempted real network access")

    monkeypatch.setattr(agent.urllib.request, "urlopen", _boom)
    return _boom


def _installer(**info_kwargs):
    """Build an installer around an explicit SystemInfo snapshot.

    ``AgentInstaller.__init__`` only reads ``self.info`` flags, so this is
    fully deterministic and never probes the host.
    """
    return AgentInstaller(SystemInfo(**info_kwargs))


def _answer_input(monkeypatch, answers):
    """Feed ``answers`` to ``input()``; raise EOFError once exhausted."""
    remaining = iter(answers)

    def _fake_input(prompt=""):
        del prompt
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(agent, "input", _fake_input, raising=False)
    monkeypatch.setattr("builtins.input", _fake_input)
    return _fake_input


def _stub_llm(monkeypatch, installer, responses):
    """Replace ``installer._llm`` with a recorder over ``responses``."""
    remaining = iter(responses)
    calls = []

    def _fake_llm(prompt, system=None):
        calls.append({"prompt": prompt, "system": system})
        try:
            return next(remaining)
        except StopIteration:
            raise AssertionError("installer called _llm more times than scripted") from None

    monkeypatch.setattr(installer, "_llm", _fake_llm)
    return calls


def _json_block(payload) -> str:
    return "Here you go:\n```json\n" + json.dumps(payload) + "\n```\n"


# ---------------------------------------------------------------------------
# SystemInfo.to_text
# ---------------------------------------------------------------------------


class TestSystemInfoToText:
    def test_defaults_render_empty_snapshot_with_stable_line_order(self):
        text = SystemInfo().to_text()
        assert text.splitlines() == [
            "Host: ",
            "OS:  ",
            "Python: ",
            "Disk free: 0.0 GB",
            "Sudo: not available",
            "PostgreSQL: not detected",
            "GRAEAE: not reachable",
            "Ollama: not running",
            "Anthropic API key: not set",
        ]

    def test_populated_snapshot_renders_each_detected_value(self):
        info = SystemInfo(
            hostname="nav-hyp",
            os_name="Linux",
            os_version="6.1.0",
            python_version="3.11.9",
            disk_free_gb=120.44,
            has_sudo=True,
            postgres_running=True,
            postgres_version="16.2",
            graeae_reachable=True,
            ollama_reachable=True,
            ollama_models=["llama3:8b", "gemma2:2b"],
            anthropic_key_set=True,
        )
        text = info.to_text()
        assert "Host: nav-hyp" in text
        assert "OS: Linux 6.1.0" in text
        assert "Python: 3.11.9" in text
        assert "Disk free: 120.4 GB" in text  # one decimal place
        assert "Sudo: available" in text
        assert "PostgreSQL: running (16.2)" in text
        assert "GRAEAE: reachable" in text
        assert "Ollama: running, models: llama3:8b, gemma2:2b" in text
        assert "Anthropic API key: set" in text

    def test_postgres_running_without_version_omits_parenthetical(self):
        assert "PostgreSQL: running" in SystemInfo(postgres_running=True).to_text().splitlines()

    def test_ollama_running_with_no_models_renders_empty_model_list(self):
        line = SystemInfo(ollama_reachable=True).to_text().splitlines()[7]
        assert line == "Ollama: running, models: "

    def test_ollama_models_are_truncated_to_first_five(self):
        models = [f"model-{index}" for index in range(8)]
        line = SystemInfo(ollama_reachable=True, ollama_models=models).to_text().splitlines()[7]
        assert line == "Ollama: running, models: model-0, model-1, model-2, model-3, model-4"

    def test_ollama_unreachable_hides_models_entirely(self):
        line = SystemInfo(ollama_reachable=False, ollama_models=["llama3"]).to_text().splitlines()[7]
        assert line == "Ollama: not running"

    def test_disk_free_rounds_to_one_decimal(self):
        assert "Disk free: 1.2 GB" in SystemInfo(disk_free_gb=1.25).to_text()

    def test_disk_free_zero_is_not_special_cased(self):
        assert "Disk free: 0.0 GB" in SystemInfo(disk_free_gb=0.0).to_text()


# ---------------------------------------------------------------------------
# _canonical_profile
# ---------------------------------------------------------------------------


class TestCanonicalProfile:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("personal", "edge"),
            ("team", "server"),
            ("enterprise", "server"),
            ("edge", "edge"),
            ("server", "server"),
            ("dev", "dev"),
            ("", ""),
        ],
    )
    def test_maps_known_aliases_and_passes_unknown_through(self, raw, expected):
        assert _canonical_profile(raw) == expected

    def test_is_case_sensitive_and_does_not_strip(self):
        assert _canonical_profile("Personal") == "Personal"
        assert _canonical_profile(" personal ") == " personal "


# ---------------------------------------------------------------------------
# detect_environment
# ---------------------------------------------------------------------------


class TestDetectEnvironment:
    def test_happy_path_populates_every_field(self, monkeypatch):
        def _fake_run(command, **kwargs):
            if command[:2] == ["sudo", "-n"]:
                return _completed(returncode=0)
            if command[:1] == ["pg_isready"]:
                return _completed(returncode=0)
            if command[:1] == ["psql"]:
                return _completed(returncode=0, stdout="psql (PostgreSQL) 16.2\n")
            raise AssertionError(f"unexpected command: {command}")

        def _fake_urlopen(request, timeout=None):
            url = request.full_url
            if "11434" in url:
                return _FakeResponse({"models": [{"name": "llama3:8b"}, {"name": "gemma2:2b"}]})
            return _FakeResponse({})

        monkeypatch.setattr(agent.subprocess, "run", _fake_run)
        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=100 * (1024**3)))
        monkeypatch.setattr(agent.socket, "gethostname", lambda: "nav-hyp")
        monkeypatch.setattr(agent.platform, "system", lambda: "Linux")
        monkeypatch.setattr(agent.platform, "version", lambda: "6.1.0")
        monkeypatch.setattr(agent.sys, "version", "3.11.9 (main, Jan 1 2026) [GCC]")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "  secret-key  ")

        info = detect_environment()

        assert info.hostname == "nav-hyp"
        assert info.os_name == "Linux"
        assert info.os_version == "6.1.0"
        assert info.python_version == "3.11.9"
        assert info.disk_free_gb == pytest.approx(100.0)
        assert info.has_sudo is True
        assert info.postgres_running is True
        assert info.postgres_version == "16.2"
        assert info.graeae_reachable is True
        assert info.ollama_reachable is True
        assert info.ollama_models == ["llama3:8b", "gemma2:2b"]
        assert info.anthropic_key_set is True

    def test_every_probe_failing_degrades_to_false_defaults(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise OSError("unavailable")

        monkeypatch.setattr(agent.subprocess, "run", _raise)
        monkeypatch.setattr(agent.urllib.request, "urlopen", _raise)
        monkeypatch.setattr(agent.shutil, "disk_usage", _raise)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

        info = detect_environment()

        assert info.disk_free_gb == 0.0
        assert info.has_sudo is False
        assert info.postgres_running is False
        assert info.postgres_version == ""
        assert info.graeae_reachable is False
        assert info.ollama_reachable is False
        assert info.ollama_models == []
        assert info.anthropic_key_set is False  # whitespace-only key counts as unset

    def test_sudo_nonzero_exit_means_unavailable(self, monkeypatch):
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _completed(returncode=1))
        monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({}))
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert detect_environment().has_sudo is False

    def test_postgres_running_but_psql_failing_leaves_version_empty(self, monkeypatch):
        def _fake_run(command, **kwargs):
            if command[:1] == ["pg_isready"]:
                return _completed(returncode=0)
            if command[:1] == ["psql"]:
                return _completed(returncode=1)
            return _completed(returncode=1)

        monkeypatch.setattr(agent.subprocess, "run", _fake_run)
        monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({}))
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))

        info = detect_environment()
        assert info.postgres_running is True
        assert info.postgres_version == ""

    def test_postgres_version_takes_last_whitespace_token(self, monkeypatch):
        def _fake_run(command, **kwargs):
            if command[:1] == ["pg_isready"]:
                return _completed(returncode=0)
            if command[:1] == ["psql"]:
                return _completed(returncode=0, stdout="psql (PostgreSQL) 15.5\n")
            return _completed(returncode=1)

        monkeypatch.setattr(agent.subprocess, "run", _fake_run)
        monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({}))
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))
        assert detect_environment().postgres_version == "15.5"

    def test_graeae_probe_uses_configured_url_env_var(self, monkeypatch):
        seen = []

        def _fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _completed(returncode=1))
        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))
        monkeypatch.setenv("MNEMOS_GRAEAE_URL", "http://graeae.internal:5002")

        detect_environment()
        assert "http://graeae.internal:5002/health" in seen

    def test_ollama_payload_without_models_key_yields_empty_list(self, monkeypatch):
        def _fake_urlopen(request, timeout=None):
            if "11434" in request.full_url:
                return _FakeResponse({})
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _completed(returncode=1))
        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))

        info = detect_environment()
        assert info.ollama_reachable is True
        assert info.ollama_models == []

    def test_ollama_models_missing_name_key_default_to_empty_string(self, monkeypatch):
        def _fake_urlopen(request, timeout=None):
            if "11434" in request.full_url:
                return _FakeResponse({"models": [{"model": "ignored"}, {"name": "llama3"}]})
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _completed(returncode=1))
        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))

        assert detect_environment().ollama_models == ["", "llama3"]

    def test_anthropic_key_present_without_trailing_whitespace_is_set(self, monkeypatch):
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _completed(returncode=1))
        monkeypatch.setattr(agent.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({}))
        monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: mock.Mock(free=0))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        assert detect_environment().anthropic_key_set is True


# ---------------------------------------------------------------------------
# AgentInstaller construction / backend detection
# ---------------------------------------------------------------------------


class TestBackendDetection:
    def test_graeae_wins_when_every_backend_is_available(self):
        installer = _installer(graeae_reachable=True, ollama_reachable=True, ollama_models=["m"], anthropic_key_set=True)
        assert installer.backend == "graeae"

    def test_ollama_used_when_graeae_unreachable(self):
        installer = _installer(ollama_reachable=True, ollama_models=["llama3"], anthropic_key_set=True)
        assert installer.backend == "ollama"

    def test_anthropic_used_when_only_key_is_set(self):
        assert _installer(anthropic_key_set=True).backend == "anthropic"

    def test_no_backend_yields_none(self):
        assert _installer().backend is None

    def test_ollama_reachable_without_models_is_not_a_backend(self):
        installer = _installer(ollama_reachable=True, ollama_models=[])
        assert installer.backend is None

    def test_construction_leaves_history_and_config_unset(self):
        installer = _installer()
        assert installer.history == []
        assert installer.config is None

    def test_max_turns_is_ten(self):
        assert AgentInstaller.MAX_TURNS == 10

    def test_graeae_url_is_captured_at_class_definition_time(self, monkeypatch):
        # Pins the existing attribute: setting the env var after import does
        # NOT change AgentInstaller.GRAEAE_URL (it is read once at class body).
        original = AgentInstaller.GRAEAE_URL
        monkeypatch.setenv("MNEMOS_GRAEAE_URL", "http://changed.example:9999")
        assert AgentInstaller.GRAEAE_URL == original


class TestPickOllamaModel:
    @pytest.mark.parametrize(
        ("models", "expected"),
        [
            ([], "llama3"),
            (["mistral:7b"], "mistral:7b"),
            (["mistral:7b", "llama3:8b"], "llama3:8b"),
            (["mistral:7b", "gemma2:2b"], "gemma2:2b"),
            (["LLAMA3:8B"], "LLAMA3:8B"),
            (["qwen", "phi", "gemma2"], "gemma2"),
        ],
    )
    def test_prefers_llama_or_gemma_else_first_model(self, models, expected):
        assert _installer(ollama_models=models)._ollama_model == expected

    def test_empty_model_list_falls_back_to_llama3(self):
        assert _installer()._ollama_model == "llama3"


class TestBuildSystemPrompt:
    def test_prompt_states_required_keys_and_embeds_environment(self):
        installer = _installer(hostname="nav-hyp", os_name="Linux", python_version="3.11.9")
        prompt = installer._build_system_prompt()
        for key in ("profile", "db_name", "db_user", "db_password", "listen_port"):
            assert key in prompt
        assert "```json" in prompt
        assert "Detected environment:\n" in prompt
        assert "Host: nav-hyp" in prompt
        assert "MNEMOS" in prompt

    def test_prompt_is_deterministic_for_the_same_snapshot(self):
        installer = _installer(hostname="h", os_name="Linux")
        assert installer._build_system_prompt() == installer._build_system_prompt()

    def test_prompt_reflects_mutated_info(self):
        installer = _installer(hostname="before")
        before = installer._build_system_prompt()
        installer.info.hostname = "after"
        assert before != installer._build_system_prompt()


# ---------------------------------------------------------------------------
# LLM dispatch and transports
# ---------------------------------------------------------------------------


class TestLlmDispatch:
    def test_none_backend_returns_empty_string_without_io(self, no_network):
        assert _installer()._llm("hello") == ""

    def test_dispatch_routes_to_each_transport(self, monkeypatch):
        installer = _installer()
        for name in ("_llm_graeae", "_llm_ollama", "_llm_anthropic"):
            monkeypatch.setattr(installer, name, lambda prompt, system=None, _n=name: f"routed:{_n}")
        installer.backend = "graeae"
        assert installer._llm("q") == "routed:_llm_graeae"
        installer.backend = "ollama"
        assert installer._llm("q") == "routed:_llm_ollama"
        installer.backend = "anthropic"
        assert installer._llm("q") == "routed:_llm_anthropic"


class TestGraeaeTransport:
    def _capture(self, monkeypatch, payload):
        captured = {}

        def _fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode())
            return _FakeResponse(payload)

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        return captured

    def test_picks_highest_scoring_response(self, monkeypatch):
        self._capture(
            monkeypatch,
            {
                "all_responses": {
                    "a": {"response_text": "low", "final_score": 1},
                    "b": {"response_text": "high", "final_score": 9},
                    "c": {"response_text": "mid", "final_score": 5},
                }
            },
        )
        assert _installer(graeae_reachable=True)._llm("q") == "high"

    def test_missing_final_score_scores_as_zero(self, monkeypatch):
        self._capture(
            monkeypatch,
            {
                "all_responses": {
                    "a": {"response_text": "scored", "final_score": 2},
                    "b": {"response_text": "unscored"},
                }
            },
        )
        assert _installer(graeae_reachable=True)._llm("q") == "scored"

    def test_all_responses_present_but_empty_falls_back_to_flat_field(self, monkeypatch):
        self._capture(monkeypatch, {"all_responses": {}, "response_text": "flat"})
        assert _installer(graeae_reachable=True)._llm("q") == "flat"

    def test_no_recognised_fields_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {})
        assert _installer(graeae_reachable=True)._llm("q") == ""

    def test_best_response_without_text_key_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {"all_responses": {"a": {"final_score": 1}}})
        assert _installer(graeae_reachable=True)._llm("q") == ""

    def test_request_shape_points_at_consult_with_reasoning_task(self, monkeypatch):
        captured = self._capture(monkeypatch, {"response_text": "x"})
        _installer(graeae_reachable=True)._llm("the question")
        assert captured["url"].endswith("/graeae/consult")
        assert captured["body"]["task_type"] == "reasoning"
        assert captured["body"]["prompt"] == "the question"

    def test_system_prompt_is_prepended_in_brackets(self, monkeypatch):
        captured = self._capture(monkeypatch, {"response_text": "x"})
        _installer(graeae_reachable=True)._llm("question", system="SYSTEM")
        assert captured["body"]["prompt"].startswith("[System]\nSYSTEM\n\nquestion")

    def test_history_is_inlined_and_truncated_to_last_six_turns(self, monkeypatch):
        captured = self._capture(monkeypatch, {"response_text": "x"})
        installer = _installer(graeae_reachable=True)
        installer.history = [{"role": "user", "content": f"msg{index}"} for index in range(10)]
        installer._llm("current")
        prompt = captured["body"]["prompt"]
        assert prompt.startswith("[Conversation so far]")
        assert "msg9" in prompt
        assert "msg4" in prompt  # sixth-from-last
        assert "msg3" not in prompt
        assert prompt.endswith("[New user message]\ncurrent")
        assert "USER: msg9" in prompt  # role is upper-cased

    def test_no_history_means_no_conversation_block(self, monkeypatch):
        captured = self._capture(monkeypatch, {"response_text": "x"})
        _installer(graeae_reachable=True)._llm("solo")
        assert "[Conversation so far]" not in captured["body"]["prompt"]

    @pytest.mark.parametrize(
        "exc",
        [urllib.error.URLError("down"), TimeoutError("slow"), json.JSONDecodeError("bad", "x", 0)],
    )
    def test_errors_degrade_to_empty_string_and_clear_backend_when_no_fallback(self, monkeypatch, exc, capsys):
        def _raise(*args, **kwargs):
            raise exc

        monkeypatch.setattr(agent.urllib.request, "urlopen", _raise)
        installer = _installer(graeae_reachable=True)
        assert installer._llm("q") == ""
        assert installer.backend is None
        assert "GRAEAE timeout/error" in capsys.readouterr().out

    def test_error_falls_back_to_ollama_when_models_exist(self, monkeypatch):
        installer = _installer(graeae_reachable=True, ollama_reachable=True, ollama_models=["llama3"])

        def _fake_urlopen(request, timeout=None):
            if "11434" in request.full_url:
                return _FakeResponse({"message": {"content": "ollama said"}})
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        assert installer._llm("q") == "ollama said"
        assert installer.backend == "ollama"

    def test_error_falls_back_to_anthropic_when_no_ollama(self, monkeypatch):
        installer = _installer(graeae_reachable=True, anthropic_key_set=True)

        def _fake_urlopen(request, timeout=None):
            if "anthropic.com" in request.full_url:
                return _FakeResponse({"content": [{"text": "anthropic said"}]})
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert installer._llm("q") == "anthropic said"
        assert installer.backend == "anthropic"

    def test_ollama_is_preferred_over_anthropic_when_both_available(self, monkeypatch):
        installer = _installer(graeae_reachable=True, ollama_reachable=True, ollama_models=["llama3"], anthropic_key_set=True)

        def _fake_urlopen(request, timeout=None):
            if "11434" in request.full_url:
                return _FakeResponse({"message": {"content": "ollama"}})
            if "anthropic.com" in request.full_url:
                raise AssertionError("anthropic should not be reached")
            raise urllib.error.URLError("graeae down")

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        assert installer._llm("q") == "ollama"


class TestOllamaTransport:
    def _capture(self, monkeypatch, payload):
        captured = {}

        def _fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            if isinstance(payload, Exception):
                raise payload
            return _FakeResponse(payload)

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        return captured

    def test_returns_message_content(self, monkeypatch):
        self._capture(monkeypatch, {"message": {"content": "hi there"}})
        assert _installer(ollama_reachable=True, ollama_models=["llama3"])._llm("q") == "hi there"

    def test_missing_message_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {})
        assert _installer(ollama_reachable=True, ollama_models=["llama3"])._llm("q") == ""

    def test_message_without_content_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {"message": {}})
        assert _installer(ollama_reachable=True, ollama_models=["llama3"])._llm("q") == ""

    def test_payload_carries_selected_model_and_non_streaming_flag(self, monkeypatch):
        captured = self._capture(monkeypatch, {"message": {"content": "x"}})
        _installer(ollama_reachable=True, ollama_models=["mistral", "gemma2"])._llm("q")
        assert captured["url"].endswith("/api/chat")
        assert captured["body"]["model"] == "gemma2"
        assert captured["body"]["stream"] is False

    def test_message_list_is_system_then_last_eight_history_then_user(self, monkeypatch):
        captured = self._capture(monkeypatch, {"message": {"content": "x"}})
        installer = _installer(ollama_reachable=True, ollama_models=["llama3"])
        installer.history = [{"role": "user", "content": f"msg{index}"} for index in range(10)]
        installer._llm("current", system="SYSTEM")
        messages = captured["body"]["messages"]
        assert messages[0] == {"role": "system", "content": "SYSTEM"}
        assert messages[-1] == {"role": "user", "content": "current"}
        assert messages[1] == {"role": "user", "content": "msg2"}  # last 8 of 10
        assert len(messages) == 10

    def test_no_system_means_no_system_message(self, monkeypatch):
        captured = self._capture(monkeypatch, {"message": {"content": "x"}})
        _installer(ollama_reachable=True, ollama_models=["llama3"])._llm("q")
        assert [m["role"] for m in captured["body"]["messages"]] == ["user"]

    @pytest.mark.parametrize(
        "exc",
        [urllib.error.URLError("down"), TimeoutError("slow"), json.JSONDecodeError("bad", "x", 0)],
    )
    def test_errors_clear_backend_when_no_anthropic_key(self, monkeypatch, exc, capsys):
        self._capture(monkeypatch, exc)
        installer = _installer(ollama_reachable=True, ollama_models=["llama3"])
        assert installer._llm("q") == ""
        assert installer.backend is None
        assert "Ollama error" in capsys.readouterr().out

    def test_error_falls_back_to_anthropic(self, monkeypatch):
        installer = _installer(ollama_reachable=True, ollama_models=["llama3"], anthropic_key_set=True)

        def _fake_urlopen(request, timeout=None):
            if "anthropic.com" in request.full_url:
                return _FakeResponse({"content": [{"text": "anthropic"}]})
            raise urllib.error.URLError("down")

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert installer._llm("q") == "anthropic"
        assert installer.backend == "anthropic"


class TestAnthropicTransport:
    def _capture(self, monkeypatch, payload):
        captured = {}

        def _fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode())
            if isinstance(payload, Exception):
                raise payload
            return _FakeResponse(payload)

        monkeypatch.setattr(agent.urllib.request, "urlopen", _fake_urlopen)
        return captured

    def test_returns_first_content_block_text(self, monkeypatch):
        self._capture(monkeypatch, {"content": [{"text": "first"}, {"text": "second"}]})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _installer(anthropic_key_set=True)._llm("q") == "first"

    def test_missing_content_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _installer(anthropic_key_set=True)._llm("q") == ""

    def test_empty_content_list_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {"content": []})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _installer(anthropic_key_set=True)._llm("q") == ""

    def test_non_list_content_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, {"content": "not-a-list"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _installer(anthropic_key_set=True)._llm("q") == ""

    def test_missing_api_key_clears_backend_without_io(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        installer = _installer(anthropic_key_set=True)

        def _boom(*args, **kwargs):
            raise AssertionError("must not call the network without a key")

        monkeypatch.setattr(agent.urllib.request, "urlopen", _boom)
        assert installer._llm("q") == ""
        assert installer.backend is None

    def test_request_carries_key_version_and_model_defaults(self, monkeypatch):
        captured = self._capture(monkeypatch, {"content": [{"text": "x"}]})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("MNEMOS_INSTALLER_CLAUDE_MODEL", raising=False)
        _installer(anthropic_key_set=True)._llm("q", system="SYS")
        assert captured["url"] == "https://api.anthropic.com/v1/messages"
        assert captured["body"]["model"] == "claude-haiku-4-5-20251001"
        assert captured["body"]["max_tokens"] == 1024
        assert captured["body"]["system"] == "SYS"
        headers = {key.lower(): value for key, value in captured["headers"].items()}
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_model_name_is_overridable_by_env(self, monkeypatch):
        captured = self._capture(monkeypatch, {"content": [{"text": "x"}]})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("MNEMOS_INSTALLER_CLAUDE_MODEL", "custom-model")
        _installer(anthropic_key_set=True)._llm("q")
        assert captured["body"]["model"] == "custom-model"

    def test_absent_system_prompt_omits_system_field(self, monkeypatch):
        captured = self._capture(monkeypatch, {"content": [{"text": "x"}]})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _installer(anthropic_key_set=True)._llm("q")
        assert "system" not in captured["body"]

    def test_history_is_limited_to_last_eight_messages(self, monkeypatch):
        captured = self._capture(monkeypatch, {"content": [{"text": "x"}]})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        installer = _installer(anthropic_key_set=True)
        installer.history = [{"role": "user", "content": f"msg{index}"} for index in range(10)]
        installer._llm("current")
        messages = captured["body"]["messages"]
        assert messages[0]["content"] == "msg2"
        assert messages[-1] == {"role": "user", "content": "current"}
        assert len(messages) == 9

    @pytest.mark.parametrize(
        "exc",
        [urllib.error.URLError("down"), TimeoutError("slow"), json.JSONDecodeError("bad", "x", 0)],
    )
    def test_errors_clear_backend_and_return_empty(self, monkeypatch, exc, capsys):
        self._capture(monkeypatch, exc)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        installer = _installer(anthropic_key_set=True)
        assert installer._llm("q") == ""
        assert installer.backend is None
        assert "Anthropic API error" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _extract_config — JSON block parsing
# ---------------------------------------------------------------------------


class TestExtractConfig:
    def _extract(self, payload):
        return _installer()._extract_config(_json_block(payload))

    def test_full_payload_maps_every_supported_field(self):
        cfg = self._extract(
            {
                "profile": "Team",
                "db_host": "db.internal",
                "db_port": 6543,
                "db_name": "memdb",
                "db_user": "memuser",
                "db_password": "s3cret",
                "listen_port": 6000,
                "service_user": "svc",
                "auth_enabled": True,
                "rls_enabled": True,
                "graeae_providers": {"openai": {}, "anthropic": {}},
                "inference_embed_host": "http://embed:11434",
                "install_docling": False,
                "create_service": False,
            }
        )
        assert isinstance(cfg, Config)
        assert cfg.profile == "server"  # "Team" lower-cased then canonicalised
        assert cfg.db_host == "db.internal"
        assert cfg.db_port == 6543
        assert cfg.db_name == "memdb"
        assert cfg.db_user == "memuser"
        assert cfg.db_password == "s3cret"
        assert cfg.listen_port == 6000
        assert cfg.service_user == "svc"
        assert cfg.auth_enabled is True
        assert cfg.rls_enabled is True
        assert cfg.graeae_providers == {"openai": {}, "anthropic": {}}
        assert cfg.inference_embed_host == "http://embed:11434"
        assert cfg.install_docling is False
        assert cfg.create_service is False

    def test_absent_keys_keep_wizard_defaults(self):
        cfg = self._extract({})
        assert cfg == Config()

    def test_personal_profile_canonicalises_to_edge(self):
        assert self._extract({"profile": "Personal"}).profile == "edge"

    def test_unknown_profile_is_passed_through_verbatim(self):
        assert self._extract({"profile": "Custom"}).profile == "custom"

    def test_profile_value_is_stripped_and_lowercased(self):
        assert self._extract({"profile": "  Enterprise  "}).profile == "server"

    def test_numeric_strings_are_coerced_for_ports(self):
        cfg = self._extract({"listen_port": "6000", "db_port": "5433"})
        assert cfg.listen_port == 6000
        assert cfg.db_port == 5433

    def test_non_string_scalars_are_stringified_for_text_fields(self):
        cfg = self._extract({"db_name": 5678, "db_password": 5678})
        # int 5678 becomes "5678", which the identifier validator then rejects.
        assert cfg is None

    def test_numeric_string_identifier_starting_with_digit_is_rejected(self):
        assert self._extract({"db_name": "1234"}) is None

    def test_truthiness_is_used_for_boolean_fields(self):
        cfg = self._extract({"auth_enabled": "yes", "rls_enabled": 0})
        assert cfg.auth_enabled is True
        assert cfg.rls_enabled is False

    def test_json_inside_surrounding_prose_is_found(self):
        text = 'Sure! Here it is:\n```json\n{"listen_port": 7000}\n```\nAnything else?'
        assert _installer()._extract_config(text).listen_port == 7000

    @pytest.mark.parametrize("text", ["", "no fence at all", "```json\n```", "```json\n\n```"])
    def test_missing_or_empty_block_returns_none(self, text):
        assert _installer()._extract_config(text) is None

    def test_uppercase_fence_language_is_not_matched(self):
        assert _installer()._extract_config('```JSON\n{"listen_port": 7000}\n```') is None

    def test_malformed_json_returns_none(self):
        assert _installer()._extract_config("```json\n{not json}\n```") is None

    def test_non_numeric_port_returns_none(self):
        assert self._extract({"listen_port": "abc"}) is None

    def test_boolean_port_returns_none(self):
        # bool is not coerced by int() — int(True) would be 1, outside the range.
        assert self._extract({"listen_port": True}) is None

    def test_list_valued_port_returns_none(self):
        assert self._extract({"listen_port": [6000]}) is None

    def test_non_mapping_providers_returns_none(self):
        assert self._extract({"graeae_providers": "openai"}) is None

    @pytest.mark.parametrize("port", [1024, 5002, 65535])
    def test_boundary_ports_inside_range_are_accepted(self, port):
        assert self._extract({"listen_port": port}).listen_port == port

    @pytest.mark.parametrize("port", [0, 80, 443, 1023, 65536, 99999, -1])
    def test_ports_outside_range_are_rejected(self, port):
        assert self._extract({"listen_port": port}) is None

    def test_rejected_port_emits_warning_on_stderr(self, capsys):
        assert self._extract({"listen_port": 80}) is None
        assert "unsafe listen_port=80" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "field_name",
        ["db_user", "db_name", "service_user"],
    )
    @pytest.mark.parametrize(
        "value",
        ["bad user", "bad-user;drop", "", "-leading-dash", "x" * 64, "tab\tname"],
    )
    def test_unsafe_identifier_fields_are_rejected(self, capsys, field_name, value):
        assert self._extract({field_name: value}) is None
        assert f"unsafe {field_name}" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["a", "A_1", "db-name", "x" * 63])
    def test_boundary_safe_identifiers_are_accepted(self, value):
        assert self._extract({"db_user": value}).db_user == value

    @pytest.mark.parametrize(
        "host",
        ["localhost", "127.0.0.1", "db.internal", "postgres-01", "a" * 254],
    )
    def test_plausible_db_hosts_are_accepted(self, host):
        assert self._extract({"db_host": host}).db_host == host

    @pytest.mark.parametrize(
        "host",
        ["", "has space", "semi;colon", "host\nnewline", "$(cmd)", "a" * 256, " trailing"],
    )
    def test_clearly_unsafe_db_hosts_are_rejected(self, host, capsys):
        assert self._extract({"db_host": host}) is None
        assert "unsafe db_host" in capsys.readouterr().err

    def test_hostname_starting_with_localhost_is_a_valid_generic_hostname(self):
        # Pins the documented intent: the validator allows "hostname, IPv4,
        # localhost". "localhostfoo" is matched by the generic-hostname
        # alternative (starts alphanumeric, only [A-Za-z0-9-.]) and is accepted.
        assert self._extract({"db_host": "localhostfoo"}).db_host == "localhostfoo"

    def test_db_port_is_coerced_but_not_range_checked(self):
        # Asymmetry pinned as-is: listen_port is range-checked (1024-65535),
        # db_port is only int()-coerced and written straight into the summary.
        assert self._extract({"db_port": 99999}).db_port == 99999
        assert self._extract({"db_port": -1}).db_port == -1


# ---------------------------------------------------------------------------
# _heuristic_extract — plain-text fallback
# ---------------------------------------------------------------------------


class TestHeuristicExtract:
    def _heuristic(self, text):
        return _installer()._heuristic_extract(text)

    @pytest.mark.parametrize("text", ["", "hello there", "I love my DB"])
    def test_text_with_no_signal_returns_none(self, text):
        assert self._heuristic(text) is None

    @pytest.mark.parametrize(
        ("text", "profile"),
        [
            ("I want a personal memory server", "edge"),
            ("just me, thanks", "edge"),
            ("for personal use", "edge"),
            ("enterprise deployment", "server"),
            ("a team installation", "server"),
        ],
    )
    def test_profile_words_map_to_edge_or_server(self, text, profile):
        assert self._heuristic(text).profile == profile

    def test_personal_wins_over_enterprise_when_both_appear(self):
        assert self._heuristic("personal use, not enterprise").profile == "edge"

    def test_enterprise_wins_over_team_when_both_appear(self):
        assert self._heuristic("a team or enterprise setup").profile == "server"

    def test_db_name_from_assignment(self):
        assert self._heuristic("db_name=proddb").db_name == "proddb"

    def test_db_name_from_colon_form(self):
        assert self._heuristic("db: mydb").db_name == "mydb"

    def test_mentioning_mnemos_defaults_the_db_name(self):
        assert self._heuristic("install mnemos for me").db_name == "mnemos"

    def test_explicit_db_name_wins_over_mnemos_mention(self):
        assert self._heuristic("mnemos with db_name=custom").db_name == "custom"

    def test_db_user_from_assignment(self):
        assert self._heuristic("db_user=alice").db_user == "alice"

    def test_db_user_from_username_alias(self):
        assert self._heuristic("username=bob").db_user == "bob"

    @pytest.mark.parametrize(
        ("text", "port"),
        [
            ("port 6000", 6000),
            ("use 5002", 5002),
            ("listen on 8080", 8080),
            ("pick 4433", 4433),
            ("set to 12345", 12345),
        ],
    )
    def test_ports_are_extracted_from_plain_prose(self, text, port):
        assert self._heuristic(text).listen_port == port

    @pytest.mark.parametrize("text", ["in 2024", "during 2025", "maybe 2026"])
    def test_year_like_values_are_skipped(self, text):
        assert self._heuristic(text) is None

    def test_first_valid_port_wins(self):
        assert self._heuristic("ports 7000 and 8000").listen_port == 7000

    def test_year_is_skipped_so_the_next_port_wins(self):
        assert self._heuristic("in 2024 use 6666").listen_port == 6666

    @pytest.mark.parametrize("text", ["443", "80", "1000", "65536"])
    def test_out_of_range_or_unmatched_numbers_are_ignored(self, text):
        assert self._heuristic(text) is None

    def test_a_valid_port_embedded_in_words_counts_as_a_change(self):
        # 9999 is in range, so "abc 9999 def" IS a signal.
        assert self._heuristic("abc 9999 def").listen_port == 9999

    def test_assignments_are_case_insensitive_but_values_kept_as_written(self):
        cfg = self._heuristic("DB_NAME=ProdDB")
        assert cfg.db_name == "proddb"  # matching runs on the lower-cased text

    def test_defaults_are_kept_for_unmatched_fields(self):
        cfg = self._heuristic("port 6000")
        assert cfg.profile == "edge"
        assert cfg.db_user == "mnemos_user"
        assert cfg.db_name == "mnemos"

    def test_profile_word_alone_counts_as_a_change(self):
        # Setting only the profile still marks the text as changed, so a
        # config is returned rather than None.
        cfg = self._heuristic("team")
        assert cfg is not None
        assert cfg.profile == "server"
        assert cfg.listen_port == 5002  # untouched default


# ---------------------------------------------------------------------------
# _confirm_config
# ---------------------------------------------------------------------------


class TestConfirmConfig:
    def _cfg(self):
        return Config(profile="server", db_user="u", db_name="d", db_password="p", listen_port=6000)

    def test_blank_answer_accepts(self, monkeypatch):
        _answer_input(monkeypatch, [""])
        cfg = self._cfg()
        assert _installer()._confirm_config(cfg) is cfg

    @pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "  y  "])
    def test_affirmative_answers_accept(self, monkeypatch, answer):
        _answer_input(monkeypatch, [answer])
        cfg = self._cfg()
        assert _installer()._confirm_config(cfg) is cfg

    @pytest.mark.parametrize("answer", ["n", "N", "no", "NO"])
    def test_negative_answers_cancel(self, monkeypatch, answer, capsys):
        _answer_input(monkeypatch, [answer])
        assert _installer()._confirm_config(self._cfg()) is None
        assert "Installation cancelled." in capsys.readouterr().out

    def test_unrecognised_answer_reprompts_then_accepts(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["maybe", "y"])
        cfg = self._cfg()
        assert _installer()._confirm_config(cfg) is cfg
        assert "Please enter Y or n." in capsys.readouterr().out

    def test_three_invalid_answers_cancel(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["maybe", "sure", "ok"])
        assert _installer()._confirm_config(self._cfg()) is None
        assert "No valid response after 3 attempts" in capsys.readouterr().out

    def test_eof_during_confirmation_returns_none(self, monkeypatch):
        _answer_input(monkeypatch, [])
        assert _installer()._confirm_config(self._cfg()) is None

    def test_keyboard_interrupt_during_confirmation_returns_none(self, monkeypatch):
        def _interrupt(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _interrupt)
        assert _installer()._confirm_config(self._cfg()) is None

    def test_summary_prints_password_state_and_providers(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["y"])
        cfg = Config(db_password="", graeae_providers={"openai": {}})
        _installer()._confirm_config(cfg)
        out = capsys.readouterr().out
        assert "Password:      (empty)" in out
        assert "GRAEAE providers: openai" in out

    def test_summary_omits_provider_line_when_none_configured(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["y"])
        _installer()._confirm_config(Config())
        assert "GRAEAE providers" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# AgentInstaller.run — the conversational loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    def test_no_backend_returns_none_before_any_prompt(self, no_network, capsys):
        assert _installer().run() is None
        assert "Using LLM backend" not in capsys.readouterr().out

    def test_seeds_history_with_the_opening_assistant_message(self, monkeypatch):
        _answer_input(monkeypatch, ["exit"])
        installer = _installer(graeae_reachable=True)
        installer.run()
        assert len(installer.history) == 1
        assert installer.history[0]["role"] == "assistant"
        assert "I've detected the following environment" in installer.history[0]["content"]

    @pytest.mark.parametrize("word", ["exit", "quit", "manual", "EXIT", "  Quit  "])
    def test_exit_words_fall_back_to_wizard(self, monkeypatch, word, capsys):
        _answer_input(monkeypatch, [word])
        installer = _installer(graeae_reachable=True)
        assert installer.run() is None
        assert "Falling back to wizard mode." in capsys.readouterr().out

    def test_eof_returns_none(self, monkeypatch, capsys):
        _answer_input(monkeypatch, [])
        assert _installer(graeae_reachable=True).run() is None
        assert "Cancelled." in capsys.readouterr().out

    def test_keyboard_interrupt_returns_none(self, monkeypatch):
        def _interrupt(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr(agent, "input", _interrupt, raising=False)
        monkeypatch.setattr("builtins.input", _interrupt)
        assert _installer(graeae_reachable=True).run() is None

    def test_blank_input_is_skipped_without_calling_the_llm(self, monkeypatch):
        _answer_input(monkeypatch, ["", "   ", "exit"])
        installer = _installer(graeae_reachable=True)
        calls = _stub_llm(monkeypatch, installer, [])
        assert installer.run() is None
        assert calls == []

    def test_empty_llm_response_falls_back_to_wizard(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["hello"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(monkeypatch, installer, [""])
        assert installer.run() is None
        assert "LLM backend unavailable" in capsys.readouterr().out

    def test_llm_receives_system_prompt_and_user_text(self, monkeypatch):
        _answer_input(monkeypatch, ["set up something", "exit"])
        installer = _installer(graeae_reachable=True)
        calls = _stub_llm(monkeypatch, installer, ["what next?"])
        installer.run()
        assert calls[0]["prompt"] == "set up something"
        assert calls[0]["system"] == installer._build_system_prompt()

    def test_user_and_assistant_turns_are_appended_to_history(self, monkeypatch):
        _answer_input(monkeypatch, ["set up something", "exit"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(monkeypatch, installer, ["what next?"])
        installer.run()
        assert installer.history[1] == {"role": "user", "content": "set up something"}
        assert installer.history[2] == {"role": "assistant", "content": "what next?"}

    def test_json_response_leads_to_confirmation_and_returns_config(self, monkeypatch):
        _answer_input(monkeypatch, ["do it", "y"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(
            monkeypatch,
            installer,
            [_json_block({"db_name": "proddb", "db_user": "prod", "listen_port": 6000})],
        )
        cfg = installer.run()
        assert isinstance(cfg, Config)
        assert cfg.db_name == "proddb"
        assert cfg.listen_port == 6000

    def test_declining_confirmation_returns_none(self, monkeypatch):
        _answer_input(monkeypatch, ["do it", "n"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(monkeypatch, installer, [_json_block({"db_name": "proddb"})])
        assert installer.run() is None

    def test_unsafe_json_falls_through_to_another_turn(self, monkeypatch):
        _answer_input(monkeypatch, ["first", "second", "y"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(
            monkeypatch,
            installer,
            [
                _json_block({"listen_port": 80}),  # rejected
                _json_block({"db_name": "good", "db_user": "u", "listen_port": 5002}),
            ],
        )
        cfg = installer.run()
        assert cfg is not None and cfg.db_name == "good"

    def test_max_turns_exhausted_returns_none(self, monkeypatch, capsys):
        _answer_input(monkeypatch, [f"turn{index}" for index in range(AgentInstaller.MAX_TURNS)])
        installer = _installer(graeae_reachable=True)
        calls = _stub_llm(monkeypatch, installer, ["no config"] * AgentInstaller.MAX_TURNS)
        assert installer.run() is None
        assert len(calls) == AgentInstaller.MAX_TURNS
        assert "Could not extract a complete configuration" in capsys.readouterr().out

    def test_forced_extraction_nudge_is_added_on_the_penultimate_turn_only(self, monkeypatch):
        _answer_input(monkeypatch, [f"turn{index}" for index in range(AgentInstaller.MAX_TURNS)])
        installer = _installer(graeae_reachable=True)
        calls = _stub_llm(monkeypatch, installer, ["no config"] * AgentInstaller.MAX_TURNS)
        installer.run()
        nudged = [index for index, call in enumerate(calls) if "[System: Maximum turns reached." in call["prompt"]]
        assert nudged == [AgentInstaller.MAX_TURNS - 2]  # turn 9 of 10, zero-based index 8

    def test_heuristic_is_not_consulted_before_turn_five(self, monkeypatch):
        _answer_input(monkeypatch, ["turn1", "turn2", "turn3", "turn4", "exit"])
        installer = _installer(graeae_reachable=True)
        consulted = []
        monkeypatch.setattr(installer, "_heuristic_extract", lambda text: consulted.append(text))
        _stub_llm(monkeypatch, installer, ["no config"] * 4)
        installer.run()
        assert consulted == []

    def test_heuristic_is_consulted_from_turn_five_but_not_returned_yet(self, monkeypatch):
        # Turns 5..7 consult the heuristic; only turn >= MAX_TURNS-2 (8) returns it.
        answers = ["port 6000"] + [f"turn{index}" for index in range(6)] + ["exit"]  # 7 turns
        _answer_input(monkeypatch, answers)
        installer = _installer(graeae_reachable=True)
        confirmed = []
        monkeypatch.setattr(installer, "_confirm_config", lambda cfg: confirmed.append(cfg) or None)
        _stub_llm(monkeypatch, installer, ["no config"] * len(answers))
        installer.run()
        assert confirmed == []

    def test_heuristic_config_is_returned_at_turn_eight(self, monkeypatch, capsys):
        answers = ["port 6000"] + [f"turn{index}" for index in range(8)]
        _answer_input(monkeypatch, answers)
        installer = _installer(graeae_reachable=True)
        confirmed = []
        monkeypatch.setattr(installer, "_confirm_config", lambda cfg: confirmed.append(cfg) or cfg)
        _stub_llm(monkeypatch, installer, ["no config"] * len(answers))
        cfg = installer.run()
        assert cfg is not None
        assert cfg.listen_port == 6000
        assert len(confirmed) == 1
        assert "Extracting config from conversation using heuristics" in capsys.readouterr().out

    def test_history_is_aggregated_across_user_turns_for_the_heuristic(self, monkeypatch):
        answers = ["I want", "a personal", "server on", "port", "6000 and", "more", "text", "here"]
        _answer_input(monkeypatch, answers)
        installer = _installer(graeae_reachable=True)
        seen = []
        original = installer._heuristic_extract
        monkeypatch.setattr(installer, "_heuristic_extract", lambda text: seen.append(text) or original(text))
        monkeypatch.setattr(installer, "_confirm_config", lambda cfg: cfg)
        _stub_llm(monkeypatch, installer, ["no config"] * len(answers))
        installer.run()
        assert seen  # consulted at least once
        assert "I want" in seen[-1] and "here" in seen[-1]

    def test_run_returns_none_when_the_backend_drops_midway(self, monkeypatch, capsys):
        _answer_input(monkeypatch, ["hello", "again"])
        installer = _installer(graeae_reachable=True)
        _stub_llm(monkeypatch, installer, ["ok", ""])
        assert installer.run() is None
        assert "LLM backend unavailable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


class TestEntryPoints:
    def test_run_agent_installer_detects_then_runs(self, monkeypatch, capsys):
        detected = SystemInfo(hostname="detected", graeae_reachable=True)
        expected = Config(profile="server")
        seen = {}

        monkeypatch.setattr(agent, "detect_environment", lambda: detected)

        def _fake_run(self):
            seen["info"] = self.info
            return expected

        monkeypatch.setattr(AgentInstaller, "run", _fake_run)
        assert run_agent_installer() is expected
        assert seen["info"] is detected
        assert "Detecting environment..." in capsys.readouterr().out

    def test_run_agent_installer_propagates_none(self, monkeypatch):
        monkeypatch.setattr(agent, "detect_environment", lambda: SystemInfo())
        monkeypatch.setattr(AgentInstaller, "run", lambda self: None)
        assert run_agent_installer() is None

    def test_run_agent_passes_systeminfo_through_untouched(self, monkeypatch):
        info = SystemInfo(hostname="direct")
        seen = {}
        monkeypatch.setattr(agent, "detect_environment", lambda: (_ for _ in ()).throw(AssertionError("no detect")))

        def _fake_run(self):
            seen["info"] = self.info
            return None

        monkeypatch.setattr(AgentInstaller, "run", _fake_run)
        run_agent(info)
        assert seen["info"] is info

    def test_run_agent_maps_duck_typed_detector_result(self, monkeypatch):
        class FakeDetected:
            os_type = "darwin"
            distro_version = "14.5"
            python_version = (3, 12, 1)
            pg_running = True
            pg_version = "16.2"
            disk_free_gb = 42.5

        detected = SystemInfo()
        seen = {}
        monkeypatch.setattr(agent, "detect_environment", lambda: detected)
        monkeypatch.setattr(AgentInstaller, "run", lambda self: seen.setdefault("info", self.info) and None)

        run_agent(FakeDetected())
        mapped = seen["info"]
        assert mapped is detected
        assert mapped.os_name == "darwin"
        assert mapped.os_version == "14.5"
        assert mapped.python_version == "3.12.1"
        assert mapped.postgres_running is True
        assert mapped.postgres_version == "16.2"
        assert mapped.disk_free_gb == 42.5

    def test_run_agent_missing_attributes_keep_detected_values(self, monkeypatch):
        class Empty:  # no attributes at all
            pass

        detected = SystemInfo(
            os_name="linux",
            os_version="orig",
            python_version="3.11.0",
            postgres_running=False,
            postgres_version="",
            disk_free_gb=7.5,
        )
        seen = {}
        monkeypatch.setattr(agent, "detect_environment", lambda: detected)
        monkeypatch.setattr(AgentInstaller, "run", lambda self: seen.setdefault("info", self.info) and None)

        run_agent(Empty())
        mapped = seen["info"]
        assert mapped.os_name == "linux"  # getattr(..., default) keeps the detected value
        assert mapped.os_version == "orig"
        assert mapped.python_version == "3.11.0"
        assert mapped.disk_free_gb == 7.5

    def test_run_agent_empty_python_version_tuple_keeps_detected_interpreter(self, monkeypatch):
        class FakeDetected:
            python_version = ()

        detected = SystemInfo(python_version="3.11.0")
        seen = {}
        monkeypatch.setattr(agent, "detect_environment", lambda: detected)
        monkeypatch.setattr(AgentInstaller, "run", lambda self: seen.setdefault("info", self.info) and None)

        run_agent(FakeDetected())
        assert seen["info"].python_version == "3.11.0"

    def test_run_agent_list_python_version_is_ignored(self, monkeypatch):
        class FakeDetected:
            python_version = [3, 12]  # list, not tuple

        detected = SystemInfo(python_version="3.11.0")
        seen = {}
        monkeypatch.setattr(agent, "detect_environment", lambda: detected)
        monkeypatch.setattr(AgentInstaller, "run", lambda self: seen.setdefault("info", self.info) and None)

        run_agent(FakeDetected())
        assert seen["info"].python_version == "3.11.0"


# ---------------------------------------------------------------------------
# __main__ behaviour
# ---------------------------------------------------------------------------


class TestMainGuard:
    """The ``if __name__ == "__main__":`` block (agent.py:615-621).

    Run as a real subprocess so the block executes exactly as shipped. With
    stdin closed and no LLM backend reachable, ``run_agent_installer`` yields
    ``None`` and the block must exit 1 — deterministic regardless of host.
    """

    @staticmethod
    def _run_main(cfg_expr):
        """Execute only the module's __main__ block in a fresh interpreter."""
        source = (
            "import sys\n"
            "import mnemos.installer.agent as a\n"
            "from mnemos.installer.wizard import Config\n"
            "a.run_agent_installer = lambda: (" + cfg_expr + ")\n"
            "import inspect, textwrap\n"
            "src = inspect.getsource(a)\n"
            "marker = 'if __name__ == \"__main__\":\\n'\n"
            "assert marker in src, 'agent.py __main__ marker not found'\n"
            "body = src[src.index(marker) + len(marker):]\n"
            "assert body.strip(), 'agent.py __main__ block is empty'\n"
            "code = compile(textwrap.dedent(body), a.__file__, 'exec')\n"
            "try:\n"
            "    exec(code, vars(a))\n"
            "except SystemExit as exc:\n"
            "    sys.exit(exc.code)\n"
        )
        return subprocess.run(
            [sys.executable, "-c", source],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "MNEMOS_GRAEAE_URL": "http://127.0.0.1:1", "ANTHROPIC_API_KEY": ""},
        )

    def test_main_exits_zero_and_reports_success(self):
        result = self._run_main("Config()")
        assert result.returncode == 0
        assert "Config collected successfully." in result.stdout

    def test_main_exits_one_and_reports_wizard_fallback(self):
        result = self._run_main("None")
        assert result.returncode == 1
        assert "No config produced" in result.stdout
        assert "falling back to wizard mode" in result.stdout
