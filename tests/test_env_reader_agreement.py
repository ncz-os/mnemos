"""Environment-variable reader agreement (M7).

Four environment variables were read with two different vocabularies in
two places, so the same knob had two answers:

* ``MNEMOS_AUDIT_CHAIN`` — only ``on``/``required`` counted as on, so
  ``1``/``true`` silently meant OFF: a deployment that believed it was
  audited was not;
* ``MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE`` — any UNRECOGNISED value
  counted as true, so a typo widened what leaves the host;
* ``MNEMOS_DEFAULT_NAMESPACE`` — the REST path defaulted to "default"
  while the MCP connector defaulted to None;
* ``MNEMOS_EMBED_THREADS`` — the settings field said "0 = auto" while
  the wrapper the embedder actually uses said "cpu_count()", and a
  literal 0 was passed straight through to llama-cpp (which then picked
  a third number).

These tests pin one vocabulary per variable.
"""

from __future__ import annotations

import logging
import os

import pytest

from mnemos.core import config


# ─── MNEMOS_AUDIT_CHAIN ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "yes", "On", "enabled"])
def test_audit_chain_truthy_spellings_mean_on(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """1/true must mean ON — an operator who writes it means it."""
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", value)
    assert config.audit_chain_enabled_flag() is True
    assert config.audit_chain_required_flag() is False


@pytest.mark.parametrize("value", ["off", "0", "false", "FALSE", "no", "disabled"])
def test_audit_chain_falsy_spellings_mean_off(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", value)
    assert config.audit_chain_enabled_flag() is False
    assert config.audit_chain_required_flag() is False


def test_audit_chain_required_is_still_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    assert config.audit_chain_enabled_flag() is True
    assert config.audit_chain_required_flag() is True


def test_audit_chain_unset_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)
    assert config.audit_chain_enabled_flag() is False
    assert config.audit_chain_required_flag() is False


def test_audit_chain_unknown_value_warns_and_is_off(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "maybe")
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config.audit_chain_enabled_flag() is False

    assert any("MNEMOS_AUDIT_CHAIN" in record.getMessage() for record in caplog.records)


# ─── MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE ─────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "ON"])
def test_federation_feed_include_private_truthy(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", value)
    assert config.federation_feed_include_private() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "OFF"])
def test_federation_feed_include_private_falsy(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", value)
    assert config.federation_feed_include_private() is False


def test_federation_feed_include_private_unset_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented default is the trusted-LAN posture."""
    monkeypatch.delenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", raising=False)
    assert config.federation_feed_include_private() is True


def test_federation_feed_include_private_unknown_value_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrecognised value must NOT silently mean true."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "flase")
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        config.federation_feed_include_private()

    messages = [record.getMessage() for record in caplog.records]
    assert any("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE" in message for message in messages), messages


def test_federation_feed_include_private_unknown_value_is_not_silent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo must not quietly widen what leaves the host without a word."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "perhaps")
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        config.federation_feed_include_private()

    assert caplog.records, "an unrecognised value must be reported"


# ─── MNEMOS_DEFAULT_NAMESPACE ───────────────────────────────────────────────


def test_default_namespace_unset_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reader: unset means "no namespace configured"."""
    monkeypatch.delenv("MNEMOS_DEFAULT_NAMESPACE", raising=False)
    assert config.default_namespace_env() == ""
    assert config.connector_default_namespace() is None


def test_default_namespace_set_is_used_by_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "work")
    assert config.default_namespace_env() == "work"
    assert config.connector_default_namespace() == "work"


def test_default_namespace_blank_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "   ")
    assert config.default_namespace_env() == ""
    assert config.connector_default_namespace() is None


def test_default_namespace_connector_and_settings_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REST field and the MCP connector read the SAME value.

    The two consumers apply it at different times (the REST field is a
    fallback for callers with no namespace of their own; the connector
    stamp must not override a caller's own namespace), but they must
    never disagree about WHAT the variable says.
    """
    monkeypatch.setenv("MNEMOS_DEFAULT_NAMESPACE", "work")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", "/nonexistent/mnemos-config.toml")
    try:
        settings = config.reload_settings()
        assert settings.auth.default_namespace == config.connector_default_namespace()
    finally:
        config.reload_settings()


# ─── MNEMOS_EMBED_THREADS ───────────────────────────────────────────────────


def test_embed_threads_unset_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset resolves to the CPU count — the documented auto value."""
    monkeypatch.delenv("MNEMOS_EMBED_THREADS", raising=False)
    assert config.embed_threads_env() == max(1, os.cpu_count() or 4)


def test_embed_threads_zero_means_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 = auto must resolve HERE, not be handed to llama-cpp.

    llama-cpp treats a falsy n_threads as its own
    ``max(cpu_count // 2, 1)`` default — a third number, different from
    the documented auto value.
    """
    monkeypatch.setenv("MNEMOS_EMBED_THREADS", "0")
    assert config.embed_threads_env() == max(1, os.cpu_count() or 4)


def test_embed_threads_explicit_value_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOS_EMBED_THREADS", "3")
    assert config.embed_threads_env() == 3


def test_embed_threads_invalid_value_warns_and_uses_auto(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MNEMOS_EMBED_THREADS", "many")
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config.embed_threads_env() == max(1, os.cpu_count() or 4)

    assert any("MNEMOS_EMBED_THREADS" in record.getMessage() for record in caplog.records)


def test_embed_threads_negative_value_warns_and_uses_auto(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MNEMOS_EMBED_THREADS", "-4")
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config.embed_threads_env() == max(1, os.cpu_count() or 4)

    assert any("MNEMOS_EMBED_THREADS" in record.getMessage() for record in caplog.records)
