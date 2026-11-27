"""ARTEMIS dedup mode resolution (FB414, 2026-11-06).

Pins ``artemis_dedup_mode`` / ``artemis_dedup_cross_namespace`` in
``mnemos/domain/artemis_dedup.py`` together with the settings they read
(``_ArtemisSettings`` in ``mnemos/core/config.py``):

* absent ``MNEMOS_ARTEMIS_DEDUP_MODE`` -> ``"reject"``;
* the four documented modes round-trip, with the value stripped and lowercased;
* an unknown mode is NOT refused — it silently becomes ``"reject"`` (divergence
  from the brief's "refused naming the allowed set");
* ``MNEMOS_ARTEMIS_DEDUP_CROSS_NAMESPACE`` parses the documented boolean
  vocabularies through pydantic, and a non-boolean value is refused by the
  settings layer with a ``ValidationError`` (not by the helper);
* the helpers do no parsing of their own: ``bool(...)`` on whatever the settings
  hold, and a second mode normalisation that falls back to ``"reject"``.

Every case reloads the cached settings and deletes the env vars afterwards, so
one case cannot leak into the next.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mnemos.core import config as core_config  # noqa: E402
from mnemos.domain.artemis_dedup import (  # noqa: E402
    artemis_dedup_cross_namespace,
    artemis_dedup_mode,
)

MODE_ENV = "MNEMOS_ARTEMIS_DEDUP_MODE"
CROSS_ENV = "MNEMOS_ARTEMIS_DEDUP_CROSS_NAMESPACE"
VALID_MODES = {"reject", "merge", "warn", "off"}


@pytest.fixture(autouse=True)
def _isolated_artemis_settings(monkeypatch):
    for name in (MODE_ENV, CROSS_ENV):
        monkeypatch.delenv(name, raising=False)
    core_config.reload_settings()
    yield
    for name in (MODE_ENV, CROSS_ENV):
        monkeypatch.delenv(name, raising=False)
    core_config.reload_settings()


def _mode_with_env(monkeypatch, value: str) -> str:
    monkeypatch.setenv(MODE_ENV, value)
    core_config.reload_settings()
    return artemis_dedup_mode()


def _cross_with_env(monkeypatch, value: str) -> bool:
    monkeypatch.setenv(CROSS_ENV, value)
    core_config.reload_settings()
    return artemis_dedup_cross_namespace()


# ── the mode ───────────────────────────────────────────────────────────────


def test_absent_mode_yields_the_documented_default():
    assert artemis_dedup_mode() == "reject"
    assert artemis_dedup_mode() in VALID_MODES


def test_each_documented_mode_round_trips(monkeypatch):
    for mode in sorted(VALID_MODES):
        assert _mode_with_env(monkeypatch, mode) == mode


def test_mode_is_stripped_and_lowercased(monkeypatch):
    assert _mode_with_env(monkeypatch, " MERGE ") == "merge"
    assert _mode_with_env(monkeypatch, "Warn") == "warn"
    assert _mode_with_env(monkeypatch, "oFf") == "off"


def test_unknown_mode_falls_back_to_reject_instead_of_being_refused(monkeypatch):
    # Divergence from the brief: no exception, no message naming the allowed
    # set -- the settings validator maps anything outside the set to "reject".
    for raw in ("delete", "bogus", "1", "rejectt", "merge,off"):
        assert _mode_with_env(monkeypatch, raw) == "reject", raw


def test_blank_mode_yields_reject(monkeypatch):
    assert _mode_with_env(monkeypatch, "") == "reject"
    assert _mode_with_env(monkeypatch, "   ") == "reject"


def test_mode_is_stable_across_calls(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "merge")
    core_config.reload_settings()
    assert [artemis_dedup_mode() for _ in range(3)] == ["merge", "merge", "merge"]


# ── cross-namespace boolean ────────────────────────────────────────────────


def test_cross_namespace_defaults_to_false():
    assert artemis_dedup_cross_namespace() is False


def test_cross_namespace_accepts_the_documented_truthy_values(monkeypatch):
    for raw in ("true", "1", "yes", "on", "t", "y", "TRUE", "Yes"):
        assert _cross_with_env(monkeypatch, raw) is True, raw


def test_cross_namespace_accepts_the_documented_falsey_values(monkeypatch):
    for raw in ("false", "0", "no", "off", "f", "n", "FALSE", "No"):
        assert _cross_with_env(monkeypatch, raw) is False, raw


def test_cross_namespace_refuses_a_non_boolean_at_the_settings_layer(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv(CROSS_ENV, "maybe")
    with pytest.raises(ValidationError) as caught:
        core_config.reload_settings()
    # The refusal names the field/env source, not an allowed value set.
    assert "dedup_cross_namespace" in str(caught.value) or CROSS_ENV in str(caught.value)


# ── the helpers do no parsing of their own ─────────────────────────────────


class _Artemis:
    def __init__(self, mode, cross):
        self.dedup_mode = mode
        self.dedup_cross_namespace = cross


class _Settings:
    def __init__(self, mode, cross):
        self.artemis = _Artemis(mode, cross)


def _fake_settings(monkeypatch, mode, cross):
    from mnemos.domain import artemis_dedup

    monkeypatch.setattr(artemis_dedup, "get_settings", lambda: _Settings(mode, cross))


def test_mode_helper_falls_back_for_an_out_of_vocabulary_value(monkeypatch):
    _fake_settings(monkeypatch, "DELETE", False)
    assert artemis_dedup_mode() == "reject"


def test_mode_helper_normalises_what_it_is_handed(monkeypatch):
    _fake_settings(monkeypatch, "  Merge ", False)
    assert artemis_dedup_mode() == "merge"


def test_cross_namespace_helper_is_a_bare_bool_coercion(monkeypatch):
    # `bool(...)`, not a string parser: a truthy string reads True even when it
    # means "false". Only the settings layer parses the env value.
    _fake_settings(monkeypatch, "reject", "false")
    assert artemis_dedup_cross_namespace() is True
    _fake_settings(monkeypatch, "reject", "")
    assert artemis_dedup_cross_namespace() is False
    _fake_settings(monkeypatch, "reject", 1)
    assert artemis_dedup_cross_namespace() is True
